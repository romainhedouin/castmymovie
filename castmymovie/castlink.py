"""The Cast (CASTV2) wire protocol, by hand: one connection class, and a read-only status client.

`CastConnection` is the plumbing every Cast client needs: a TLS socket to port 8009, the framing, request ids.
Two clients use it:

  - `StatusReader` (here) only *listens*: it connects as a second "sender" (the way the Google Home app does),
    sends GET_STATUS, and reads the TV's own playback position and player state. It never launches an app,
    loads media or sends playback commands, so it can sit next to VLC without disturbing its cast.
  - `nativecast.NativeCast` is a full sender: it launches the TV's media player and drives it.

The wire format is a 4-byte big-endian length followed by a protobuf `CastMessage`; the one message
shape we need is small enough to encode and decode by hand, so there is no protobuf dependency.
"""

from __future__ import annotations

import json
import math
import socket
import ssl
import struct
import time
from dataclasses import dataclass

CONN_NS = "urn:x-cast:com.google.cast.tp.connection"
RECV_NS = "urn:x-cast:com.google.cast.receiver"
MEDIA_NS = "urn:x-cast:com.google.cast.media"
HEART_NS = "urn:x-cast:com.google.cast.tp.heartbeat"
SENDER = "sender-castmymovie"
HEARTBEAT_EVERY = 4.0
MAX_FRAME = 65536    # the Cast protocol's limit for one message: a longer length prefix is not a Cast receiver
MAX_VARINT = 10      # bytes: a protobuf varint holds at most 64 bits


# ---------------------------------------------------------------- protobuf by hand

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf: bytes, i: int) -> tuple:
    n = shift = 0
    for _ in range(MAX_VARINT):
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
    raise ValueError("protobuf varint longer than 10 bytes")  # else a run of 0xff bytes makes this quadratic


def _string_field(num: int, value: str) -> bytes:
    raw = value.encode()
    return _varint(num << 3 | 2) + _varint(len(raw)) + raw


def encode_message(source: str, dest: str, namespace: str, payload: dict) -> bytes:
    """CastMessage: protocol_version=CASTV2_1_0, ids, namespace, payload_type=STRING, JSON payload."""
    body = (b"\x08\x00" + _string_field(2, source) + _string_field(3, dest) + _string_field(4, namespace)
            + b"\x28\x00" + _string_field(6, json.dumps(payload)))
    return struct.pack(">I", len(body)) + body


def decode_message(body: bytes) -> dict:
    """Returns {field number: int or bytes} for one CastMessage body (without the length prefix)."""
    out, i = {}, 0
    while i < len(body):
        key, i = _read_varint(body, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(body, i)
        elif wire == 2:
            size, i = _read_varint(body, i)
            if i + size > len(body):
                raise ValueError("protobuf field runs past the end of the message")
            value, i = body[i:i + size], i + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out[num] = value
    return out


# ---------------------------------------------------------------- one connection

@dataclass(frozen=True)
class Message:
    source: str
    dest: str
    namespace: str
    data: dict          # the parsed JSON payload

    @property
    def type(self) -> str:
        return str(self.data.get("type", ""))


class CastConnection:
    """A CASTV2 channel to one receiver. Not thread-safe: exactly one thread may use a connection.

    `read()` closes the socket when the TV hangs up, and `send()` then raises OSError, which every caller
    already treats as "the TV went away"."""

    SEND_TIMEOUT = 5.0   # a send may wait this long for room in the socket buffer on a congested link
    READ_SLICE = 0.2

    def __init__(self, host: str, port: int = 8009, tls: bool = True, sender: str = SENDER):
        self.host, self.port, self.tls, self.sender = host, port, tls, sender
        self.sock: socket.socket | None = None
        self.last_rx = 0.0           # monotonic time anything at all last arrived: how "dead" is measured
        self._buf = b""
        self._rid = 0

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def open(self, timeout: float = 4.0) -> None:
        """Raises OSError (ssl.SSLError is one) when the TV cannot be reached."""
        self.close()
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            if self.tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # Cast devices present self-signed certificates; this is LAN traffic
                raw = ctx.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        raw.settimeout(self.READ_SLICE)
        self.sock, self._buf, self.last_rx = raw, b"", time.monotonic()

    def send(self, dest: str, namespace: str, payload: dict) -> int:
        """Send one message with a fresh request id, which is returned (replies carry it back)."""
        if self.sock is None:
            raise OSError("not connected")
        self._rid += 1
        frame = encode_message(self.sender, dest, namespace, dict(payload, requestId=self._rid))
        self.sock.settimeout(self.SEND_TIMEOUT)  # a timeout half way through a frame would garble the stream
        try:
            self.sock.sendall(frame)
        finally:
            if self.sock is not None:
                self.sock.settimeout(self.READ_SLICE)
        return self._rid

    def read(self, seconds: float, until_first: bool = False) -> list:
        """Every Message that arrives within `seconds` (or, with `until_first`, as soon as some have). Closes the
        connection on EOF, and raises OSError (after closing it) on a frame over MAX_FRAME. A frame that is not a
        message we understand is skipped: its length prefix keeps us in step with the next one."""
        messages, end = [], time.monotonic() + seconds
        while time.monotonic() < end and self.sock and not (until_first and messages):
            try:
                data = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not data:
                self.close()
                break
            self.last_rx = time.monotonic()
            self._buf += data
            while len(self._buf) >= 4:
                size = struct.unpack(">I", self._buf[:4])[0]
                if size > MAX_FRAME:  # refused before buffering any of it, or a 4 GB prefix would be waited on forever
                    self.close()
                    raise OSError(f"frame of {size} bytes is over the Cast limit of {MAX_FRAME}")
                if len(self._buf) < 4 + size:
                    break
                body, self._buf = self._buf[4:4 + size], self._buf[4 + size:]
                try:
                    f = decode_message(body)
                    payload = json.loads(f[6].decode())
                    if not isinstance(payload, dict):
                        continue  # every message we act on is a JSON object
                    messages.append(Message(f.get(2, b"").decode(), f.get(3, b"").decode(), f[4].decode(), payload))
                except Exception:  # untrusted bytes: a binary payload, a field of the wrong type, hostile JSON
                    continue
        return messages

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self._buf = None, b""


# ---------------------------------------------------------------- the reader

def num(value, default=None, lo: float = 0.0, hi: float = 1e6):
    """A number the TV sent, as a float clamped to lo..hi, or `default` for anything else. JSON lets NaN, Infinity,
    text, lists and booleans through where a number was expected, and none of them must reach the arithmetic."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        x = float(value)
    except OverflowError:  # an integer too big for a float
        return default
    return min(max(x, lo), hi) if math.isfinite(x) else default


@dataclass
class TvStatus:
    position: float | None   # seconds, as the TV reports it
    state: str | None        # IDLE | PLAYING | PAUSED | BUFFERING
    duration: float | None


class StatusReader:
    """Connect once, then poll() as often as you like. Every method is safe to call when offline."""

    def __init__(self, host: str, port: int = 8009, tls: bool = True):
        self.host, self.port, self.tls = host, port, tls
        self.conn = CastConnection(host, port, tls)
        self.transport: str | None = None
        self.last: TvStatus | None = None
        self._last_ping = 0.0

    @property
    def sock(self):
        return self.conn.sock

    @property
    def connected(self) -> bool:
        return self.conn.connected and self.transport is not None

    # --- plumbing
    def _send(self, dest: str, namespace: str, payload: dict) -> None:
        self.conn.send(dest, namespace, payload)  # raises OSError once read() saw the TV hang up

    def _drain(self, seconds: float) -> list:
        """Read whatever arrives in `seconds`; returns [(namespace, parsed JSON)]. Closes on EOF."""
        return [(m.namespace, m.data) for m in self.conn.read(seconds)]

    # --- public
    def open(self, timeout: float = 4.0) -> bool:
        """Connect and attach to the app currently running on the TV. False if there is none (yet)."""
        self.close()
        try:
            self.conn.open(timeout)
            self._send("receiver-0", CONN_NS, {"type": "CONNECT"})
            self._send("receiver-0", RECV_NS, {"type": "GET_STATUS"})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self.conn.connected and not self.transport:
                for _, msg in self._drain(0.3):
                    apps = (msg.get("status") or {}).get("applications") or []
                    if msg.get("type") == "RECEIVER_STATUS" and apps:
                        self.transport = apps[0].get("transportId")
            if self.transport:
                self._send(self.transport, CONN_NS, {"type": "CONNECT"})
                return True
        except (OSError, ssl.SSLError):
            pass
        self.close()
        return False

    def poll(self) -> TvStatus | None:
        """Ask for the current status and return it (the last known one if the TV did not answer in time)."""
        if not self.connected:
            return self.last
        try:
            now = time.monotonic()
            if now - self._last_ping > HEARTBEAT_EVERY:
                self._send("receiver-0", HEART_NS, {"type": "PING"})
                self._last_ping = now
            self._send(self.transport, MEDIA_NS, {"type": "GET_STATUS"})
            for namespace, msg in self._drain(0.25):
                kind = msg.get("type")
                if namespace == HEART_NS and kind == "PING":
                    self._send("receiver-0", HEART_NS, {"type": "PONG"})
                elif kind == "CLOSE":
                    self.close()
                    break
                elif kind == "MEDIA_STATUS" and msg.get("status"):
                    st = msg["status"][0]
                    media = st.get("media") or {}
                    self.last = TvStatus(num(st.get("currentTime")), st.get("playerState"), num(media.get("duration")))
        except (OSError, ssl.SSLError):
            self.close()
        return self.last

    def close(self) -> None:
        self.conn.close()
        self.transport = None
