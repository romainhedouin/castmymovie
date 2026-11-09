"""The media server must answer, never hang up, on Range values that int() cannot take, and its OPTIONS reply must be clean."""

import http.client
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie.mediaserver import MediaServer, parse_range  # noqa: E402
from test_mediaserver import make_mp4  # noqa: E402

HUGE = "9" * 5000   # past the 4300-digit limit int() has had since Python 3.11


class RangeParsing(unittest.TestCase):
    def test_numbers_too_long_to_be_real_do_not_match_and_mean_the_whole_file(self):
        for header in (f"bytes=0-{HUGE}", f"bytes={HUGE}-", f"bytes={HUGE}-{HUGE}", f"bytes=-{HUGE}",
                       "bytes=0-" + "9" * 19, "bytes=" + "9" * 19 + "-"):
            self.assertIsNone(parse_range(header, 1000), header[:30])

    def test_the_longest_accepted_number_still_works(self):
        self.assertEqual(parse_range("bytes=0-" + "9" * 18, 1000), (0, 999))
        self.assertEqual(parse_range("bytes=" + "9" * 18 + "-", 1000), "unsatisfiable")

    def test_negative_and_garbage(self):
        for header in ("bytes=-5-9", "bytes=--5", "bytes=a-b", "bytes=1-b", "bytes=a-1", "bytes=0x10-0x20", "bytes=1.5-2"):
            self.assertIsNone(parse_range(header, 1000), header)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.media = make_mp4(self.tmp / "movie.cast.mp4", payload_size=20_000)
        self.data = self.media.read_bytes()
        self.srv = MediaServer(self.media, allow={"127.0.0.1"})
        self.srv.start(host="127.0.0.1")
        self.addCleanup(self.srv.stop)
        self.path = f"/{self.srv.token}.mp4"

    def get(self, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)   # a fresh connection every time
        try:
            c.request(method, self.path, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def test_bad_ranges_get_an_answer_and_the_server_keeps_serving(self):
        n = len(self.data)
        cases = {
            f"bytes=0-{HUGE}": 200,
            f"bytes={HUGE}-": 200,
            f"bytes=-{HUGE}": 200,
            "bytes=-5-9": 200,
            "bytes=-": 200,
            "bytes=a-b": 200,
            f"bytes={n}-": 416,
        }
        for header, want in cases.items():
            with self.subTest(header[:30]):
                status, h, body = self.get(headers={"Range": header})
                self.assertEqual(status, want)
                self.assertEqual(body, self.data if want == 200 else b"")
                status, h, body = self.get(headers={"Range": "bytes=10-19"})   # the next request is still answered
                self.assertEqual((status, body), (206, self.data[10:20]))

    def test_head_with_a_huge_range_is_answered_too(self):
        status, h, body = self.get("HEAD", {"Range": f"bytes=0-{HUGE}"})
        self.assertEqual((status, h["content-length"], body), (200, str(len(self.data)), b""))

    def test_options_has_no_content_length_and_echoes_the_requested_headers(self):
        status, h, body = self.get("OPTIONS", {"Origin": "https://www.gstatic.com",
                                               "Access-Control-Request-Headers": "range, x-goog-api-key, content-type"})
        self.assertEqual((status, body), (204, b""))
        self.assertNotIn("content-length", h)
        self.assertEqual(h["access-control-allow-headers"], "range, x-goog-api-key, content-type")
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertIn("GET", h["access-control-allow-methods"])

    def test_options_without_a_request_for_headers_keeps_the_default_list(self):
        status, h, body = self.get("OPTIONS")
        self.assertEqual(status, 204)
        self.assertNotIn("content-length", h)
        self.assertIn("Range", h["access-control-allow-headers"])

    def test_options_keeps_a_connection_usable(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        self.addCleanup(c.close)
        c.request("OPTIONS", self.path, headers={"Access-Control-Request-Headers": "range"})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (204, b""))
        c.request("GET", self.path, headers={"Range": "bytes=0-9"})   # same connection: the 204 must not have eaten the next reply
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (206, self.data[:10]))


if __name__ == "__main__":
    unittest.main()
