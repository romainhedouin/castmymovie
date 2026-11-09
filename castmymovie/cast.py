"""Find Cast receivers on the network, the interface every cast backend offers, and the VLC backend.

There are two ways to play a file on the TV, both behind the same `CastSession` interface:

- `nativecast.NativeCast` (the default): this Mac serves the file over HTTP and tells the TV's own media
  player to play it as an ordinary, buffered movie.
- `VlcCast` (here): a headless VLC re-muxes the file into a *live* stream for the TV. We drive VLC through
  its remote-control (rc) interface on a local socket, so the wizard can show a live position bar and offer
  pause / seek / stop.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .engine import CastError

CAST_TYPE = "_googlecast._tcp.local."
VLC_MAC = "/Applications/VLC.app/Contents/MacOS/VLC"


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


# ---------------------------------------------------------------- VLC

def find_vlc() -> str | None:
    if os.path.exists(VLC_MAC):
        return VLC_MAC
    return shutil.which("vlc")


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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class PlayState:
    position: float
    length: float
    paused: bool
    buffering: bool
    alive: bool       # False once the cast is over (the movie ended, the TV stopped it, or it could not be saved)


class CastSession(Protocol):
    """What the player needs from a cast backend (`VlcCast`, `nativecast.NativeCast`).

    start() blocks until the TV is actually playing and raises engine.CastError with a message for the user
    otherwise. stop() must be safe to call at any time, more than once, and after a failed start(). The
    player calls state() a couple of times a second, so it must be cheap.

    Optional members the player and the telemetry use when a backend has them:
      proc           the process doing the streaming (VLC), whose TCP counters `nettop` can read
      notice         one short human sentence about what just happened ("TV reconnected"), shown in the panel
      failed         True once the cast ended on its own because it gave up (not because the movie finished)
      link_sample()  a linkstats.LinkSample from the backend's own byte counters (no nettop needed)
      tv_status()    a castlink.TvStatus from the backend's own Cast connection (no second connection needed)
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


def build_vlc_cmd(vlc: str, receiver: Receiver, media: Path, rc_port: int, http_port: int, start: float = 0.0) -> list:
    cmd = [vlc, "-vv", "-I", "dummy", "--extraintf", "rc", f"--rc-host=127.0.0.1:{rc_port}",
           "--sout", "#chromecast", f"--sout-chromecast-ip={receiver.host}",
           f"--sout-chromecast-http-port={http_port}", "--demux-filter=demux_chromecast",
           "--no-sub-autodetect-file"]
    if start > 0:
        cmd.append(f"--start-time={int(start)}")
    return cmd + [str(media)]


class VlcCast:
    """One cast session through VLC (a `CastSession`). VLC runs headless; we talk to it over the rc socket."""

    STALL_SECONDS = 2.5

    def __init__(self, vlc: str, receiver: Receiver, media: Path, start: float = 0.0):
        self.vlc, self.receiver, self.media, self.start_at = vlc, receiver, Path(media), start
        self.proc: subprocess.Popen | None = None
        self.awake: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._owed = False  # the last rc command has not been fully answered yet
        self.paused = False
        self.length = 0.0
        self._last_pos = -1.0
        self._last_move = time.monotonic()
        self.log_path = Path(tempfile.gettempdir()) / "castmymovie-vlc.log"
        self._logging = False

    def _see_log(self) -> str:
        return f" (see {self.log_path})" if self._logging else ""

    # --- lifecycle
    def start(self, timeout: float = 40.0) -> None:
        rc, http = free_port(), free_port()
        log = open_private_log(self.log_path)
        self._logging = log is not None  # without a log, no message should send the user to a stale one
        self.proc = subprocess.Popen(build_vlc_cmd(self.vlc, self.receiver, self.media, rc, http, self.start_at),
                                     stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        if log is not None:
            log.close()
        self.awake = keep_awake(self.proc.pid)  # the Mac must not doze off mid-movie
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise CastError(f"VLC exited unexpectedly{self._see_log()}")
            try:
                self.sock = socket.create_connection(("127.0.0.1", rc), timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            self.stop()
            raise CastError("could not reach VLC's control interface")
        self.sock.settimeout(0.4)
        self._read_prompt()  # swallow the banner
        while time.monotonic() < deadline:  # wait until the TV is actually playing
            if self.proc.poll() is not None:
                raise CastError(f"VLC stopped while starting the cast{self._see_log()}")
            t, ln, playing = self._number("get_time"), self._number("get_length"), self._number("is_playing")
            if ln:
                self.length = float(ln)
            if playing and t:
                return
            time.sleep(0.5)
        self.stop()
        raise CastError(f"{self.receiver.name} did not start playing within {int(timeout)} s")

    def stop(self) -> None:
        if self.sock:
            try:
                self.sock.sendall(b"quit\n")
            except OSError:
                pass
            if self.proc:
                try:
                    self.proc.wait(timeout=1.0)  # let VLC close the cast session cleanly if it will
                except subprocess.TimeoutExpired:
                    pass
            self.sock.close()
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()  # VLC's chromecast teardown does not always exit on quit
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        let_sleep(self.awake)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # --- rc protocol
    def _read_prompt(self, timeout: float = 1.5) -> str:
        buf, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                d = self.sock.recv(4096)
            except socket.timeout:
                if buf:
                    break
                continue
            except OSError:
                break
            if not d:
                break
            buf += d
            if buf.rstrip(b" \r\n").endswith(b">"):
                break
        return buf.decode(errors="replace")

    def _settle(self) -> None:
        """Get the rc socket back in step before a command. VLC has no request ids: a reply that comes in late would
        be read as the answer to the next command (a time taken for a length, a length for a state)."""
        if self._owed:  # the last command got no complete reply yet: let it arrive, then throw it away
            self._read_prompt()
            self._owed = False
        self.sock.settimeout(0)
        try:
            while self.sock.recv(4096):
                pass
        except OSError:  # nothing left to read (or the socket is gone: the send below reports that)
            pass
        self.sock.settimeout(0.4)

    def _cmd(self, text: str) -> str:
        if not self.sock:
            return ""
        try:
            self._settle()
            self.sock.sendall(text.encode() + b"\n")
        except OSError:
            return ""
        reply = self._read_prompt()
        self._owed = not reply.rstrip(" \r\n").endswith(">")
        return reply

    def _number(self, text: str) -> int | None:
        nums = re.findall(r"^\s*(-?\d+)\s*$", self._cmd(text).replace("\r", ""), re.M)
        return int(nums[-1]) if nums else None

    # --- controls
    def state(self) -> PlayState:
        alive = self.proc is not None and self.proc.poll() is None
        pos = self._number("get_time") if alive else None
        if self.length <= 0 and alive:
            self.length = float(self._number("get_length") or 0)
        position = float(pos) if pos is not None else max(self._last_pos, 0.0)
        now = time.monotonic()
        if position != self._last_pos:
            self._last_pos, self._last_move = position, now
        stuck = now - self._last_move > self.STALL_SECONDS
        if alive and pos is None and self._last_pos >= 0 and stuck and self._number("is_playing") == 0:
            alive = False  # VLC is up but has no input left: the movie ended or the TV stopped it
        buffering = alive and not self.paused and stuck
        return PlayState(position, self.length, self.paused, buffering, alive)

    def toggle_pause(self) -> None:
        self._cmd("pause")
        self.paused = not self.paused
        self._last_move = time.monotonic()

    def seek_to(self, seconds: float) -> None:
        top = max(self.length - 5, 0) if self.length else seconds
        self._cmd(f"seek {int(min(max(seconds, 0), top))}")
        self._last_move = time.monotonic()

    def seek_by(self, delta: float) -> None:
        self.seek_to(max(self._last_pos, 0) + delta)
