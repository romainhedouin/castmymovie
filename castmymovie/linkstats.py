"""How well is the stream getting from this Mac to the TV? Read from the operating system's TCP counters.

macOS only for now (`nettop`). Everything degrades to "no data" elsewhere: the player simply omits the line.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass

CONTROL_PORTS = (":8008", ":8009")  # Cast HTTP + control connections, not the video stream


@dataclass(frozen=True)
class LinkSample:
    t: float
    bytes_out: int      # bytes handed to the TV on the stream connection(s), cumulative
    retx: int           # of which retransmitted
    rtt_ms: float | None


@dataclass(frozen=True)
class LinkHealth:
    delivered_mbps: float | None   # recent average actually flowing to the TV
    retx_pct: float | None         # share of bytes that had to be re-sent
    rtt_ms: float | None


def available() -> bool:
    return shutil.which("nettop") is not None


def parse_nettop(text: str, host: str) -> tuple | None:
    """(bytes_out, retx, rtt_ms) summed over the stream connections to `host`, or None if there are none.

    `nettop -x` prints a header such as `,bytes_out,re-tx,rtt_avg,` then one CSV row per connection:
    `tcp4 192.168.1.10:49511<->192.168.1.20:46376,56819035,675044,14.56 ms,`
    """
    lines = [l for l in text.splitlines() if l.strip()]
    header = next((l.split(",") for l in lines if "bytes_out" in l and "<->" not in l), None)
    if not header:
        return None
    col = {name.strip(): i for i, name in enumerate(header)}
    if "bytes_out" not in col:
        return None
    total_bytes = total_retx = 0
    rtt, biggest = None, -1
    for line in lines:
        if "<->" not in line:
            continue
        fields = line.split(",")
        remote = fields[0].split("<->")[-1]
        if host not in remote or remote.endswith(CONTROL_PORTS):
            continue
        try:
            sent = int(fields[col["bytes_out"]] or 0)
            retx = int(fields[col["re-tx"]] or 0) if "re-tx" in col else 0
            rtt_text = fields[col["rtt_avg"]].split()[0] if "rtt_avg" in col and fields[col["rtt_avg"]].strip() else None
            rtt_ms = float(rtt_text) if rtt_text else None
        except (IndexError, ValueError):
            continue
        total_bytes += sent
        total_retx += retx
        if sent > biggest:
            biggest, rtt = sent, rtt_ms
    return (total_bytes, total_retx, rtt) if biggest >= 0 else None


def read_sample(pid: int, host: str, run=subprocess.run) -> LinkSample | None:
    if not available():
        return None
    try:
        out = run(["nettop", "-p", str(pid), "-m", "tcp", "-L", "1", "-x", "-J", "bytes_out,re-tx,rtt_avg"],
                  capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parsed = parse_nettop(out, host)
    return LinkSample(time.monotonic(), *parsed) if parsed else None


class LinkMonitor:
    """Turns raw counters into recent rates."""

    RATE_WINDOW = 30.0   # the TV pulls in bursts, so a short window would swing between 0 and a few Mbps
    RETX_WINDOW = 60.0
    MIN_RATE_SPAN = 15.0

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

    def health(self) -> LinkHealth | None:
        if not self.samples:
            return None
        if len(self.samples) < 2:
            return LinkHealth(None, None, self.samples[-1].rtt_ms)   # e.g. right after a seek replaced the connection
        old, new = self._since(self.RATE_WINDOW)
        span = new.t - old.t
        mbps = (new.bytes_out - old.bytes_out) * 8 / span / 1e6 if span >= self.MIN_RATE_SPAN else None
        old, new = self._since(self.RETX_WINDOW)
        sent = new.bytes_out - old.bytes_out
        retx_pct = (new.retx - old.retx) / sent * 100 if sent >= 200_000 else None
        return LinkHealth(mbps, retx_pct, new.rtt_ms)
