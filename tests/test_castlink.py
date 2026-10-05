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


class Wire(unittest.TestCase):
    def test_golden_bytes(self):
        # Hand-computed protobuf: 08 00 | 12 01 'a' | 1a 01 'b' | 22 01 'n' | 28 00 | 32 02 '{}'  (17 bytes)
        expected = b"\x00\x00\x00\x11" + b"\x08\x00\x12\x01a\x1a\x01b\x22\x01n\x28\x00\x32\x02{}"
        self.assertEqual(castlink.encode_message("a", "b", "n", {}), expected)

    def test_round_trip(self):
        frame = castlink.encode_message("sender-x", "receiver-0", castlink.CONN_NS, {"type": "CONNECT", "n": 1})
        self.assertEqual(struct.unpack(">I", frame[:4])[0], len(frame) - 4)
        f = castlink.decode_message(frame[4:])
        self.assertEqual((f[2], f[3], f[4]), (b"sender-x", b"receiver-0", castlink.CONN_NS.encode()))
        self.assertEqual(json.loads(f[6]), {"type": "CONNECT", "n": 1})

    def test_long_payloads_use_multibyte_varints(self):
        big = {"text": "x" * 300}
        f = castlink.decode_message(castlink.encode_message("a", "b", "c", big)[4:])
        self.assertEqual(json.loads(f[6]), big)

    def test_unknown_wire_type_is_rejected(self):
        with self.assertRaises(ValueError):
            castlink.decode_message(b"\x0d\x00\x00\x00\x00")  # field 1, wire type 5 (32-bit)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def real_frame(name):
    """A frame captured byte-for-byte from a real Google TV (LAN addresses scrubbed)."""
    return bytes.fromhex(json.loads((FIXTURES / "real_cast_frames.json").read_text())[name])


class RealFrames(unittest.TestCase):
    def test_real_receiver_status_decodes(self):
        f = castlink.decode_message(real_frame("receiver:RECEIVER_STATUS"))
        msg = json.loads(f[6])
        self.assertEqual(msg["type"], "RECEIVER_STATUS")
        app = msg["status"]["applications"][0]
        self.assertEqual(app["displayName"], "Default Media Receiver")
        self.assertEqual(app["transportId"], app["sessionId"])

    def test_real_media_status_decodes_including_null_duration(self):
        msg = json.loads(castlink.decode_message(real_frame("media:MEDIA_STATUS"))[6])
        st = msg["status"][0]
        self.assertEqual(st["playerState"], "PLAYING")
        self.assertIsNone(st["media"]["duration"])            # VLC streams live Matroska: no duration
        self.assertEqual(st["media"]["streamType"], "LIVE")

    def test_real_pong_decodes(self):
        self.assertEqual(json.loads(castlink.decode_message(real_frame("heartbeat:PONG"))[6]), {"type": "PONG"})


class FakeTV:
    """A plain-TCP stand-in for a Cast receiver (no TLS), speaking the real framing."""

    def __init__(self, apps=True, state="PLAYING", close_after=None):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.apps, self.state, self.close_after = apps, state, close_after
        self.position, self.seen = 10.0, []
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        for sock in (self.conn, self.srv):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _reply(self, conn, src, ns, payload):
        conn.sendall(castlink.encode_message(src, castlink.SENDER, ns, payload))

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn = conn
        buf, handled = b"", 0
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while len(buf) >= 4:
                size = struct.unpack(">I", buf[:4])[0]
                if len(buf) < 4 + size:
                    break
                f = castlink.decode_message(buf[4:4 + size])
                buf = buf[4 + size:]
                ns, msg = f[4].decode(), json.loads(f[6])
                self.seen.append((f[3].decode(), ns, msg["type"]))
                handled += 1
                if msg["type"] == "GET_STATUS" and ns == castlink.RECV_NS:
                    apps = [{"transportId": "tid-1", "displayName": "Default Media Receiver"}] if self.apps else []
                    self._reply(conn, "receiver-0", ns, {"type": "RECEIVER_STATUS", "status": {"applications": apps}})
                elif msg["type"] == "GET_STATUS" and ns == castlink.MEDIA_NS:
                    self.position += 1.0
                    self._reply(conn, "tid-1", ns, {"type": "MEDIA_STATUS", "status": [
                        {"currentTime": self.position, "playerState": self.state, "media": {"duration": 600.0}}]})
                elif msg["type"] == "PING":
                    self._reply(conn, "receiver-0", castlink.HEART_NS, {"type": "PONG"})
                if self.close_after and handled >= self.close_after:
                    conn.close()
                    return


def reader_for(tv):
    return castlink.StatusReader("127.0.0.1", tv.port, tls=False)


class ReplayTV(FakeTV):
    """Answers with the real captured frames instead of synthesized ones."""

    def _reply(self, conn, src, ns, payload):
        name = {"RECEIVER_STATUS": "receiver:RECEIVER_STATUS", "MEDIA_STATUS": "media:MEDIA_STATUS",
                "PONG": "heartbeat:PONG"}[payload["type"]]
        body = real_frame(name)
        conn.sendall(struct.pack(">I", len(body)) + body)


class Reader(unittest.TestCase):
    def test_reader_works_against_real_tv_payloads(self):
        tv = ReplayTV()
        self.addCleanup(tv.close)
        r = reader_for(tv)
        self.assertTrue(r.open(timeout=3))
        st = r.poll()
        self.assertEqual(st.state, "PLAYING")
        self.assertAlmostEqual(st.position, 1.946108)
        self.assertIsNone(st.duration)
        r.close()

    def tv(self, **kw):
        tv = FakeTV(**kw)
        self.addCleanup(tv.close)
        return tv

    def test_reads_position_and_state(self):
        tv = self.tv()
        r = reader_for(tv)
        self.assertTrue(r.open(timeout=3))
        self.assertEqual(r.transport, "tid-1")
        st = r.poll()
        self.assertEqual((st.position, st.state, st.duration), (11.0, "PLAYING", 600.0))
        self.assertEqual(r.poll().position, 12.0)
        r.close()

    def test_it_only_ever_asks_never_commands(self):
        tv = self.tv()
        r = reader_for(tv)
        r.open(timeout=3)
        for _ in range(3):
            r.poll()
        r.close()
        time.sleep(0.2)
        kinds = {t for _, _, t in tv.seen}
        self.assertLessEqual(kinds, {"CONNECT", "GET_STATUS", "PING", "PONG"}, kinds)

    def test_paused_and_buffering_states_pass_through(self):
        for state in ("PAUSED", "BUFFERING", "IDLE"):
            tv = self.tv(state=state)
            r = reader_for(tv)
            r.open(timeout=3)
            self.assertEqual(r.poll().state, state)
            r.close()

    def test_no_app_running_means_not_connected(self):
        tv = self.tv(apps=False)
        r = reader_for(tv)
        self.assertFalse(r.open(timeout=1))
        self.assertFalse(r.connected)
        self.assertIsNone(r.poll())

    def test_unreachable_tv_is_a_quiet_false(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        r = castlink.StatusReader("127.0.0.1", dead_port, tls=False)
        self.assertFalse(r.open(timeout=1))
        self.assertIsNone(r.poll())
        r.close()  # safe to call repeatedly
        r.close()

    def test_tv_hanging_up_keeps_the_last_status_and_disconnects(self):
        tv = self.tv(close_after=5)  # CONNECT, GET_STATUS, CONNECT(app), PING, then one media GET_STATUS (answered, then hang up)
        r = reader_for(tv)
        r.open(timeout=3)
        first = r.poll()
        self.assertIsNotNone(first)
        for _ in range(5):
            r.poll()
        self.assertFalse(r.connected)
        self.assertEqual(r.poll(), first)


if __name__ == "__main__":
    unittest.main()
