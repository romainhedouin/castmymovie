"""How well is the stream getting from this Mac to the TV?

The cast serves the file itself, so it hands over its own byte counters (`NativeCast.link_sample()`). They carry no
retransmit or round-trip figures: those fields are None and the player leaves them out. Cast on the fly, it also says
how many seconds of the movie the TV has fetched ahead of where it plays (`LinkSample.ahead`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LinkSample:
    t: float
    bytes_out: int      # bytes handed to the TV on the stream connection(s), cumulative
    retx: int | None    # of which retransmitted (None: not measurable)
    rtt_ms: float | None
    offset: int | None = None  # native cast only: media bytes (past the MP4 header) the TV's download has reached
    ahead: float | None = None  # native cast on the fly only: seconds of movie the TV has fetched past its position


@dataclass(frozen=True)
class LinkHealth:
    delivered_mbps: float | None   # recent average actually flowing to the TV
    retx_pct: float | None         # share of bytes that had to be re-sent
    rtt_ms: float | None


class LinkMonitor:
    """Turns raw counters into recent rates."""

    RATE_WINDOW = 30.0   # the TV pulls in bursts, so a short window would swing between 0 and a few Mbit/s
    RETX_WINDOW = 60.0
    MIN_RATE_SPAN = 15.0
    STALE_AFTER = 8.0    # no sample for this long (stream connection gone): the last numbers are history, not health

    def __init__(self):
        self.samples: deque = deque(maxlen=400)

    def add(self, s: LinkSample) -> None:
        if self.samples and s.bytes_out < self.samples[-1].bytes_out:
            self.samples.clear()  # the connection was replaced: counters restarted
        self.samples.append(s)

    def _since(self, seconds: float):
        newest = self.samples[-1]
        old = next((s for s in self.samples if newest.t - s.t <= seconds), self.samples[0])
        return old, newest

    def health(self, now: float | None = None) -> LinkHealth | None:
        """Recent health. Pass `now` (same clock as the samples) to drop readings that have gone stale."""
        if not self.samples:
            return None
        if now is not None and now - self.samples[-1].t > self.STALE_AFTER:
            return None
        if len(self.samples) < 2:
            return LinkHealth(None, None, self.samples[-1].rtt_ms)   # e.g. right after a seek replaced the connection
        old, new = self._since(self.RATE_WINDOW)
        span = new.t - old.t
        mbps = (new.bytes_out - old.bytes_out) * 8 / span / 1e6 if span >= self.MIN_RATE_SPAN else None
        old, new = self._since(self.RETX_WINDOW)
        sent = new.bytes_out - old.bytes_out
        measurable = new.retx is not None and old.retx is not None
        retx_pct = (new.retx - old.retx) / sent * 100 if sent >= 200_000 and measurable else None
        return LinkHealth(mbps, retx_pct, new.rtt_ms)
