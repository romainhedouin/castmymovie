"""A tiny read-only Cast (CASTV2) client: asks the TV what it is playing right now.

VLC starts and controls the cast. This only *listens*: it connects as a second "sender" (the way the
Google Home app does), sends GET_STATUS, and reads the TV's own playback position and player state. It
never launches an app, loads media or sends playback commands.

The wire format is a 4-byte big-endian length followed by a protobuf `CastMessage`; the one message
shape we need is small enough to encode and decode by hand, so there is no protobuf dependency.
"""

from __future__ import annotations

import json
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
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


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
            value, i = body[i:i + size], i + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        out[num] = value
    return out


# ---------------------------------------------------------------- the reader

@dataclass
class TvStatus:
    position: float | None   # seconds, as the TV reports it
    state: str | None        # IDLE | PLAYING | PAUSED | BUFFERING
    duration: float | None


class StatusReader:
    """Connect once, then poll() as often as you like. Every method is safe to call when offline."""

    def __init__(self, host: str, port: int = 8009, tls: bool = True):
        self.host, self.port, self.tls = host, port, tls
        self.sock: socket.socket | None = None
        self.transport: str | None = None
        self.last: TvStatus | None = None
        self._buf = b""
        self._rid = 0
        self._last_ping = 0.0

    @property
    def connected(self) -> bool:
        return self.sock is not None and self.transport is not None

    # --- plumbing
    def _send(self, dest: str, namespace: str, payload: dict) -> None:
        if self.sock is None:  # _drain closes the socket on EOF, even when it still hands back messages to answer
            raise OSError("not connected")
        self._rid += 1
        self.sock.sendall(encode_message(SENDER, dest, namespace, dict(payload, requestId=self._rid)))

    def _drain(self, seconds: float) -> list:
        """Read whatever arrives in `seconds`; returns [(namespace, parsed JSON)]. Closes on EOF."""
        messages, end = [], time.monotonic() + seconds
        while time.monotonic() < end and self.sock:
            try:
                data = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                continue
            if not data:
                self.close()
                break
            self._buf += data
            while len(self._buf) >= 4:
                size = struct.unpack(">I", self._buf[:4])[0]
                if len(self._buf) < 4 + size:
                    break
                body, self._buf = self._buf[4:4 + size], self._buf[4 + size:]
                try:
                    fields = decode_message(body)
                    messages.append((fields[4].decode(), json.loads(fields[6].decode())))
                except (KeyError, ValueError, UnicodeDecodeError):
                    continue
        return messages

    # --- public
    def open(self, timeout: float = 4.0) -> bool:
        """Connect and attach to the app currently running on the TV. False if there is none (yet)."""
        self.close()
        try:
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
            if self.tls:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # Cast devices present self-signed certificates; this is read-only LAN traffic
                raw = ctx.wrap_socket(raw, server_hostname=self.host)
            self.sock = raw
            self.sock.settimeout(0.2)
            self._send("receiver-0", CONN_NS, {"type": "CONNECT"})
            self._send("receiver-0", RECV_NS, {"type": "GET_STATUS"})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self.sock and not self.transport:
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
                    self.last = TvStatus(st.get("currentTime"), st.get("playerState"), media.get("duration"))
        except (OSError, ssl.SSLError):
            self.close()
        return self.last

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self.transport, self._buf = None, None, b""
