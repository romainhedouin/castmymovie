"""A tiny HTTP server that hands one movie (and its subtitles) to the TV, the way a web server would.

The native cast tells the TV "play http://<this Mac>:<port>/<token>.mp4" and the TV does the rest: it downloads
ahead, pauses when it runs dry, seeks with Range requests. All this server has to do is answer those requests
correctly and fast, so it is a plain file server with a few deliberate choices:

- One file, under a random token path: nothing else on the Mac is reachable, the URL never shows the file name,
  and only the TV's own address gets an answer (anyone else is hung up on).
- Proper Range support (`bytes=N-`, `N-M`, `-N`, 206 + Content-Range, 416 past the end). Anything it does not
  understand (several ranges, another unit) is answered with the whole file, which HTTP allows.
- CORS headers, because the TV's player is a web page from Google's origin fetching from ours.
- It counts what it sends (total, per request, how far into the file the TV has read), which is what the
  player panel's Link and Buffer rows are built from, and logs one line per request for troubleshooting.

With a `stream` (cast on the fly) there is no file: the TV is handed an HLS playlist and the segments the encoder
produces, under the same token. A segment is answered only once the encoder has it, so a request may wait (a TV that
hangs up meanwhile, or a stop(), ends the wait), and the server remembers which segments were delivered in full,
which is how far ahead of the playhead the TV has buffered.
"""

from __future__ import annotations

import io
import re
import secrets
import select
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .engine import mdat_offset

CHUNK = 512 * 1024
UNSATISFIABLE = "unsatisfiable"
PLAYLIST_TYPE = "application/x-mpegURL"
SEGMENT_TYPE = "video/mp2t"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, Content-Type, Accept, Origin",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}


def parse_range(header: str | None, size: int):
    """None (send the whole file), (first, last) inclusive, or UNSATISFIABLE (answer 416)."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*", header)
    if not m or m.groups() == ("", ""):
        # several ranges, another unit, garbage, or a number no file has (int() also refuses 4300+ digits):
        # RFC 7233 lets us ignore the header
        return None
    first, last = m.groups()
    if not first:  # "-N": the last N bytes
        n = int(last)
        return (max(size - n, 0), size - 1) if n and size else UNSATISFIABLE
    first = int(first)
    if last and int(last) < first:
        return None
    if first >= size:
        return UNSATISFIABLE
    return first, min(int(last), size - 1) if last else size - 1


@dataclass(frozen=True)
class Served:
    """One finished request, kept in a short ring for the log and the tests."""
    client: str
    method: str
    path: str
    status: int
    first: int          # first byte offset asked for
    sent: int           # bytes actually written
    seconds: float
    aborted: bool       # the TV hung up before the end (normal after a seek or when its buffer is full)
    segment: int | None = None   # index of the HLS segment this request delivered in full, if it did


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
    owner: "MediaServer"

    def verify_request(self, request, client_address) -> bool:
        return self.owner.admit(client_address[0])

    def handle_error(self, request, client_address) -> None:
        pass  # errors are logged by the handler; a traceback on the terminal would wreck the player panel


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: the TV reuses one connection for many range requests
    server_version = "castmymovie"
    timeout = 120                   # a TV that has not read anything for 2 minutes has moved on (nginx uses 60 s)

    def log_message(self, format, *args) -> None:
        pass  # the owner writes its own, more useful, lines per request

    def setup(self) -> None:
        super().setup()
        self.server.owner.track(self.connection, True)

    def finish(self) -> None:
        self.server.owner.track(self.connection, False)
        try:
            super().finish()
        except OSError:
            pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionError, socket.timeout, OSError):
            self.close_connection = True  # the TV hung up or went quiet: nothing to report to anyone

    def _headers(self, status: int, extra: dict) -> None:
        self.send_response(status)
        for k, v in {**CORS, **extra}.items():
            self.send_header(k, str(v))
        self.end_headers()

    def do_OPTIONS(self) -> None:
        extra = {"Access-Control-Max-Age": 600}   # a 204 has no body and must not say Content-Length (RFC 7230 3.3.2)
        asked = self.headers.get("Access-Control-Request-Headers")
        if asked:
            extra["Access-Control-Allow-Headers"] = asked
        self._headers(204, extra)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_GET(self) -> None:
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        owner: MediaServer = self.server.owner
        t0, client = time.monotonic(), self.client_address[0]
        kind, arg = owner.resolve(self.path)
        if kind is None or (kind == "segment" and arg >= owner.stream.timeline.count):
            self._headers(404, {"Content-Length": 0})
            owner.finished(Served(client, self.command, self.path, 404, 0, 0, 0.0, False), self.headers.get("Range"))
            return
        segment = None
        if kind == "file":
            source, ctype = arg
            size = source.stat().st_size
            owner.begun(client, self.command, self.path, self.headers.get("Range"))
        elif kind == "playlist":
            source, ctype = owner.stream.timeline.playlist(), PLAYLIST_TYPE
            size = len(source)
            owner.begun(client, self.command, self.path, self.headers.get("Range"))
        else:
            segment, ctype = arg, SEGMENT_TYPE
            owner.begun(client, self.command, self.path, self.headers.get("Range"))
            source, why = self._wait_for_segment(segment)
            if source is None:
                self._not_ready(owner, client, t0, why)
                return
            size = source.stat().st_size
        rng = parse_range(self.headers.get("Range"), size)
        if rng == UNSATISFIABLE:
            self._headers(416, {"Content-Range": f"bytes */{size}", "Content-Length": 0})
            owner.finished(Served(client, self.command, self.path, 416, 0, 0, 0.0, False), self.headers.get("Range"))
            return
        first, last = rng if rng else (0, size - 1)
        length = max(last - first + 1, 0)
        extra = {"Content-Type": ctype, "Content-Length": length, "Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
        if rng:
            extra["Content-Range"] = f"bytes {first}-{last}/{size}"
        status = 206 if rng else 200
        self._headers(status, extra)
        sent, aborted = 0, False
        is_media = source == owner.media
        if body and length:
            try:
                with io.BytesIO(source) if isinstance(source, bytes) else open(source, "rb") as f:
                    f.seek(first)
                    while sent < length and not owner.closed.is_set():
                        data = f.read(min(CHUNK, length - sent))
                        if not data:
                            break
                        self.wfile.write(data)
                        sent += len(data)
                        owner.count(len(data), first + sent if is_media else None)
            except (ConnectionError, socket.timeout, OSError):
                pass
            aborted = sent < length
            if aborted:
                self.close_connection = True
        whole = body and not aborted and first == 0 and sent == size
        owner.finished(Served(client, self.command, self.path, status, first, sent, time.monotonic() - t0, aborted,
                              segment if whole else None), self.headers.get("Range"))

    def _wait_for_segment(self, n: int):
        """(path, "") once the encoder has segment n; (None, why) when it will not come: "closed" (the server is
        stopping), "gone" (the TV hung up while waiting), "dead" (the encoder died), "timeout"."""
        owner: MediaServer = self.server.owner
        deadline = time.monotonic() + owner.SEGMENT_WAIT
        while True:
            left = deadline - time.monotonic()
            if owner.closed.is_set():
                return None, "closed"
            if left <= 0:
                return None, "timeout"
            path = owner.stream.segment(n, min(owner.WAIT_SLICE, left))
            if path is not None:
                return Path(path), ""
            if owner.stream.error:
                return None, "dead"
            if self._peer_gone():
                return None, "gone"
            owner.closed.wait(0.02)   # segment() answering at once without a file must not turn this into a busy loop

    def _peer_gone(self) -> bool:
        """True when the TV closed its end of the connection (a request waits silently, so it would not notice)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _not_ready(self, owner, client: str, t0: float, why: str) -> None:
        seconds = time.monotonic() - t0
        if why in ("closed", "gone"):
            self.close_connection = True   # nobody is listening any more: do not even try to answer
            owner.finished(Served(client, self.command, self.path, 503, 0, 0, seconds, True), self.headers.get("Range"))
            return
        reason = f"the encoder stopped: {owner.stream.error}" if why == "dead" else f"not ready after {owner.SEGMENT_WAIT:.0f} s"
        owner.log(f"HTTP {client} {self._what_named()} -> 503, {reason}")
        self._headers(503, {"Retry-After": 2, "Content-Length": 0, "Cache-Control": "no-cache"})
        owner.finished(Served(client, self.command, self.path, 503, 0, 0, seconds, False), self.headers.get("Range"))

    def _what_named(self) -> str:
        return f"{self.command} {self.server.owner._what(self.path)}"


class MediaServer:
    """Serves `media` (and `subtitles`) to the addresses in `allow` only. start() before use, stop() after.

    With `stream` (an onthefly.OnTheFly) it serves that instead of the file: the HLS playlist and its segments."""

    SEGMENT_WAIT = 45.0    # how long a request waits for the encoder to produce a segment before it is told to retry
    WAIT_SLICE = 0.2       # a waiting request looks up this often for a stop() or a TV that hung up

    def __init__(self, media: Path, subtitles=(), allow=(), log: Callable[[str], None] | None = None, stream=None):
        self.media = Path(media)
        self.stream = stream
        self.token = secrets.token_urlsafe(18)
        self.routes = {} if stream else {f"/{self.token}.mp4": (self.media, "video/mp4")}   # streaming: no file to offer
        self.subtitle_paths = []
        for i, sub in enumerate(subtitles, 1):
            p = f"/{self.token}-{i}.vtt"
            self.routes[p] = (Path(sub), "text/vtt; charset=utf-8")
            self.subtitle_paths.append(p)
        self.allow = set(allow)
        self.log = log or (lambda line: None)
        self.payload_start = None if stream else mdat_offset(self.media)   # the source file is never opened when streaming
        self.closed = threading.Event()
        self.port = 0
        self._httpd: _Server | None = None
        self._lock = threading.Lock()
        self.bytes_served = 0            # every byte of every file, all requests together
        self.media_requests = 0          # GETs for the movie that got through: 0 means the TV never came to fetch it
        self.furthest = 0                # the furthest file offset ever handed out
        self.reach: int | None = None    # file offset the most recently active movie download has got to
        self.recent: deque = deque(maxlen=200)
        self._open: set = set()          # live connections, hung up on at stop() so no download outlives the cast
        self.refused: dict = {}          # address -> attempts, for the log and for the "never fetched" diagnosis
        self.fetched: set[int] = set()   # streaming: indexes of the segments the TV has received in full

    # --- lifecycle
    def start(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self._httpd = _Server((host, port), _Handler)
        self._httpd.owner = self
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         name="castmymovie-http", daemon=True).start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self.closed.set()  # running downloads stop at their next chunk
        httpd, self._httpd = self._httpd, None
        httpd.shutdown()
        httpd.server_close()
        with self._lock:
            live = list(self._open)
        for conn in live:  # a download blocked on a TV that stopped reading would otherwise linger for minutes
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def url(self, host: str, path: str | None = None) -> str:
        return f"http://{host}:{self.port}{path or f'/{self.token}.mp4'}"

    def stream_url(self, host: str) -> str:
        return f"http://{host}:{self.port}/{self.token}/index.m3u8"

    def resolve(self, path: str):
        """What a request path asks for: ("file", (path, content type)), ("playlist", None), ("segment", index) or (None, None)."""
        path = path.split("?", 1)[0]
        if path in self.routes:
            return "file", self.routes[path]
        if self.stream is not None:
            if path == f"/{self.token}/index.m3u8":
                return "playlist", None
            m = re.fullmatch(re.escape(f"/{self.token}/") + r"seg(\d{5,9})\.ts", path)
            if m and path.endswith(f"seg{int(m.group(1)):05d}.ts"):
                return "segment", int(m.group(1))
        return None, None

    def buffered_until(self, position: float) -> float:
        """Media time up to which the TV holds delivered segments without a gap, from the one containing `position`.
        `position` itself when that segment has not been delivered."""
        if self.stream is None:
            return position
        timeline = self.stream.timeline
        with self._lock:
            i = timeline.index_at(position)
            if i not in self.fetched:
                return position
            while i + 1 in self.fetched:
                i += 1
        return timeline.end(i)

    # --- bookkeeping, called from the request threads
    def admit(self, client: str) -> bool:
        if client in self.allow:
            return True
        with self._lock:
            self.refused[client] = self.refused.get(client, 0) + 1
            first = self.refused[client] == 1
        if first:
            self.log(f"HTTP refused a connection from {client}: only the TV ({', '.join(sorted(self.allow))}) is served")
        return False

    def track(self, conn, live: bool) -> None:
        with self._lock:
            (self._open.add if live else self._open.discard)(conn)

    def begun(self, client: str, method: str, path: str, range_header: str | None) -> None:
        with self._lock:
            if method == "GET" and (path.split("?", 1)[0].endswith(".mp4") or self.resolve(path)[0] in ("playlist", "segment")):
                self.media_requests += 1
        self.log(f"HTTP {client} {method} {self._what(path)} Range={range_header or '-'} (start)")

    def _what(self, path: str) -> str:
        kind, arg = self.resolve(path)
        if kind == "playlist":
            return "playlist"
        if kind == "segment":
            return f"segment {arg}"
        path = path.split("?", 1)[0]
        return "movie" if path.endswith(".mp4") else "subtitles" if path.endswith(".vtt") else path[:60]

    def count(self, nbytes: int, media_offset: int | None) -> None:
        with self._lock:
            self.bytes_served += nbytes
            if media_offset is not None:
                self.reach = media_offset
                self.furthest = max(self.furthest, media_offset)

    def finished(self, served: Served, range_header: str | None) -> None:
        with self._lock:
            self.recent.append(served)
            if served.segment is not None:
                self.fetched.add(served.segment)
        self.log(f"HTTP {served.client} {served.method} {self._what(served.path)} Range={range_header or '-'} -> {served.status} "
                 f"from {served.first} sent {served.sent} B in {served.seconds:.1f} s"
                 + (" (TV hung up)" if served.aborted else ""))
