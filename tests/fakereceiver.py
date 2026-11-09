"""A fake Cast receiver for the tests: the Default Media Receiver as a TV runs it, on 127.0.0.1.

It speaks the real CASTV2 framing over plain TCP (senders connect with tls=False, so no certificates are
needed) and behaves like a TV:

- answers GET_STATUS, LAUNCH, CONNECT/CLOSE and heartbeats; broadcasts RECEIVER_STATUS and MEDIA_STATUS changes
- on LOAD it actually downloads the media URL with Range requests, as a TV does (the start of the file, then
  from the offset the start position maps to), checks the bytes against the real file, and only then reports
  PLAYING. Its clock then runs in real time.
- honours PAUSE, PLAY, SEEK (and fetches from the new offset), STOP, and the receiver's STOP (closes the app)
- HLS mode: when the media's contentType says mpegurl (a playlist, as cast on the fly sends), it downloads like
  a TV playing HLS instead: the playlist (a plain GET, CORS checked), parsed for EXTINF durations, segment URIs
  (resolved against the playlist's URL) and ENDLIST, then the segment holding the start position and the one after
  it. A SEEK fetches the segments at the new position from the playlist it already has. `fetches` then holds
  ("playlist" | the segment's file name, status, length, ok) instead of byte ranges, and `playlist` the last
  parsed one ({"segments": [(start, duration, url)], "ended": bool}).

Tests make it misbehave on purpose:
  drop()       hang up on every sender (TCP close), the movie keeps playing
  mute         stop answering anything, without closing (a silent TV)
  forget()     lose the media session (the app keeps running, status lists nothing)
  fail()       IDLE + ERROR;  finish()  IDLE + FINISHED;  interrupt()  IDLE + INTERRUPTED (someone else cast)
  buffering()  report BUFFERING;  close_app()  the player closes (Back on the remote)
  odd          fields that replace those of every media status entry (NaN, text, ...: JSON allows them)
and constructor flags for failures at start: fetch=False (never downloads, like a firewalled Mac),
load_error="LOAD_FAILED", launch_error="NOT_FOUND", launch=False (ignores LAUNCH), play=False (downloads but
never starts playing), fail_load_after_fetch=True (downloads, then LOAD_FAILED: a format the TV cannot play),
reset_session_ids_on_launch=True (a freshly launched app numbers its media sessions from 1 again, as real ones do),
other_sender=True (another sender's movie is already playing; our LOAD ends it, and the TV announces that under our
LOAD's request id and without `media`),
idle_app=IDLE_APP (a TV that lists its idle screen in RECEIVER_STATUS while no player runs),
zero_while_buffering=True (reports currentTime 0 whenever it is BUFFERING).
"""

import http.client
import json
import socket
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from castmymovie.castlink import CONN_NS, HEART_NS, MEDIA_NS, RECV_NS, decode_message, encode_message

APP = {"appId": "CC1AD845", "displayName": "Default Media Receiver", "transportId": "web-7", "sessionId": "app-1",
       "namespaces": [{"name": MEDIA_NS}]}
IDLE_APP = {"appId": "E8C28D3C", "displayName": "Backdrop", "isIdleScreen": True, "transportId": "web-0",
            "sessionId": "idle-1", "namespaces": []}


class FakeReceiver:
    def __init__(self, media=None, fetch=True, play=True, load_error=None, launch_error=None, app_running=False,
                 fail_load_after_fetch=False, launch=True, reset_session_ids_on_launch=False, other_sender=False, idle_app=None, zero_while_buffering=False):
        self.media = Path(media) if media else None
        self.fetch, self.play, self.load_error, self.launch_error = fetch, play, load_error, launch_error
        self.launch = launch               # False: LAUNCH is silently ignored (a TV that never opens the app)
        self.fail_load_after_fetch = fail_load_after_fetch
        self.reset_session_ids_on_launch = reset_session_ids_on_launch
        self.idle_app = idle_app           # listed in place of the player when none runs
        self.zero_while_buffering = zero_while_buffering
        self.lock = threading.RLock()
        self.conns = []
        self.other_sender = other_sender
        self.app = dict(APP) if app_running or other_sender else None
        self.session = None            # {"id", "state", "idle", "pos", "at", "media"}
        self.next_id = 1
        if other_sender:
            self.session = {"id": 1, "state": "PLAYING", "idle": None, "pos": 30.0, "at": time.monotonic(),
                            "media": {"contentId": "http://192.0.2.9:8009/theirs.mp4", "contentType": "video/mp4"}}
            self.next_id = 2
        self.loads, self.seen, self.fetches = [], [], []
        self.fetch_errors = []
        self.playlist = None               # HLS mode: the last playlist fetched, parsed
        self.mute = False
        self.odd = {}
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ------------------------------------------------------------ test controls
    def close(self):
        self._closed = True
        self.drop()
        try:
            self.srv.close()
        except OSError:
            pass

    def drop(self):
        with self.lock:
            conns, self.conns = self.conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def forget(self):
        with self.lock:
            self.session = None

    def fail(self):
        self._end("ERROR")

    def finish(self):
        self._end("FINISHED")

    def interrupt(self):
        self._end("INTERRUPTED")

    def buffering(self):
        with self.lock:
            self._set("BUFFERING")
            self._broadcast_media()

    def close_app(self):
        with self.lock:
            self.app, self.session = None, None
            self._broadcast(RECV_NS, self._receiver_status())

    def kinds(self, namespace=None):
        """Message types received so far (optionally only on one namespace)."""
        return [t for ns, t, _ in self.seen if namespace is None or ns == namespace]

    def position(self):
        with self.lock:
            s = self.session
            if not s:
                return None
            if s["state"] == "PLAYING":
                return s["pos"] + (time.monotonic() - s["at"])
            return s["pos"]

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------ playback model
    def _set(self, state, idle=None, pos=None):
        s = self.session
        s["pos"] = self.position() if pos is None else pos
        s["at"], s["state"], s["idle"] = time.monotonic(), state, idle

    def _end(self, reason):
        with self.lock:
            if self.session:
                self._set("IDLE", reason)
                self._broadcast_media()

    def _media_status(self):
        s = self.session
        if not s:
            return []
        shown = 0.0 if self.zero_while_buffering and s["state"] == "BUFFERING" else self.position()
        st = {"mediaSessionId": s["id"], "playerState": s["state"], "currentTime": shown,
              "playbackRate": 1, "supportedMediaCommands": 15, "media": s["media"]}
        if s["idle"]:
            st["idleReason"] = s["idle"]
        st.update(self.odd)
        return [st]

    def _receiver_status(self, rid=0):
        apps = [self.app] if self.app else [self.idle_app] if self.idle_app else []
        return {"type": "RECEIVER_STATUS", "requestId": rid,
                "status": {"applications": apps, "volume": {"level": 1, "muted": False}}}

    # ------------------------------------------------------------ wire
    def _accept(self):
        while not self._closed:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send(self, conn, src, dst, ns, payload):
        try:
            conn.sendall(encode_message(src, dst, ns, payload))
        except OSError:
            pass

    def _broadcast(self, ns, payload):
        src = self.app["transportId"] if ns == MEDIA_NS and self.app else "receiver-0"
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            self._send(c, src, "*", ns, payload)

    def _broadcast_media(self):
        if self.app:
            self._broadcast(MEDIA_NS, {"type": "MEDIA_STATUS", "requestId": 0, "status": self._media_status()})

    def _serve(self, conn):
        try:
            self._read_loop(conn)
        finally:
            conn.close()

    def _read_loop(self, conn):
        buf = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while len(buf) >= 4:
                size = struct.unpack(">I", buf[:4])[0]
                if len(buf) < 4 + size:
                    break
                f = decode_message(buf[4:4 + size])
                buf = buf[4 + size:]
                src, dst, ns = f[2].decode(), f[3].decode(), f[4].decode()
                msg = json.loads(f[6].decode())
                with self.lock:
                    self.seen.append((ns, msg.get("type"), msg))
                    if not self.mute:
                        self._on(conn, src, dst, ns, msg)

    def _on(self, conn, src, dst, ns, msg):
        kind, rid = msg.get("type"), msg.get("requestId", 0)
        me = self.app["transportId"] if self.app and dst == self.app["transportId"] else "receiver-0"

        def reply(payload, namespace=ns):
            self._send(conn, me, src, namespace, dict(payload, requestId=rid))

        if ns == HEART_NS and kind == "PING":
            self._send(conn, "receiver-0", src, HEART_NS, {"type": "PONG"})
        elif ns == RECV_NS and kind == "GET_STATUS":
            reply(self._receiver_status(rid))
        elif ns == RECV_NS and kind == "LAUNCH":
            if not self.launch:
                return
            if self.launch_error:
                reply({"type": "LAUNCH_ERROR", "reason": self.launch_error})
                return
            self.app = dict(APP)
            if self.reset_session_ids_on_launch:
                self.next_id = 1
            self._broadcast(RECV_NS, self._receiver_status(rid))
        elif ns == RECV_NS and kind == "STOP":
            self.app, self.session = None, None
            self._broadcast(RECV_NS, self._receiver_status(rid))
        elif ns == MEDIA_NS and not self.app:
            reply({"type": "INVALID_REQUEST", "reason": "INVALID_COMMAND"})
        elif ns == MEDIA_NS and kind == "GET_STATUS":
            reply({"type": "MEDIA_STATUS", "status": self._media_status()})
        elif ns == MEDIA_NS and kind == "LOAD":
            self._load(conn, src, msg, reply)
        elif ns == MEDIA_NS and kind in ("PAUSE", "PLAY", "SEEK", "STOP"):
            s = self.session
            if not s or msg.get("mediaSessionId") != s["id"]:
                reply({"type": "INVALID_REQUEST", "reason": "INVALID_MEDIA_SESSION_ID"})
                return
            if kind == "PAUSE":
                self._set("PAUSED")
            elif kind == "PLAY":
                self._set("PLAYING")
            elif kind == "STOP":
                self._set("IDLE", "CANCELLED")
            else:
                state = {"PLAYBACK_START": "PLAYING", "PLAYBACK_PAUSE": "PAUSED"}.get(msg.get("resumeState"), s["state"])
                self._set(state, pos=float(msg["currentTime"]))
                threading.Thread(target=self._download, args=(s["media"], float(msg["currentTime"]), True), daemon=True).start()
            self._broadcast_media()

    def _load(self, conn, src, msg, reply):
        self.loads.append(msg)
        if self.load_error:
            reply({"type": self.load_error, "detailedErrorCode": 104})
            return
        old = self.session
        if old and old["state"] != "IDLE":  # a new LOAD ends the old session, like the real receiver does
            self._set("IDLE", "INTERRUPTED")
            if self.other_sender:
                status = self._media_status()
                status[0].pop("media")
                self._broadcast(MEDIA_NS, {"type": "MEDIA_STATUS", "requestId": rid_of(msg), "status": status})
            else:
                self._broadcast_media()
        media = msg["media"]
        self.session = {"id": self.next_id, "state": "BUFFERING", "idle": None, "pos": float(msg.get("currentTime", 0)),
                        "at": time.monotonic(), "media": media}
        self.next_id += 1
        reply({"type": "MEDIA_STATUS", "status": self._media_status()})
        if self.fetch:
            threading.Thread(target=self._start_playing, args=(dict(self.session), msg, rid_of(msg)), daemon=True).start()

    def _start_playing(self, session, msg, rid):
        ok = self._download(session["media"], session["pos"])
        with self.lock:
            if not self.session or self.session["id"] != session["id"]:
                return
            if not ok or self.fail_load_after_fetch:
                self.session["state"], self.session["idle"] = "IDLE", "ERROR"
                self._broadcast(MEDIA_NS, {"type": "LOAD_FAILED", "requestId": rid, "detailedErrorCode": 104})
                self._broadcast_media()
                return
            if self.play:
                self._set("PLAYING" if msg.get("autoplay", True) else "PAUSED")
                self._broadcast_media()

    # ------------------------------------------------------------ being a TV on the HTTP side
    def _download(self, media, position, seek=False):
        """Fetch the start of the file, then 256 KB from where `position` falls, like a TV filling its buffer.
        For an HLS playlist: see _download_hls. `seek` says this is a SEEK, not a LOAD."""
        url = media.get("contentUrl") or media["contentId"]
        if "mpegurl" in str(media.get("contentType", "")).lower():
            return self._download_hls(url, position, seek)
        try:
            head = self._get(url, "bytes=0-65535")
            total = int(head[1]["content-range"].rsplit("/", 1)[1])
            duration = media.get("duration") or 0
            offset = int(total * position / duration) if duration else 0
            offset = min(offset, max(total - 1, 0))
            self._get(url, f"bytes={offset}-", limit=256 * 1024)
            return True
        except Exception as e:  # noqa: BLE001 - any failure is what a TV would report as a load error
            self.fetch_errors.append(repr(e))
            return False

    def _download_hls(self, url, position, seek):
        """Fetch the playlist (not on a SEEK: a TV keeps the one it has), then the segment holding `position` and the next."""
        try:
            if not (seek and self.playlist):
                body, _ = self._fetch(url, "playlist")
                self.playlist = parse_playlist(body.decode(), url)
            segments = self.playlist["segments"]
            if not segments:
                raise AssertionError("empty playlist")
            at = next((i for i, (start, length, _) in enumerate(segments) if start <= position < start + length), len(segments) - 1)
            for _, _, seg_url in segments[at:at + 2]:
                self._fetch(seg_url, urlsplit(seg_url).path.rsplit("/", 1)[-1])
            return True
        except Exception as e:  # noqa: BLE001
            self.fetch_errors.append(repr(e))
            return False

    def _fetch(self, url, name):
        """A plain GET, no Range, as a TV does for playlists and segments; the response must be a 200 with CORS."""
        parts = urlsplit(url)
        c = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            c.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""), headers={"Origin": "https://www.gstatic.com"})
            r = c.getresponse()
            body = r.read()
            headers = {k.lower(): v for k, v in r.getheaders()}
            ok = (r.status == 200 and headers.get("access-control-allow-origin") == "*"
                  and headers.get("content-length") == str(len(body)) and len(body) > 0)
            self.fetches.append((name, r.status, len(body), ok))
            if not ok:
                raise AssertionError(f"bad response to {name}: {r.status}")
            return body, headers
        finally:
            c.close()

    def _get(self, url, rng, limit=None):
        parts = urlsplit(url)
        c = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        try:
            c.request("GET", parts.path, headers={"Range": rng, "Origin": "https://www.gstatic.com"})
            r = c.getresponse()
            body = r.read(limit) if limit else r.read()
            headers = {k.lower(): v for k, v in r.getheaders()}
            first = int(rng.split("=")[1].split("-")[0])
            ok = r.status == 206 and headers.get("access-control-allow-origin") == "*"
            if self.media is not None:
                with open(self.media, "rb") as f:
                    f.seek(first)
                    ok = ok and f.read(len(body)) == body
            self.fetches.append((rng, r.status, len(body), ok))
            if not ok:
                raise AssertionError(f"bad response to {rng}: {r.status}")
            return body, headers
        finally:
            c.close()


def parse_playlist(text, url):
    """{"segments": [(start, duration, absolute url)], "ended": ENDLIST seen}: start is the sum of the EXTINF before it."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise AssertionError("not a playlist")
    segments, start, duration = [], 0.0, None
    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",")[0])
        elif not line.startswith("#"):
            if duration is None:
                raise AssertionError(f"segment without EXTINF: {line}")
            segments.append((start, duration, urljoin(url, line)))
            start, duration = start + duration, None
    return {"segments": segments, "ended": "#EXT-X-ENDLIST" in lines}


def rid_of(msg):
    return msg.get("requestId", 0)
