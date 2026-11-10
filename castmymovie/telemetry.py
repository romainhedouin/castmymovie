"""Live health of a cast: what the TV says it is doing, how the link is holding up, and how many seconds the TV
has buffered ahead of what it is showing.

Nothing here can touch playback: it only reads what the cast session already knows.

Cast receivers do not report how much they have buffered, but the session serves the file itself, so it knows
exactly how far into the file the TV has downloaded. That offset maps through the movie's real bitrate profile
(`BufferEstimator.update_exact`), with no byte counting and no drift. What is left of the error is how the file
interleaves audio and video, about a second.

Cast on the fly there is no file and no bitrate profile, but the server knows which segments the TV has fetched, so
the session reports the buffer itself (`LinkSample.ahead`, in seconds) and nothing is estimated.
"""

from __future__ import annotations

import bisect
import math
import threading
import time
from dataclasses import dataclass

from . import linkstats
from .castlink import TvStatus

NATIVE_K = 1.0          # the TV downloads the file itself, byte for byte
MAX_BUFFER_S = 120.0
SMOOTHING = 0.35        # exponential moving average weight per update
JUMP_S = 15.0           # a bigger position jump than this means a seek: re-anchor
REFILL_S = 20.0         # after a re-anchor the TV is rebuilding its buffer: say so instead of crying wolf


class BufferEstimator:
    def __init__(self, curve: list, k: float = NATIVE_K):
        self.cum, self.k = curve, k
        self._last_position: float | None = None
        self._last_bytes = 0
        self.reanchored = False          # True when the last update_exact() had to start over (seek, stall, new download)
        self.value: float | None = None

    def _c_inv(self, b: float) -> float:
        i = max(min(bisect.bisect_right(self.cum, b) - 1, len(self.cum) - 2), 0)
        width = self.cum[i + 1] - self.cum[i]
        return i + ((b - self.cum[i]) / width if width else 0.0)

    def update_exact(self, position: float, fetched: int, stalled: bool = False) -> float | None:
        """Native cast: `fetched` is how many media bytes (past the MP4 header) the TV's download has reached, so the
        media time it adds up to is known outright. A seek still empties the TV's buffer: `reanchored` says so."""
        if len(self.cum) < 3 or not math.isfinite(position):
            return None
        jumped = self._last_position is not None and abs(position - self._last_position) > JUMP_S
        restarted = fetched < self._last_bytes          # a new download began further back: a seek
        self._last_position, self._last_bytes = position, fetched
        self.reanchored = self.value is not None and (jumped or restarted or stalled)
        raw = 0.0 if stalled else min(max(self._c_inv(fetched / self.k) - position, 0.0), MAX_BUFFER_S)
        smooth = self.value is not None and not self.reanchored and not stalled
        self.value = self.value + SMOOTHING * (raw - self.value) if smooth else raw
        return self.value


@dataclass(frozen=True)
class Snapshot:
    tv_state: str | None = None       # PLAYING | PAUSED | BUFFERING | IDLE, straight from the TV
    tv_position: float | None = None
    buffer_s: float | None = None     # seconds ahead of the TV's position, see module docstring
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


class SessionReader:
    """Gives Telemetry the TV status the cast already has, so that no second Cast connection is opened next to the
    one that is driving the TV."""

    def __init__(self, session):
        self.session = session

    def poll(self) -> TvStatus | None:
        return self.session.tv_status()


class Telemetry:
    """Samples once a second on a background thread so the player's keys stay responsive. `reader` gives the TV's
    status, `read_link` the link counters, both from the cast session itself."""

    INTERVAL = 1.0

    def __init__(self, reader, read_link, curve: list | None = None, clock=time.monotonic, k: float = NATIVE_K):
        self.curve, self.k = curve or [], k
        self.reader, self.read_link, self.clock = reader, read_link, clock
        self.link = linkstats.LinkMonitor()
        self.estimator = BufferEstimator(self.curve, k) if len(self.curve) >= 3 else None
        self.position = 0.0            # set by the player on every tick
        self.latest = Snapshot()
        self._refill_until = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- one sampling pass (also used directly by the tests)
    def step(self) -> Snapshot:
        now = self.clock()
        tv = self.reader.poll()
        sample = self.read_link()
        if sample:
            self.link.add(sample)
        health = self.link.health(now)

        position = self.position
        stalled = bool(tv and tv.state == "BUFFERING")
        buffer_s = None
        on_the_fly = sample is not None and sample.ahead is not None
        if on_the_fly:   # the segments the TV has fetched, counted by the server: exact, and a seek needs no re-anchoring
            buffer_s = min(max(sample.ahead, 0.0), MAX_BUFFER_S)
        elif self.estimator and sample and sample.offset is not None:
            buffer_s = self.estimator.update_exact(position, sample.offset, stalled)
            if self.estimator.reanchored:
                self._refill_until = now + REFILL_S
        needed = None
        if len(self.curve) >= 3:
            i, j = int(min(position, len(self.curve) - 2)), int(min(position + 10, len(self.curve) - 1))
            if j > i:
                needed = (self.curve[j] - self.curve[i]) * 8 / (j - i) / 1e6 * self.k
        self.latest = Snapshot(
            tv_state=tv.state if tv else None, tv_position=tv.position if tv else None, buffer_s=buffer_s,
            delivered_mbps=health.delivered_mbps if health else None, needed_mbps=needed,
            retx_pct=health.retx_pct if health else None, rtt_ms=health.rtt_ms if health else None,
            refilling=not on_the_fly and now < self._refill_until and not stalled and (buffer_s or 0) < 30)
        return self.latest

    # --- background thread
    def start(self) -> None:
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
