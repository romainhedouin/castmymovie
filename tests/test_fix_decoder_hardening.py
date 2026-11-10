"""Hostile bytes from the "TV": the Cast decoder and reader must fail fast and cleanly, and carry on where they can."""

import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import castlink  # noqa: E402
from castmymovie.castlink import RECV_NS, SENDER, CastConnection, Message  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402
from test_castlink import real_frame  # noqa: E402
from test_nativecast import Base  # noqa: E402

NS = RECV_NS.encode()


def field(num, value):
    return castlink._varint(num << 3 | 2) + castlink._varint(len(value)) + value


def varint_field(num, value):
    return castlink._varint(num << 3) + castlink._varint(value)


def body_of(source=None, dest=None, ns=None, payload=None):
    """A CastMessage body. Each argument is a ready-encoded field, so a test can swap in a malformed one."""
    return ((source or field(2, b"receiver-0")) + (dest or field(3, b"sender-x")) + (ns or field(4, NS))
            + (payload or field(6, b'{"type":"X"}')))


def json_body(raw):
    return body_of(payload=field(6, raw))


def framed(body):
    return struct.pack(">I", len(body)) + body


def good(n=1):
    return castlink.encode_message("receiver-0", SENDER, RECV_NS, {"type": "RECEIVER_STATUS", "n": n})


def sized_body(size):
    """A valid CastMessage body of exactly `size` bytes (JSON padded to fit)."""
    pad = 0
    while True:
        body = castlink.encode_message("receiver-0", SENDER, RECV_NS, {"type": "X", "pad": "p" * pad})[4:]
        if len(body) == size:
            return body
        pad += size - len(body)


class RawServer:
    """A plain-TCP server that pushes `chunks` to its first client, then keeps the connection open."""

    def __init__(self, *chunks, gap=0.0):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.chunks, self.gap, self.conn = chunks, gap, None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            self.conn, _ = self.srv.accept()
            for chunk in self.chunks:
                self.conn.sendall(chunk)
                if self.gap:
                    time.sleep(self.gap)
        except OSError:
            pass

    def close(self):
        for sock in (self.conn, self.srv):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass


class Plumbing(unittest.TestCase):
    def conn_to(self, *chunks, **kw):
        srv = RawServer(*chunks, **kw)
        self.addCleanup(srv.close)
        conn = CastConnection("127.0.0.1", srv.port, tls=False)
        conn.open(timeout=2)
        self.addCleanup(conn.close)
        return conn

    def collect(self, conn, count, timeout=5.0):
        got, end = [], time.monotonic() + timeout
        while len(got) < count and time.monotonic() < end:
            got += conn.read(0.5, until_first=True)
        return got


class Decoder(unittest.TestCase):
    def test_a_varint_longer_than_ten_bytes_is_rejected_without_quadratic_work(self):
        # 256 KB of 0xff: decoded as one ever-growing integer this took over a second
        started = time.monotonic()
        with self.assertRaises(ValueError):
            castlink.decode_message(b"\x08" + b"\xff" * 262144)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_the_widest_valid_varint_still_decodes_and_one_more_byte_does_not(self):
        widest = castlink._varint(2 ** 64 - 1)
        self.assertEqual(len(widest), 10)
        self.assertEqual(castlink.decode_message(b"\x08" + widest), {1: 2 ** 64 - 1})
        with self.assertRaises(ValueError):
            castlink.decode_message(b"\x08" + b"\xff" * 10 + b"\x01")

    def test_a_field_that_runs_past_the_end_of_the_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            castlink.decode_message(b"\x32\x64{}")          # field 6 claims 100 bytes, has 2

    def test_a_truncated_varint_is_still_an_index_error(self):
        with self.assertRaises(IndexError):
            castlink.decode_message(b"\x08\xff")


HOSTILE = {
    "source as a varint": body_of(source=varint_field(2, 5)),
    "destination as a varint": body_of(dest=varint_field(3, 5)),
    "namespace as a varint": body_of(ns=varint_field(4, 5)),
    "payload as a varint": body_of(payload=varint_field(6, 5)),
    "payload that is a list": json_body(b"[1,2]"),
    "payload that is a number": json_body(b"7"),
    "payload that is null": json_body(b"null"),
    "payload that is a string": json_body(b'"hi"'),
    "payload that is not JSON": json_body(b"{not json"),
    "payload that is not UTF-8": json_body(b'{"type":"\xff"}'),
    "deeply nested lists": json_body(b"[" * 30000 + b"]" * 30000),
    "deeply nested objects": json_body(b'{"a":' * 10800 + b"1" + b"}" * 10800),
    "an eleven byte varint": b"\x08" + b"\xff" * 10 + b"\x01" + body_of(),
    "a 64 KB varint": b"\x08" + b"\xff" * 65000,
    "a field longer than the frame": body_of(payload=b"\x32\x64{}"),
    "a 32-bit field": b"\x0d\x00\x00\x00\x00" + body_of(),
    "a binary payload": body_of(payload=field(7, b"\x00\x01")),
    "a truncated varint": b"\x08\xff",
    "an empty frame": b"",
}


class Reader(Plumbing):
    def test_real_tv_frames_come_through_unchanged_whole_or_one_byte_at_a_time(self):
        names = ("receiver:RECEIVER_STATUS", "media:MEDIA_STATUS", "heartbeat:PONG")
        wire = b"".join(framed(real_frame(n)) for n in names)
        expected = []
        for n in names:
            f = castlink.decode_message(real_frame(n))
            expected.append(Message(f[2].decode(), f[3].decode(), f[4].decode(), json.loads(f[6].decode())))
        for chunks in ([wire], [wire[i:i + 1] for i in range(len(wire))]):
            self.assertEqual(self.collect(self.conn_to(*chunks), 3), expected)

    def test_a_malformed_frame_is_skipped_and_the_next_good_one_still_arrives(self):
        for name, body in HOSTILE.items():
            with self.subTest(name):
                conn = self.conn_to(framed(body) + good(1) + framed(body) + good(2))
                started = time.monotonic()
                got = self.collect(conn, 2)
                self.assertEqual([m.data["n"] for m in got], [1, 2])
                self.assertEqual(got[0].type, "RECEIVER_STATUS")
                self.assertTrue(conn.connected)
                self.assertLess(time.monotonic() - started, 3.0)
                conn.close()

    def test_a_json_payload_that_is_not_an_object_never_becomes_a_message(self):
        conn = self.conn_to(framed(json_body(b"[1]")) + framed(json_body(b"null")) + framed(json_body(b"3.5")))
        self.assertEqual(conn.read(0.5), [])
        self.assertTrue(conn.connected)

    def test_a_frame_of_exactly_the_cast_limit_is_accepted(self):
        body = sized_body(65536)
        self.assertEqual(len(body), 65536)
        conn = self.conn_to(framed(body))
        (m,) = conn.read(5, until_first=True)
        self.assertEqual(m.type, "X")
        self.assertTrue(conn.connected)

    def test_a_frame_one_byte_over_the_limit_drops_the_link(self):
        conn = self.conn_to(framed(sized_body(65537)))
        with self.assertRaises(OSError) as ctx:
            conn.read(5)
        self.assertIn("65537", str(ctx.exception))
        self.assertFalse(conn.connected)

    def test_a_four_gigabyte_length_prefix_drops_the_link_at_once_without_buffering(self):
        conn = self.conn_to(struct.pack(">I", 0xFFFFFFFF))      # only the prefix, and then the TV keeps quiet
        started = time.monotonic()
        with self.assertRaises(OSError):
            conn.read(5)
        self.assertLess(time.monotonic() - started, 2.0)         # not the 5 s the read was allowed
        self.assertFalse(conn.connected)
        self.assertFalse(conn._buf)
        with self.assertRaises(OSError):
            conn.send("receiver-0", RECV_NS, {"type": "GET_STATUS"})

    def test_junk_after_a_huge_prefix_is_not_kept(self):
        conn = self.conn_to(struct.pack(">I", 0xFFFFFFFF) + b"x" * 200000)
        with self.assertRaises(OSError):
            conn.read(5)
        self.assertFalse(conn._buf)

    def test_an_endless_slow_frame_cannot_keep_the_link_looking_alive(self):
        conn = self.conn_to(struct.pack(">I", 0x7FFFFFFF), b"x" * 10, b"x" * 10, gap=0.05)
        with self.assertRaises(OSError):
            conn.read(5)
        self.assertFalse(conn.connected)

    def test_the_buffer_stays_small_when_a_big_frame_trickles_in(self):
        wire = framed(sized_body(65000))
        conn = self.conn_to(*[wire[i:i + 5000] for i in range(0, len(wire), 5000)], gap=0.01)
        (m,) = conn.read(5, until_first=True)
        self.assertEqual(m.type, "X")
        self.assertFalse(conn._buf)


class Injector(FakeReceiver):
    def inject(self, raw):
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.sendall(raw)
            except OSError:
                pass


class EndToEnd(Base):
    """The native sender against a TV that sends garbage in the middle of a movie."""

    def fake(self, **kw):
        f = Injector(self.media, **kw)
        self.fakes.append(f)
        return f

    def test_malformed_frames_do_not_end_the_cast(self):
        fake, s = self.started()
        for body in HOSTILE.values():
            fake.inject(framed(body))
        time.sleep(1.0)
        log = self.log(s)
        self.assertNotIn("BUG", log)
        self.assertTrue(s.state().alive, s.notice)
        s.toggle_pause()                                                # and it still drives the TV
        self.wait(lambda: fake.session["state"] == "PAUSED", what="pause after the garbage")
        self.assertEqual(len(fake.loads), 1)

    def test_an_oversized_frame_is_a_lost_link_and_the_cast_reconnects(self):
        fake, s = self.started()
        fake.inject(struct.pack(">I", 0xFFFFFFFF))
        self.wait(lambda: "LINK LOST: read failed: frame of 4294967295" in self.log(s), what="the link to be dropped")
        self.wait(lambda: s.notice == "TV reconnected", what="the reconnect")
        self.assertTrue(s.state().alive)
        self.assertEqual(len(fake.loads), 1, "playback must not be touched")
        self.assertNotIn("BUG", self.log(s))


if __name__ == "__main__":
    unittest.main()
