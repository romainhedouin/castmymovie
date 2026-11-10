import json
import struct
import sys
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
        self.assertIsNone(st["media"]["duration"])            # a live stream has no duration
        self.assertEqual(st["media"]["streamType"], "LIVE")

    def test_real_pong_decodes(self):
        self.assertEqual(json.loads(castlink.decode_message(real_frame("heartbeat:PONG"))[6]), {"type": "PONG"})


if __name__ == "__main__":
    unittest.main()
