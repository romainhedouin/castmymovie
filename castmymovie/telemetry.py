"""Live health of a cast: what the TV says it is doing, how the link is holding up, and an *estimate*
of how many seconds the TV has buffered ahead of what it is showing.

Nothing here can touch playback: it only reads the TV's status and the OS's TCP counters.

About the buffer figure. Cast receivers do not report how much they have buffered, so it is inferred:

    buffered  ≈  (media time the bytes sent so far add up to)  -  (where the TV is now)

Bytes become media time through the movie's real bitrate profile. The conversion is not exact (VLC
re-muxes the stream and converts the audio), so the figure is only good to roughly +/-10 s (sometimes
worse), and it is re-anchored whenever the truth is known: at a stall the buffer is empty, and after a seek
the TV starts filling from scratch. Measured against a real TV, it showed a believable sawtooth that stayed
flat while paused and stayed in a plausible band over a 5 minute run, without runaway drift.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass

from . import linkstats
from .castlink import StatusReader

K = 1.05                # stream bytes per source-file byte (Matroska framing + re-encoded audio); measured on a real cast
MAX_BUFFER_S = 120.0
SMOOTHING = 0.35        # exponential moving average weight per update
JUMP_S = 15.0           # a bigger position jump than this means a seek: re-anchor
REFILL_S = 20.0         # after a re-anchor the TV is rebuilding its buffer: say so instead of crying wolf


class BufferEstimator:
    def __init__(self, curve: list, k: float = K):
        self.cum, self.k = curve, k
        self._p0: float | None = None
        self._b0 = 0
        self._last_position: float | None = None
        self._last_bytes = 0
        self.reanchored = False          # True when the last update() had to start over (seek, stall, new connection)
        self.value: float | None = None

    def _c(self, t: float) -> float:
        t = min(max(t, 0.0), len(self.cum) - 1.0)
        i = min(int(t), len(self.cum) - 2)
        return self.cum[i] + (self.cum[i + 1] - self.cum[i]) * (t - i)

    def _c_inv(self, b: float) -> float:
        i = max(min(bisect.bisect_right(self.cum, b) - 1, len(self.cum) - 2), 0)
        width = self.cum[i + 1] - self.cum[i]
        return i + ((b - self.cum[i]) / width if width else 0.0)

    def begin(self, stream_start: float) -> None:
        """The stream began at media time `stream_start` and byte counter 0."""
        self._p0, self._b0, self.value = stream_start, 0, None

    def anchor(self, position: float, bytes_out: int, buffered: float = 0.0) -> None:
        """We know the buffer holds `buffered` seconds right now (0 after a stall or a seek)."""
        self._p0 = position + buffered
        self._b0 = bytes_out
        self.value = buffered

    def update(self, position: float, bytes_out: int, stalled: bool = False) -> float | None:
        if len(self.cum) < 3:
            return None
        jumped = self._last_position is not None and abs(position - self._last_position) > JUMP_S
        self._last_position = position
        replaced = bytes_out < self._last_bytes          # the TCP connection was replaced: its counter restarted
        self._last_bytes = bytes_out
        self.reanchored = False
        if self._p0 is None or replaced or stalled or jumped:
            self.reanchored = self._p0 is not None
            self.anchor(position, bytes_out)
            return self.value
        delivered = self._c_inv(self._c(self._p0) + (bytes_out - self._b0) / self.k)
        raw = min(max(delivered - position, 0.0), MAX_BUFFER_S)
        self.value = raw if self.value is None else self.value + SMOOTHING * (raw - self.value)
        return self.value


@dataclass(frozen=True)
class Snapshot:
    tv_state: str | None = None       # PLAYING | PAUSED | BUFFERING | IDLE, straight from the TV
    tv_position: float | None = None
    buffer_s: float | None = None     # estimate, see module docstring
    delivered_mbps: float | None = None
    needed_mbps: float | None = None
    retx_pct: float | None = None
    rtt_ms: float | None = None
    refilling: bool = False           # a seek or stall just emptied the buffer; a low reading is expected

    @property
    def verdict(self) -> str | None:
        """good | fair | poor | refilling, or None when there is nothing to judge."""
        low = lambda limit: self.buffer_s is not None and self.buffer_s < limit and not self.refilling  # noqa: E731
        if self.tv_state == "BUFFERING" or low(5) or (self.retx_pct is not None and self.retx_pct > 5):
            return "poor"
        if low(15) or (self.retx_pct is not None and self.retx_pct > 2) or (self.rtt_ms is not None and self.rtt_ms > 250):
            return "fair"
        if self.refilling:
            return "refilling"
        if self.buffer_s is None and self.retx_pct is None and self.delivered_mbps is None and self.tv_state is None:
            return None
        return "good"


class Telemetry:
    """Samples once a second on a background thread so the player's keys stay responsive."""

    INTERVAL = 1.0
    RECONNECT_EVERY = 10.0

    def __init__(self, pid: int, host: str, curve: list | None = None, reader=None,
                 read_link=linkstats.read_sample, clock=time.monotonic):
        self.pid, self.host, self.curve = pid, host, curve or []
        self.reader = reader if reader is not None else StatusReader(host)
        self.read_link, self.clock = read_link, clock
        self.link = linkstats.LinkMonitor()
        self.estimator = BufferEstimator(self.curve) if len(self.curve) >= 3 else None
        self.position = 0.0            # set by the player on every tick
        self.latest = Snapshot()
        self._began = False
        self._refill_until = 0.0
        self._t0 = clock()
        self._last_connect = -1e9
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- one sampling pass (also used directly by the tests)
    def step(self) -> Snapshot:
        now = self.clock()
        if not self.reader.connected and now - self._last_connect >= self.RECONNECT_EVERY:
            self._last_connect = now
            self.reader.open(timeout=3.0)
        tv = self.reader.poll() if self.reader.connected else None
        sample = self.read_link(self.pid, self.host)
        if sample:
            self.link.add(sample)
        health = self.link.health()

        position = self.position
        stalled = bool(tv and tv.state == "BUFFERING")
        buffer_s = None
        if self.estimator and sample:
            if not self._began and tv and tv.position is not None:
                self.estimator.begin(position - tv.position)   # stream start = now minus how far the TV has played
                self._began = True
            elif not self._began:
                self.estimator.begin(max(position - (now - self._t0), 0.0))
                self._began = True
            buffer_s = self.estimator.update(position, sample.bytes_out, stalled)
            if self.estimator.reanchored:
                self._refill_until = now + REFILL_S
        needed = None
        if len(self.curve) >= 3:
            i, j = int(min(position, len(self.curve) - 2)), int(min(position + 10, len(self.curve) - 1))
            if j > i:
                needed = (self.curve[j] - self.curve[i]) * 8 / (j - i) / 1e6 * K
        self.latest = Snapshot(
            tv_state=tv.state if tv else None, tv_position=tv.position if tv else None, buffer_s=buffer_s,
            delivered_mbps=health.delivered_mbps if health else None, needed_mbps=needed,
            retx_pct=health.retx_pct if health else None, rtt_ms=health.rtt_ms if health else None,
            refilling=now < self._refill_until and not stalled and (buffer_s or 0) < 30)
        return self.latest

    # --- background thread
    def start(self) -> None:
        self._t0 = self.clock()
        self._thread = threading.Thread(target=self._run, name="castmymovie-telemetry", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            try:
                self.step()
            except Exception:  # telemetry must never take the player down
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.reader.close()
