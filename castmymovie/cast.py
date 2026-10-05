"""Find Cast receivers on the network and play a file on one through VLC.

VLC does the casting: it serves the file over HTTP and the TV pulls and buffers it, which rides out
Wi-Fi hiccups far better than Chrome's live tab mirroring. We run VLC without a window and drive it
through its remote-control (rc) interface on a local socket, so the wizard can show a live
position bar and offer pause / seek / stop.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

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
    return Receiver(name=props.get("fn") or props.get("md") or host, host=host,
                    model=props.get("md", ""), port=port or 8009, uid=props.get("id", ""))


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
    try:
        ServiceBrowser(zc, CAST_TYPE, Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return sorted(found.values(), key=lambda r: r.name.lower())


# ---------------------------------------------------------------- VLC

def find_vlc() -> str | None:
    if os.path.exists(VLC_MAC):
        return VLC_MAC
    return shutil.which("vlc")


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
    alive: bool


def parse_time(text: str) -> float | None:
    """'90' -> 90, '12:30' -> 750, '1:02:03' -> 3723. Blank means the beginning. None means invalid."""
    text = text.strip()
    if not text:
        return 0.0
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip().isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return float(secs)


def build_vlc_cmd(vlc: str, receiver: Receiver, media: Path, rc_port: int, http_port: int, start: float = 0.0) -> list:
    cmd = [vlc, "-I", "dummy", "--extraintf", "rc", f"--rc-host=127.0.0.1:{rc_port}",
           "--sout", "#chromecast", f"--sout-chromecast-ip={receiver.host}",
           f"--sout-chromecast-http-port={http_port}", "--demux-filter=demux_chromecast",
           "--no-sub-autodetect-file"]
    if start > 0:
        cmd.append(f"--start-time={int(start)}")
    return cmd + [str(media)]


class VlcCast:
    """One cast session. VLC runs headless; we talk to it over the rc socket."""

    STALL_SECONDS = 2.5

    def __init__(self, vlc: str, receiver: Receiver, media: Path, start: float = 0.0):
        self.vlc, self.receiver, self.media, self.start_at = vlc, receiver, Path(media), start
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.paused = False
        self.length = 0.0
        self._last_pos = -1.0
        self._last_move = time.monotonic()
        self.log_path = Path(tempfile.gettempdir()) / "castmymovie-vlc.log"

    # --- lifecycle
    def start(self, timeout: float = 40.0) -> None:
        rc, http = free_port(), free_port()
        log = open(self.log_path, "w")
        self.proc = subprocess.Popen(build_vlc_cmd(self.vlc, self.receiver, self.media, rc, http, self.start_at),
                                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise CastError(f"VLC exited unexpectedly (see {self.log_path})")
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
                raise CastError(f"VLC stopped while starting the cast (see {self.log_path})")
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

    def _cmd(self, text: str) -> str:
        if not self.sock:
            return ""
        try:
            self.sock.sendall(text.encode() + b"\n")
        except OSError:
            return ""
        return self._read_prompt()

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
        buffering = alive and not self.paused and now - self._last_move > self.STALL_SECONDS
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
