"""Find Cast receivers on the network, and the interface a cast session offers the player.

`nativecast.NativeCast` is the session: this Mac serves the file over HTTP and tells the TV's own media player
to play it as an ordinary, buffered movie.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .engine import CastError

CAST_TYPE = "_googlecast._tcp.local."


# ---------------------------------------------------------------- receivers

@dataclass(frozen=True)
class Receiver:
    name: str
    host: str
    model: str = ""
    port: int = 8009
    uid: str = ""

    @property
    def label(self) -> str:
        extra = f"{self.model}, " if self.model else ""
        return f"{self.name}  ({extra}{self.host})"


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL, C1: ESC, BEL and the one-character CSI/OSC starters


def clean_text(value) -> str:
    """`value` as text without control characters. A name from mDNS or an error from the TV is printed to the
    terminal, which obeys ESC sequences (retitle the window, clear the screen), and Rich lets ESC through."""
    if value is None:
        return ""
    return _CONTROL.sub(lambda m: " " if m.group().isspace() else "", str(value)).strip()


# Where a TV can be. Not Python's `is_private`, which also takes 0.0.0.0/8, 240.0.0.0/4, the documentation and
# benchmarking ranges (none can be a TV; 0.0.0.0 reaches this very Mac) and has changed between versions.
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                      "169.254.0.0/16", "127.0.0.0/8"))


def lan_address(text: str) -> str | None:
    """`text` if it is the IPv4 address of a device on a home network (private, link-local or loopback), else None.

    The native cast serves the movie to this address, so a typo or a pasted public address must never get there.
    Names are refused too, since a name can point anywhere. So are leading zeros ("010.0.0.1"): the system
    resolver reads that as octal, 8.0.0.1."""
    text = text.strip()
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        return None
    if str(ip) != text or not any(ip in net for net in LAN_NETWORKS):
        return None
    return text


def receiver_from_service(props: dict, addresses: list, port: int) -> Receiver | None:
    """Build a Receiver from a Cast mDNS record. Skips audio-only devices and records without an address."""
    if not addresses:
        return None
    caps = props.get("ca")
    try:
        if caps is not None and not (int(caps) & 1):  # bit 0 = video out; speakers are not for movies
            return None
    except ValueError:
        pass
    host = next((a for a in addresses if ":" not in a), addresses[0])  # prefer IPv4
    model = clean_text(props.get("md"))
    return Receiver(name=clean_text(props.get("fn")) or model or host, host=host,
                    model=model, port=port or 8009, uid=clean_text(props.get("id")))


def discover(timeout: float = 4.0) -> list:
    """Browse mDNS for Cast devices for `timeout` seconds."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as e:  # pragma: no cover
        raise CastError("the 'zeroconf' package is missing (pip install zeroconf)") from e

    found: dict = {}

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            props = {k.decode(errors="replace"): (v.decode(errors="replace") if isinstance(v, bytes) else str(v))
                     for k, v in info.properties.items()}
            r = receiver_from_service(props, info.parsed_addresses(), info.port)
            if r:
                found[r.uid or r.host] = r

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    browser = ServiceBrowser(zc, CAST_TYPE, Listener())
    try:
        time.sleep(timeout)
    finally:
        browser.cancel()  # closing Zeroconf alone leaves the browser's thread running, one more per search
        zc.close()
    return sorted(found.values(), key=lambda r: r.name.lower())


# ---------------------------------------------------------------- helpers

def keep_awake(pid: int) -> subprocess.Popen | None:
    """Stop the Mac from idle-sleeping while process `pid` runs. Nothing a cast does counts as user activity, so
    the Mac would doze off mid-movie and the stream would stop. caffeinate ends on its own when `pid` does."""
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(pid)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None  # the Mac may doze off mid-movie, which is better than not casting at all


def let_sleep(awake: subprocess.Popen | None) -> None:
    if awake and awake.poll() is None:
        awake.terminate()
        awake.wait()


def open_private_log(path: Path):
    """Open `path` as a fresh log that only this user can read, or return None when that is not possible (a log
    must never stop a cast). The temp dir is shared: a symlink planted at `path`, or a file that someone else owns
    or has linked elsewhere, is refused instead of being truncated, filled or exposed."""
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_uid != os.geteuid():
            raise OSError("not a log of ours")
        os.fchmod(fd, 0o600)  # an older log may have been created world-readable
        os.ftruncate(fd, 0)
        return os.fdopen(fd, "w", encoding="utf-8", errors="replace")
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


@dataclass
class PlayState:
    position: float
    length: float
    paused: bool
    buffering: bool
    alive: bool       # False once the cast is over (the movie ended, the TV stopped it, or it could not be saved)


class CastSession(Protocol):
    """What the player needs from a cast backend (`nativecast.NativeCast`).

    start() blocks until the TV is actually playing and raises engine.CastError with a message for the user
    otherwise. stop() must be safe to call at any time, more than once, and after a failed start(). The
    player calls state() a couple of times a second, so it must be cheap.

    Optional members the player and the telemetry use when a backend has them:
      notice         one short human sentence about what just happened ("TV reconnected"), shown in the panel
      failed         True once the cast ended on its own because it gave up (not because the movie finished)
      link_sample()  a linkstats.LinkSample from the backend's own byte counters
      tv_status()    a castlink.TvStatus from the backend's own Cast connection
    """

    def start(self, timeout: float = 40.0) -> None: ...

    def stop(self) -> None: ...

    def state(self) -> PlayState: ...

    def toggle_pause(self) -> None: ...

    def seek_to(self, seconds: float) -> None: ...

    def seek_by(self, delta: float) -> None: ...

    def __enter__(self): ...

    def __exit__(self, *exc): ...


def parse_time(text: str) -> float | None:
    """'90' -> 90, '12:30' -> 750, '1:02:03' -> 3723. Blank means the beginning. None means invalid."""
    text = text.strip()
    if not text:
        return 0.0
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip().isdecimal() for p in parts):  # isdigit() also accepts '²', which int() rejects
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return float(secs)
