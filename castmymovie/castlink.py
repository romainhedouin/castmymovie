"""The Cast (CASTV2) wire protocol, by hand: the connection class `nativecast.NativeCast` drives the TV with.

`CastConnection` is the plumbing a Cast client needs: a TLS socket to port 8009, the framing, request ids.

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
