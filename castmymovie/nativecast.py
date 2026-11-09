"""Cast natively: this Mac serves the movie over HTTP and the TV's own media player plays it.

Why not VLC. VLC sends the TV a *live* stream (re-muxed on the fly), and when the TV stalls on it VLC rewinds
to where the stream began and switches to live video transcoding: on a weak Wi-Fi the picture freezes while
the sound goes on. Here the TV is handed an ordinary file (`streamType: BUFFERED`): it downloads ahead,
pauses and rebuffers both tracks together when the Wi-Fi dips, and seeks with HTTP Range requests, exactly
as it would with a movie from any web server.

How:
  1. `MediaServer` serves the prepared .cast.mp4 to the TV's address only, under a random URL.
  2. Over the Cast protocol (`castlink.CastConnection`): CONNECT, GET_STATUS, LAUNCH the Default Media
     Receiver unless it is already running, CONNECT to it, LOAD the URL.
  3. A worker thread owns the connection from then on. It answers and sends heartbeats, polls the media
     status, runs the player's commands (they are queued, never sent from the UI thread), and saves the cast
     when something goes wrong.

Recovery, which is the point of this backend:
  - The control connection drops or goes silent for 15 s: reconnect with backoff (1, 2, 4, 8... s). If the TV
    is still playing our movie, re-attach without touching playback.
  - The TV reports a playback error (IDLE + ERROR), forgets the movie, or its player is gone when we come
    back: LOAD again at the last known position. At most 3 times in 10 minutes, then give up and say where
    the movie stopped. Never from the start, never transcoded.
  - Someone else takes over (another sender, another app) or stops it on the TV: let them, end quietly.

On the fly (`stream=`): when the movie is still being converted, the server hands the TV an HLS playlist
(MPEG-TS segments, H.264 + AAC) instead of a file, and the TV's own player downloads the segments as the encoder
writes them (`onthefly.OnTheFly`). The playlist lists every segment up front, so the TV shows the full length and
seeks as usual; a segment that is not ready yet is simply awaited, and a seek far ahead restarts the encoder there.
The buffer figure is then exact (the segments the TV has fetched, ahead of where it plays), and the session ends as
failed, saying why, if the encoder dies. Everything else (recovery, the log) works as for a file.

Everything is written to $TMPDIR/castmymovie-native.log: every Cast message except heartbeats, every state
change and recovery decision, and the HTTP requests the TV made. That file is what to read when a real TV
misbehaves.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import tempfile
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .cast import PlayState, Receiver, clean_text, keep_awake, lan_address, let_sleep, open_private_log
from .castlink import CONN_NS, HEART_NS, MEDIA_NS, RECV_NS, CastConnection, Message, TvStatus, num
from .engine import LANG_ALIASES, CastError, fmt_dur
from .linkstats import LinkSample
from .mediaserver import MediaServer

if TYPE_CHECKING:
    from .onthefly import EncoderStatus, OnTheFly

APP_ID = "CC1AD845"             # Google's Default Media Receiver, built into every Cast device
IDLE_APP_ID = "E8C28D3C"        # Backdrop, the screen a Cast device shows when nothing is cast
LOG_NAME = "castmymovie-native.log"
HLS_CONTENT_TYPE = "application/x-mpegURL"
HLS_SEGMENT_FORMAT = "ts"               # LOAD's hlsSegmentFormat for an on-the-fly cast; "" leaves the field out
HLS_VIDEO_SEGMENT_FORMAT = "mpeg2_ts"   # LOAD's hlsVideoSegmentFormat; "" leaves the field out
HLS_ENV = ("CASTMYMOVIE_HLS_SEGMENT_FORMAT", "CASTMYMOVIE_HLS_VIDEO_SEGMENT_FORMAT")  # override them, even with ""
START_SEGMENTS = 2                      # segments the encoder must have written before the TV is told to play
START_TIMEOUT = 60.0                    # seconds the encoder gets to write them
MEDIA_ERRORS = ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST", "INVALID_PLAYER_STATE", "ERROR")
FIREWALL_HINT = ("Most likely macOS blocked the incoming connection: if a window asked whether Python may accept "
                 "incoming network connections, choose Allow (or allow it in System Settings > Network > Firewall "
                 "> Options). The TV and this Mac must also be on the same Wi-Fi network (not a guest network, "
                 "and no VPN on the Mac).")
TWO_LETTER = {three: two for two, three in LANG_ALIASES.items() if len(two) == 2}


def _reason(e: BaseException) -> str:
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


def _short(namespace: str) -> str:
    return namespace.rsplit(".", 1)[-1]


def _running_apps(status: dict) -> list:
    """The applications a RECEIVER_STATUS lists, less the TV's idle screen: that is no one else's app."""
    apps = (status.get("status") or {}).get("applications") or []
    return [a for a in apps if not a.get("isIdleScreen") and a.get("appId") != IDLE_APP_ID]


class SessionLog:
    """The native cast's diary, one timestamped line per event. Thread-safe, flushed line by line, overwritten
    by the next cast (like VLC's log). Only this user can read it, it stops growing at MAX_BYTES, and when it
    cannot be opened or written the cast simply runs without it."""

    MAX_LINE = 4000
    MAX_BYTES = 20_000_000    # about 5 MB per 3 hours normally; a flood of messages must not fill the disk

    def __init__(self, path: Path):
        self.path = path
        self.usable = False       # a log file could be opened (stays true after close()), so it is worth pointing at
        self._f = None
        self._size = 0
        self._hidden: list = []
        self._lock = threading.Lock()

    def hide(self, secret: str) -> None:
        """Write `secret` (the movie URL's token) as *** from now on: the log gets pasted into bug reports."""
        self._hidden.append(secret)

    def open(self) -> None:
        with self._lock:
            if self._f is None:
                self._f = open_private_log(self.path)
                self.usable, self._size = self._f is not None, 0

    def write(self, line: str) -> None:
        for secret in self._hidden:
            line = line.replace(secret, "***")
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        data = f"{stamp} {line[:self.MAX_LINE]}\n"
        with self._lock:
            if not self._f:
                return
            size = len(data.encode("utf-8", "replace"))
            full = self._size + size > self.MAX_BYTES
            if full:
                data = f"{stamp} the log is full ({self.MAX_BYTES // 1_000_000} MB): later lines are not kept\n"
            try:
                self._f.write(data)
                self._f.flush()
            except OSError:  # disk full or the file is gone: the cast goes on without its log
                full = True
            self._size += size
            if full:
                self._close()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._f:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None


@dataclass(frozen=True)
class Playback:
    """The TV's playback as last reported. The TV only speaks up on changes, so the position in between is
    extrapolated from when it was read."""
    state: str = "IDLE"            # IDLE | BUFFERING | PLAYING | PAUSED (as the TV says it)
    position: float = 0.0
    at: float = 0.0                # time.monotonic() when `position` was true
    rate: float = 1.0
    idle_reason: str | None = None

    def position_at(self, now: float) -> float:
        if self.state == "PLAYING":
            return self.position + max(now - self.at, 0.0) * self.rate
        return self.position


class _LinkLost(Exception):
    """The control connection broke or went silent: reconnect and carry on."""


class _Ended(Exception):
    """The cast is over, or never got going. `message` is for the user. `stop_app` says whether the TV's player is
    still ours to close (it is not when someone else took over). `failed` says we gave up (the TV kept failing or
    was lost), as opposed to the movie finishing or someone else ending the cast."""

    def __init__(self, message: str, stop_app: bool = False, failed: bool = False):
        super().__init__(message)
        self.message, self.stop_app, self.failed = message, stop_app, failed


class _Quit(Exception):
    """stop() was called."""


class NativeCast:
    """One native cast session: a `cast.CastSession` that also offers `notice`, `link_sample()` and `tv_status()`.

    `subtitles` are .vtt files to offer the TV; the first one is switched on. `duration` (seconds) lets the TV
    show the length before it has read the file. `clock` only drives the reload limit, so tests can age it.
    With a `stream` the movie is cast while it is being converted: `media` is then only the source file (for the
    title) and the session owns the stream from start(): stop() closes it."""

    PING_EVERY = 5.0
    POLL_EVERY = 1.5          # the TV only reports changes, so ask; this also proves the link is alive
    MIN_POLL_GAP = 0.5        # whatever the TV answers, never poll sooner than this after the last poll
    MAX_REFUSALS = 3          # INVALID_* replies in a row, with no media status between, before a reload
    DEAD_AFTER = 15.0         # nothing at all from the TV for this long: the connection is gone
    CONNECT_TIMEOUT = 5.0
    REPLY_TIMEOUT = 10.0
    LAUNCH_TIMEOUT = 25.0
    BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    GIVE_UP_AFTER = 300.0     # seconds without the TV before the session is declared lost
    MAX_RELOADS = 3
    RELOAD_WINDOW = 600.0
    RELOAD_GAP = 2.0          # a refused reload is not retried in a tight loop
    EARLY_FINISH = 30.0       # "finished" this long before the end is a broken download, not the end credits
    NOTICE_SECONDS = 15.0

    def __init__(self, receiver: Receiver, media: Path, start: float = 0.0, subtitles=(), tls: bool = True,
                 duration: float = 0.0, clock=time.monotonic, stream: OnTheFly | None = None):
        self.receiver, self.media, self.start_at = receiver, Path(media), float(start)
        self.stream = stream                   # the on-the-fly encoder when the movie is not a finished file
        self.subtitles = [Path(s) for s in subtitles]
        self.tls, self.duration, self.clock = tls, float(duration or 0.0), clock
        self.proc = None                       # no process to measure: link_sample() and tv_status() instead
        self.sender = f"sender-castmymovie-{secrets.token_hex(4)}"
        self.log_path = Path(tempfile.gettempdir()) / LOG_NAME
        self.log = SessionLog(self.log_path)
        self.server: MediaServer | None = None
        self.awake = None
        self._tv_ip = receiver.host
        self._lock = threading.RLock()         # guards what the UI thread reads: playback, notice, alive
        now = time.monotonic()
        self._pb = Playback("BUFFERING", self.start_at, now)
        self._resume = Playback("PAUSED", self.start_at, now)  # where a reload would start: last trusted position
        self._alive, self._stopped = True, False
        self.failed = False                    # set once the cast gave up on its own (see cast.CastSession)
        self._notice, self._notice_until = "", None
        self._failure: str | None = None
        self._started, self._quit, self._event = threading.Event(), threading.Event(), threading.Event()
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        # owned by the worker thread
        self._conn: CastConnection | None = None
        self._ever_connected = self._attached = self._loaded_once = self._settled = False
        self._owner = True                     # False once someone else has the TV: never stop their player
        self._phase = "connecting"             # connecting | launching | loading | playing, for error messages
        self._transport: str | None = None
        self._app_session: str | None = None
        self._media_id = None
        self._stale: set = set()               # media sessions we replaced: their last words are not about us
        self._load_rid: int | None = None      # our LOAD until the TV plays it or refuses it
        self._load_position = self.start_at
        self._seek_later: float | None = None  # a SEEK asked while a LOAD is still starting: sent once the TV plays
        self._ignore_below = 0                 # status replies older than our last command would undo it
        self._reloads: deque = deque()
        self._reload_reason: str | None = None
        self._last_reload_at = -1e9
        self._recovering: str | None = None
        self._last_ping = self._last_poll = 0.0
        self._refusals = 0
        self._url = ""
        self._tv_state = "?"                   # the last state the TV itself reported, for the log

    # ------------------------------------------------------------ the session interface
    def start(self, timeout: float = 40.0) -> None:
        self.log.open()
        if self.stream is None:
            what = f"{self.media.stat().st_size if self.media.exists() else -1} bytes"
        else:
            what = "converted on the fly"
        self.log.write(f"castmymovie {__version__} native cast of {self.media} ({what}, "
                       f"{fmt_dur(self.duration)}) to {self.receiver.name} at {self.receiver.host}:{self.receiver.port}, "
                       f"start at {fmt_dur(self.start_at)}, sender id {self.sender}, tls={self.tls}")
        try:
            self._tv_ip = socket.gethostbyname(self.receiver.host)
            if lan_address(self._tv_ip) is None:  # never serve the movie to a stranger, whatever the receiver says
                self.stop()
                named = self._tv_ip if self._tv_ip == self.receiver.host else f"{self.receiver.host} ({self._tv_ip})"
                raise CastError(f"{named} is not an address on your home network, so the movie is not served to it. "
                                "Choose the TV again, or type its own address.")
            if self.stream is not None:  # only now: never convert for a TV that would not be served
                self._start_stream()
            extra = {} if self.stream is None else {"stream": self.stream}
            self.server = MediaServer(self.media, self.subtitles, allow={self._tv_ip}, log=self.log.write, **extra)
            self.log.hide(self.server.token)
            self.server.start()
        except OSError as e:
            self.stop()
            raise CastError(f"Could not get ready to serve the movie to {self.receiver.host} ({_reason(e)}).")
        where = ("the playlist and segments of the conversion" if self.stream is not None
                 else f"movie data starts at byte {self.server.payload_start}")
        self.log.write(f"HTTP listening on port {self.server.port} for {self._tv_ip} only; {where}")
        self.awake = keep_awake(os.getpid())  # the Mac must not doze off while it serves the movie
        self._thread = threading.Thread(target=self._run, name="castmymovie-native", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while (not self._started.is_set() and self._failure is None and self._thread.is_alive()
               and time.monotonic() < deadline):
            self._event.wait(0.1)
        if self._started.is_set() and self._failure is None:
            return
        message = self._failure or self._timeout_message(timeout)
        self.log.write(f"START FAILED: {message}")
        self.stop()
        raise CastError(message + (f"\n\nThe details are in {self.log_path}" if self.log.usable else ""))

    def _start_stream(self) -> None:
        """Start the encoder and wait for the first segments. A CastError (it died, or was too slow) ends the start."""
        try:
            self.stream.begin(self.start_at)
            self.stream.wait_ready(START_SEGMENTS, START_TIMEOUT)
        except CastError as e:
            self.log.write(f"START FAILED: {e}")
            self.stop()
            raise
        except OSError as e:
            self.log.write(f"START FAILED: could not start the converter ({_reason(e)})")
            self.stop()
            raise CastError(f"Could not start the converter ({_reason(e)}).")
        self.log.write(f"the encoder has written its first {START_SEGMENTS} segments")

    def encoder_status(self) -> EncoderStatus | None:
        """How the on-the-fly encoder is doing (for the player panel); None when the movie is a finished file."""
        return self.stream.status() if self.stream is not None else None

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._quit.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)  # the worker closes the TV's player (when it is ours) on its way out
        if self.server:
            self.server.stop()
            self.log.write(f"HTTP served {self.server.bytes_served} bytes in {self.server.media_requests} movie requests")
        if self.stream is not None:  # after the server: no request is left waiting for a segment
            try:
                st = self.stream.status()
                self.log.write(f"ENCODER stopped at {fmt_dur(st.frontier_s)}"
                               + (f", {st.speed:.1f}x" if st.speed else "") + (", too slow" if st.slow else ""))
            except Exception as e:  # noqa: BLE001 - closing the stream matters more than saying how it did
                self.log.write(f"ENCODER status unavailable ({e})")
            self.stream.close()
        let_sleep(self.awake)
        self.awake = None
        with self._lock:
            self._alive = False
        self.log.write("STOPPED")
        self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def state(self) -> PlayState:
        with self._lock:
            pb, alive = self._pb, self._alive and not self._stopped
        pos = max(pb.position_at(time.monotonic()), 0.0)
        if self.duration:
            pos = min(pos, self.duration)
        return PlayState(pos, self.duration, pb.state == "PAUSED", pb.state not in ("PLAYING", "PAUSED"), alive)

    def toggle_pause(self) -> None:
        with self._lock:
            pb, now = self._pb, time.monotonic()
            kind = "PLAY" if pb.state == "PAUSED" else "PAUSE"
            # shown at once; the TV's confirmation follows within a poll
            self._pb = replace(pb, state="PLAYING" if kind == "PLAY" else "PAUSED", position=pb.position_at(now), at=now)
        self._command(kind)

    def seek_to(self, seconds: float) -> None:
        top = max(self.duration - 5, 0) if self.duration else max(seconds, 0)
        target = float(min(max(seconds, 0), top))
        with self._lock:
            self._pb = replace(self._pb, position=target, at=time.monotonic())
        self._command("SEEK", currentTime=round(target, 3))  # no resumeState: the TV keeps playing or paused

    def seek_by(self, delta: float) -> None:
        self.seek_to(self.state().position + delta)

    # --- optional members (see cast.CastSession)
    @property
    def notice(self) -> str:
        with self._lock:
            if self._notice_until is not None and time.monotonic() > self._notice_until:
                return ""
            return self._notice

    def tv_status(self) -> TvStatus | None:
        with self._lock:
            pb = self._pb
        return TvStatus(pb.position_at(time.monotonic()), pb.state, self.duration or None)

    def link_sample(self) -> LinkSample | None:
        srv = self.server
        if srv is None:
            return None
        if self.stream is not None:  # the segments the TV has fetched, ahead of where it plays
            now = time.monotonic()
            with self._lock:
                pos = self._pb.position_at(now)
            return LinkSample(now, srv.bytes_served, None, None, ahead=max(srv.buffered_until(pos) - pos, 0.0))
        offset = None
        if srv.reach is not None and srv.payload_start is not None:
            offset = max(srv.reach - srv.payload_start, 0)
        return LinkSample(time.monotonic(), srv.bytes_served, None, None, offset)

    # ------------------------------------------------------------ helpers for the UI side
    def _command(self, kind: str, **extra) -> None:
        if self._alive and not self._stopped:
            self._commands.put((kind, extra))

    def _say(self, text: str, seconds: float | None = None) -> None:
        with self._lock:
            self._notice = text
            self._notice_until = None if seconds is None else time.monotonic() + seconds
        self.log.write(f"NOTICE {text}")

    def _timeout_message(self, timeout: float) -> str:
        name = self.receiver.name
        if self._phase in ("connecting", "launching"):
            return f"{name} did not open its Cast media player within {int(timeout)} s."
        srv = self.server
        if srv is None or srv.media_requests == 0:
            return f"{name} took the movie but never came to download it from this Mac. {self._firewall_hint()}"
        return (f"{name} started downloading the movie but did not start playing within {int(timeout)} s. The Wi-Fi "
                "to the TV may be too slow right now: try again, or pick a lower quality.")

    def _load_failure_message(self, detail: str) -> str:
        name = self.receiver.name
        if self.server is None or self.server.media_requests == 0:
            return f"{name} could not load the movie ({detail}) and never downloaded any of it. {self._firewall_hint()}"
        return (f"{name} could not play this movie ({detail}). It did download part of it, so the network is fine: "
                "the TV probably cannot decode this video or audio format. Try casting it with VLC instead.")

    def _firewall_hint(self) -> str:
        refused = sorted(self.server.refused) if self.server else []
        extra = ""
        if refused:
            extra = (f" This Mac turned away {', '.join(refused)}, because it only serves the TV's own address "
                     f"({self._tv_ip}): if that is the TV, enter that address instead.")
        return FIREWALL_HINT + extra

    # ------------------------------------------------------------ the worker thread
    def _run(self) -> None:
        lost_since, attempt = None, 0
        try:
            while True:
                try:
                    self._connect()
                    self._attach()
                    if lost_since is not None:
                        self._say("TV reconnected", self.NOTICE_SECONDS)
                    lost_since, attempt = None, 0
                    self._pump()
                except _LinkLost as e:
                    self._hang_up(f"LINK LOST: {e}")
                    now = time.monotonic()
                    lost_since = now if lost_since is None else lost_since
                    if self._started.is_set():
                        if now - lost_since > self.GIVE_UP_AFTER:
                            raise _Ended(f"Lost the TV for {int(self.GIVE_UP_AFTER / 60)} minutes and gave up. The movie "
                                         f"stopped at about {fmt_dur(self._resume.position)}.", failed=True)
                        self._say("Lost the connection to the TV, reconnecting…")
                    wait = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
                    attempt += 1
                    self.log.write(f"RECONNECT in {wait:g} s (attempt {attempt})")
                    if self._quit.wait(wait):
                        return
        except _Quit:
            self._goodbye()
        except _Ended as e:
            self._finish(e)
        except Exception as e:  # a bug must end the cast cleanly, not leave the player spinning
            self.log.write("BUG " + traceback.format_exc().replace("\n", " | "))
            self._finish(_Ended(f"castmymovie hit an internal error ({e}).", stop_app=False, failed=True))
        finally:
            self._hang_up(None)
            self._event.set()

    def _finish(self, e: _Ended) -> None:
        self.log.write(f"END: {e.message}")
        if e.stop_app:
            self._goodbye()
        else:
            self._owner = False
        with self._lock:
            if self._started.is_set():
                self._alive, self._notice, self._notice_until, self.failed = False, e.message, None, e.failed
            else:
                self._failure = e.message
        self._event.set()

    # --- connection plumbing
    def _connect(self) -> None:
        self._attached = False
        conn = CastConnection(self.receiver.host, self.receiver.port, self.tls, self.sender)
        try:
            conn.open(self.CONNECT_TIMEOUT)
        except OSError as e:
            if not self._ever_connected:
                raise _Ended(f"Could not reach {self.receiver.name} at {self.receiver.host}:{self.receiver.port} "
                             f"({_reason(e)}). Check that the TV is on and on the same Wi-Fi as this Mac.")
            raise _LinkLost(f"could not connect: {_reason(e)}")
        self._conn, self._ever_connected = conn, True
        self._last_ping = self._last_poll = time.monotonic()
        self._ignore_below, self._load_rid = 0, None  # request ids start again at 1 on every connection
        self.log.write(f"CONNECTED to {self.receiver.host}:{self.receiver.port}")
        self._send("receiver-0", CONN_NS, {"type": "CONNECT"})

    def _hang_up(self, why: str | None) -> None:
        if why:
            self.log.write(why)
        if self._conn is not None:
            self._conn.close()
        self._attached = False
        self._freeze_resume()

    def _send(self, dest: str, namespace: str, payload: dict) -> int:
        if self._conn is None:
            raise _LinkLost("not connected")
        try:
            rid = self._conn.send(dest, namespace, payload)
        except OSError as e:
            raise _LinkLost(f"send failed: {_reason(e)}")
        if namespace != HEART_NS:
            self.log.write(f"TX {dest} {_short(namespace)} {json.dumps(dict(payload, requestId=rid), separators=(',', ':'))}")
        return rid

    def _receive(self, seconds: float) -> list:
        try:
            msgs = self._conn.read(seconds, until_first=True) if self._conn else []
        except OSError as e:
            raise _LinkLost(f"read failed: {_reason(e)}")
        for m in msgs:
            if m.namespace != HEART_NS:
                self.log.write(f"RX {m.source} {_short(m.namespace)} {json.dumps(m.data, separators=(',', ':'))}")
        return msgs

    def _step(self, seconds: float = 0.2) -> list:
        """One beat of the worker: read, react, keep the connection alive. Returns what arrived."""
        if self._quit.is_set():
            raise _Quit()
        if self.stream is not None and self.stream.error:  # waiting requests fail fast, so the TV would only stall
            raise _Ended(f"The conversion stopped ({clean_text(self.stream.error)}). The movie stopped at "
                         f"about {fmt_dur(self._freeze_resume())}.", stop_app=True, failed=True)
        msgs = self._receive(seconds)
        for m in msgs:
            self._handle(m)
        if self._conn is None or not self._conn.connected:
            raise _LinkLost("the TV closed the connection")
        now = time.monotonic()
        if now - self._conn.last_rx > self.DEAD_AFTER:
            raise _LinkLost(f"nothing from the TV for {self.DEAD_AFTER:g} s")
        if now - self._last_ping >= self.PING_EVERY:
            self._last_ping = now
            self._send("receiver-0", HEART_NS, {"type": "PING"})
        return msgs

    def _wait(self, wanted, timeout: float) -> Message | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self._step():
                if wanted(m):
                    return m
        return None

    # --- getting the movie on screen
    def _attach(self) -> None:
        """Find (or open) the TV's media player, then LOAD the movie, or pick up the one already playing."""
        self._phase = "launching" if not self._started.is_set() else self._phase
        while not self._commands.empty():  # keys pressed while we were away are stale now
            self._commands.get_nowait()
        rid = self._send("receiver-0", RECV_NS, {"type": "GET_STATUS"})
        reply = self._wait(lambda m: m.type == "RECEIVER_STATUS", self.REPLY_TIMEOUT)
        if reply is None:
            raise _LinkLost(f"no answer to GET_STATUS #{rid}")
        apps = _running_apps(reply.data)
        app = next((a for a in apps if a.get("appId") == APP_ID and a.get("transportId")), None)
        if app is None and self._loaded_once and apps:
            raise _Ended(f"Another app is on the TV now. The movie stopped at about {fmt_dur(self._resume.position)}.")
        relaunched = app is None
        if relaunched:
            app = self._launch()
        instance = app["transportId"], app.get("sessionId")
        if relaunched or instance != (self._transport, self._app_session):
            self._media_id, self._stale = None, set()  # a new app numbers its media sessions from 1 again
        self._transport, self._app_session = instance
        self.log.write(f"APP {'launched' if relaunched else 'found'}: transport {self._transport}, session {self._app_session}")
        self._send(self._transport, CONN_NS, {"type": "CONNECT"})
        self._attached = True
        if not self._loaded_once:
            self._load(self.start_at)
            self._loaded_once = True  # only once it went out: if it could not, the next attach does it again
            return
        if relaunched:  # the player had gone while we were away
            self._want_reload("the TV's player had closed while the connection was down")
            return
        rid = self._send(self._transport, MEDIA_NS, {"type": "GET_STATUS"})
        reply = self._wait(lambda m: m.type == "MEDIA_STATUS" and m.data.get("requestId") == rid, self.REPLY_TIMEOUT)
        if reply is None:
            raise _LinkLost(f"no answer to media GET_STATUS #{rid}")
        if self._reload_reason is None:
            self.log.write(f"RE-ATTACHED to media session {self._media_id} without reloading")

    def _launch(self) -> dict:
        self._send("receiver-0", RECV_NS, {"type": "LAUNCH", "appId": APP_ID})
        deadline = time.monotonic() + self.LAUNCH_TIMEOUT
        while time.monotonic() < deadline:
            for m in self._step():
                if m.type == "LAUNCH_ERROR":
                    raise _Ended(f"{self.receiver.name} refused to open its Cast media player "
                                 f"({clean_text(m.data.get('reason')) or 'no reason given'}).")
                if m.type != "RECEIVER_STATUS":
                    continue
                apps = _running_apps(m.data)
                app = next((a for a in apps if a.get("appId") == APP_ID and a.get("transportId")), None)
                if app:
                    return app
        raise _Ended(f"{self.receiver.name} did not open its Cast media player within {int(self.LAUNCH_TIMEOUT)} s.")

    def _local_ip(self) -> str:
        """This Mac's address as the TV sees it: the one the OS would route to the TV from (no packet is sent)."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self._tv_ip, self.receiver.port))
            return s.getsockname()[0]

    def _load(self, position: float) -> None:
        try:
            host = self._local_ip()  # asked again for every LOAD: the Mac may have rejoined the Wi-Fi with a new address
        except OSError as e:  # its Wi-Fi is mid-rejoin: no route to the TV right now
            raise _LinkLost(f"no route to the TV: {_reason(e)}")
        streaming = self.stream is not None
        self._url = self.server.stream_url(host) if streaming else self.server.url(host)
        media = {"contentId": self._url, "contentUrl": self._url,
                 "contentType": HLS_CONTENT_TYPE if streaming else "video/mp4", "streamType": "BUFFERED",
                 "metadata": {"metadataType": 1, "title": self.media.name}}
        if streaming:
            for field, value, env in (("hlsSegmentFormat", HLS_SEGMENT_FORMAT, HLS_ENV[0]),
                                      ("hlsVideoSegmentFormat", HLS_VIDEO_SEGMENT_FORMAT, HLS_ENV[1])):
                value = os.environ.get(env, value)  # for experiments on the TV without editing the code
                if value:
                    media[field] = value
        if self.duration:
            media["duration"] = self.duration
        payload = {"type": "LOAD", "media": media, "currentTime": round(position, 3), "autoplay": True}
        if self._app_session:
            payload["sessionId"] = self._app_session
        if self.subtitles:
            media["tracks"] = [self._track(i, sub, path, host)
                               for i, (sub, path) in enumerate(zip(self.subtitles, self.server.subtitle_paths), 1)]
            payload["activeTrackIds"] = [1]
        if self._media_id is not None:
            self._stale.add(self._media_id)
        self._media_id, self._settled, self._load_position = None, False, position
        self._seek_later = None                # `position` already includes any seek asked before this LOAD
        self._refusals = 0
        with self._lock:
            self._pb = Playback("BUFFERING", position, time.monotonic())
        self._phase = "loading" if not self._started.is_set() else self._phase
        self._load_rid = self._send(self._transport, MEDIA_NS, payload)
        self._ignore_below = self._load_rid
        self._last_reload_at = time.monotonic()

    def _track(self, track_id: int, sub: Path, path: str, host: str) -> dict:
        code = sub.stem.rsplit(".", 1)[-1] if "." in sub.stem else "und"  # movie.cast.eng.vtt -> eng
        track = {"trackId": track_id, "type": "TEXT", "subtype": "SUBTITLES", "name": code,
                 "trackContentId": self.server.url(host, path), "trackContentType": "text/vtt"}
        lang = TWO_LETTER.get(code.rstrip("0123456789"))
        if lang:
            track["language"] = lang
        return track

    # --- the steady state
    def _pump(self) -> None:
        while True:
            self._step()
            self._run_commands()
            now = time.monotonic()
            if self._reload_reason and now - self._last_reload_at >= self.RELOAD_GAP:
                self._reload()
            if self._transport and now - self._last_poll >= self.POLL_EVERY:
                self._last_poll = now
                payload = {"type": "GET_STATUS"}
                if self._media_id is not None:
                    payload["mediaSessionId"] = self._media_id
                self._send(self._transport, MEDIA_NS, payload)

    def _run_commands(self) -> None:
        while True:
            try:
                kind, extra = self._commands.get_nowait()
            except queue.Empty:
                return
            if kind == "SEEK":
                where = extra["currentTime"]
                self._resume = Playback("PAUSED", where, time.monotonic())  # a reload starts where the user went
                if self._reload_reason is not None:  # the old session is going: the coming reload starts at `where`
                    self.log.write(f"SEEK to {fmt_dur(where)} goes into the coming reload instead of the old session")
                    continue
                if not self._settled:  # a LOAD is still starting: the TV has no session to seek in yet
                    self._seek_later = self._load_position = where
                    self.log.write(f"SEEK to {fmt_dur(where)} waits until the TV plays the movie")
                    continue
            if self._media_id is None or not self._transport:
                self.log.write(f"DROPPED {kind}: the TV has no media session for us right now")
                continue
            self._ignore_below = self._send(self._transport, MEDIA_NS,
                                            dict({"type": kind, "mediaSessionId": self._media_id}, **extra))

    def _goodbye(self) -> None:
        """Close the TV's player if it is ours, so the TV leaves the Cast screen. Best effort: we are leaving anyway."""
        conn = self._conn
        if not self._owner or conn is None or not conn.connected:
            return
        try:
            if self._media_id is not None and self._transport:
                self._send(self._transport, MEDIA_NS, {"type": "STOP", "mediaSessionId": self._media_id})
            if self._app_session:
                rid = self._send("receiver-0", RECV_NS, {"type": "STOP", "sessionId": self._app_session})
                end = time.monotonic() + 1.0  # let the TV take it in (and log what it says) before hanging up
                while time.monotonic() < end and conn.connected:
                    if any(m.data.get("requestId") == rid for m in self._receive(0.2)):
                        break
        except _LinkLost:
            pass

    # --- what the TV tells us
    def _handle(self, m: Message) -> None:
        kind = m.type
        if m.namespace == HEART_NS:
            if kind == "PING":
                self._send(m.source or "receiver-0", HEART_NS, {"type": "PONG"})
        elif m.namespace == CONN_NS and kind == "CLOSE":
            if m.source in ("receiver-0", ""):
                raise _LinkLost("the TV closed the connection")
            self.log.write("the TV's player closed our channel: asking the TV what is running")
            self._send("receiver-0", RECV_NS, {"type": "GET_STATUS"})
        elif m.namespace == RECV_NS and kind == "RECEIVER_STATUS":
            self._on_receiver_status(m.data)
        elif m.namespace == MEDIA_NS and kind == "MEDIA_STATUS":
            self._on_media_status(m.data)
        elif m.namespace == MEDIA_NS and kind in MEDIA_ERRORS:
            self._on_media_error(m.data)

    def _on_receiver_status(self, data: dict) -> None:
        if not self._attached:
            return  # while attaching, _attach and _launch read the app list themselves
        apps = _running_apps(data)
        if any(a.get("transportId") == self._transport or (self._app_session and a.get("sessionId") == self._app_session)
               for a in apps):
            return
        where = fmt_dur(self._freeze_resume())
        if apps:
            raise _Ended(f"Another app took over the TV. The movie stopped at {where}.")
        long_pause = " after a long pause" if self._pb.state == "PAUSED" else ""  # receivers close idle players
        raise _Ended(f"The TV closed the player{long_pause} at {where}. Cast again and start there to carry on.")

    def _whose(self, entry: dict) -> bool | None:
        """True if a status entry is about our movie, False if about another, None if it does not say (the TV
        often leaves `media` out of status updates)."""
        media = entry.get("media") or {}
        url = media.get("contentUrl") or media.get("contentId")
        if not url:
            return None
        return self.server is not None and self.server.token in url

    def _pick(self, entries: list, rid: int) -> dict | None:
        """The status entry about our movie, if any. Raises when the entries say someone else has the TV."""
        for e in entries:
            if self._media_id is not None and e.get("mediaSessionId") == self._media_id and self._whose(e) is not False:
                return e
        loading = self._load_rid is not None and rid != self._load_rid
        for e in entries:
            if e.get("mediaSessionId") in self._stale:
                continue
            whose = self._whose(e)
            ended = e.get("playerState") == "IDLE" and bool(e.get("idleReason"))
            if loading and (ended or whose is False):
                continue  # the last words of whatever our LOAD is replacing
            # a replaced session's goodbye carries our LOAD's request id but no media; only ERROR is our own load failing
            replaced = ended and whose is None and e.get("idleReason") != "ERROR"
            if self._media_id is None and whose is not False and not replaced:
                return e  # the answer to our LOAD
            if whose is True and not ended:  # our movie under a new session id: follow it
                self.log.write(f"MEDIA session {self._media_id} is now {e.get('mediaSessionId')}")
                self._media_id = e.get("mediaSessionId")
                return e
            if whose is False and not ended:
                self._owner = False
                raise _Ended(f"Another device started casting to the TV. The movie stopped at "
                             f"{fmt_dur(self._freeze_resume())}.")
        return None

    def _on_media_status(self, data: dict) -> None:
        self._refusals = 0
        rid = int(num(data.get("requestId"), 0, hi=2 ** 31))
        if (rid and rid < self._ignore_below) or not self._commands.empty():
            return  # said before the user's latest command reached the TV: it would undo what they just did
        entries = data.get("status") or []
        if not entries:
            if self._loaded_once and self._load_rid is None and self._attached:
                self._freeze_resume()
                self._want_reload("the TV no longer has the movie")
            return
        entry = self._pick(entries, rid)
        if entry is None:
            return
        if self._media_id is None and entry.get("mediaSessionId") is not None:
            self._media_id = entry["mediaSessionId"]
            self.log.write(f"MEDIA session {self._media_id}")
        media = entry.get("media") or {}
        if not self.duration:
            self.duration = num(media.get("duration"), 0.0)
        self._apply(entry)

    def _apply(self, entry: dict) -> None:
        now = time.monotonic()
        with self._lock:
            old = self._pb
            state = entry.get("playerState") or self._tv_state
            cur = entry.get("currentTime")
            position = num(cur, old.position_at(now))
            if not self._settled and state not in ("PLAYING", "PAUSED"):
                position = self._load_position  # some TVs report 0 until they have seeked to where we asked
            rate = num(entry.get("playbackRate"), old.rate, hi=8.0) or 1.0
            self._pb = Playback(state, position, now, rate, entry.get("idleReason"))
        if state != self._tv_state:
            self.log.write(f"STATE {self._tv_state} -> {state} at {fmt_dur(position)} ({position:.1f} s)"
                           + (f" reason {entry['idleReason']}" if entry.get("idleReason") else ""))
            self._tv_state = state
        if state in ("PLAYING", "PAUSED"):
            self._settled, self._load_rid = True, None  # the LOAD is done (until then a LOAD_FAILED may still come)
            if self._seek_later is not None:
                self._command("SEEK", currentTime=round(self._seek_later, 3))
                self._seek_later = None
        if self._settled:
            if state in ("PLAYING", "PAUSED"):
                self._resume = self._pb
            elif state == "BUFFERING":
                self._freeze_resume()  # a stalling TV may say 0 or stale times: keep the last good position, stopped
        if state == "PLAYING":
            if not self._started.is_set():
                self._phase = "playing"
                self._started.set()
                self._event.set()
                self.log.write("STARTED")
            if self._recovering:
                self._say(self._recovering, self.NOTICE_SECONDS)
                self._recovering = None
        if state == "IDLE" and entry.get("idleReason"):
            self._on_idle(entry["idleReason"], entry)

    def _on_idle(self, reason: str, entry: dict) -> None:
        reason = clean_text(reason)
        where = self._freeze_resume()
        if reason == "FINISHED":
            if self.duration and where < self.duration - self.EARLY_FINISH:
                self._want_reload(f"the TV said the movie finished at {fmt_dur(where)}, long before the end")
                return
            raise _Ended("The movie finished.", stop_app=True)
        if reason == "CANCELLED":
            raise _Ended(f"Stopped on the TV at {fmt_dur(where)}.")
        if reason == "INTERRUPTED":
            self._owner = False
            raise _Ended(f"Another device started casting to the TV. The movie stopped at {fmt_dur(where)}.")
        detail = clean_text(entry.get("detailedErrorCode") or entry.get("reason"))  # extendedStatus is an object
        if not self._started.is_set():
            raise _Ended(self._load_failure_message(f"the TV reported {reason}{f' {detail}' if detail else ''}"),
                         stop_app=True)
        self._want_reload(f"the TV reported a playback {reason}")

    def _on_media_error(self, data: dict) -> None:
        kind, rid = data.get("type"), data.get("requestId") or 0
        detail = clean_text(data.get("detailedErrorCode") or data.get("reason"))
        what = f"{kind}{f' {detail}' if detail else ''}"
        if kind == "LOAD_CANCELLED" and self._load_rid is not None and rid in (0, self._load_rid):
            where = fmt_dur(self._freeze_resume())  # our own earlier LOADs have older ids: someone else did this
            if not self._started.is_set():
                raise _Ended("Another device started casting to the TV at the same moment, so the movie did not start.")
            raise _Ended(f"Another device started casting to the TV. The movie stopped at {where}.")
        # a TV may report a failed LOAD without saying which (requestId 0); only an older id is not about this LOAD
        loading = self._load_rid is not None
        if loading and (rid == self._load_rid or (kind == "LOAD_FAILED" and not 0 < rid < self._load_rid)):
            self._load_rid = None
            if not self._started.is_set():
                raise _Ended(self._load_failure_message(what), stop_app=True)
            self._want_reload(f"the TV refused the reload ({what})")
        elif kind in ("INVALID_REQUEST", "INVALID_PLAYER_STATE"):
            self._refused(rid, what)

    def _refused(self, rid: int, what: str) -> None:
        """The TV refused a request, most likely because the media session we hold is gone or renumbered. Forget the
        id, so the next poll lists every session (none left: reload; one of ours: follow it)."""
        if rid and rid < self._ignore_below:
            return  # about something asked before our last command or LOAD
        self._refusals += 1
        if self._refusals >= self.MAX_REFUSALS:
            self._want_reload(f"the TV keeps refusing our requests ({what})")
        if self._media_id is not None:
            self.log.write(f"MEDIA session {self._media_id} refused by the TV ({what}): asking for all sessions")
            self._media_id = None
            # soon, not at once: a TV that refuses this poll too must not be asked in a tight loop
            self._last_poll = min(self._last_poll, time.monotonic() - self.POLL_EVERY + self.MIN_POLL_GAP)

    # --- recovery
    def _freeze_resume(self) -> float:
        """Pin the resume point where the movie is now, so it does not keep running on in our heads."""
        now = time.monotonic()
        where = self._resume.position_at(now)
        self._resume = Playback("PAUSED", where, now)
        return where

    def _want_reload(self, reason: str) -> None:
        if self._reload_reason is None:
            self.log.write(f"RELOAD wanted: {reason}")
            self._reload_reason = reason

    def _reload(self) -> None:
        reason, self._reload_reason = self._reload_reason, None
        now = self.clock()
        while self._reloads and now - self._reloads[0] > self.RELOAD_WINDOW:
            self._reloads.popleft()
        where = self._freeze_resume()
        if len(self._reloads) >= self.MAX_RELOADS:
            raise _Ended(f"The TV kept failing ({reason}), so I gave up after {self.MAX_RELOADS} reloads in "
                         f"{int(self.RELOAD_WINDOW / 60)} minutes. It stopped at {fmt_dur(where)}: cast again and "
                         "start there.", stop_app=True, failed=True)
        self._reloads.append(now)
        self.log.write(f"RELOAD {len(self._reloads)}/{self.MAX_RELOADS} at {fmt_dur(where)} ({where:.1f} s) "
                       f"because {reason}")
        self._say(f"Reloading after a TV error at {fmt_dur(where)}…")
        self._recovering = f"Reloaded after a TV error at {fmt_dur(where)}"
        try:
            self._load(where)
        except _LinkLost:
            self._reloads.pop()  # the TV never got this one: not its failure, so it does not use up the budget
            raise
