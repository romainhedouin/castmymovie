import http.client
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from castmymovie import mediaserver  # noqa: E402
from castmymovie.mediaserver import UNSATISFIABLE, MediaServer, parse_range  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402


def make_mp4(path, payload_size=2_000_000):
    """A file shaped like a faststart MP4 (ftyp, moov, mdat) with random media bytes: no ffmpeg needed."""
    payload = os.urandom(payload_size)
    path.write_bytes(b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00" + b"\x00\x00\x00\x10moov" + b"\x00" * 8
                     + (8 + len(payload)).to_bytes(4, "big") + b"mdat" + payload)
    return path


class Ranges(unittest.TestCase):
    def test_the_usual_forms(self):
        self.assertEqual(parse_range("bytes=0-", 1000), (0, 999))
        self.assertEqual(parse_range("bytes=100-199", 1000), (100, 199))
        self.assertEqual(parse_range("bytes=-100", 1000), (900, 999))           # the last 100 bytes
        self.assertEqual(parse_range("bytes=900-5000", 1000), (900, 999))       # clamped to the end
        self.assertEqual(parse_range("bytes=-5000", 1000), (0, 999))            # a suffix longer than the file
        self.assertEqual(parse_range(" bytes = 5 - 9 ", 1000), (5, 9))

    def test_unsatisfiable(self):
        self.assertEqual(parse_range("bytes=1000-", 1000), UNSATISFIABLE)
        self.assertEqual(parse_range("bytes=2000-3000", 1000), UNSATISFIABLE)
        self.assertEqual(parse_range("bytes=-0", 1000), UNSATISFIABLE)
        self.assertEqual(parse_range("bytes=0-", 0), UNSATISFIABLE)              # nothing to give of an empty file

    def test_what_we_do_not_understand_means_the_whole_file(self):
        for header in (None, "", "bytes=0-1,5-9", "items=0-5", "bytes=9-5", "bytes=-", "garbage"):
            self.assertIsNone(parse_range(header, 1000), header)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.media = make_mp4(self.tmp / "My Movie (2001).cast.mp4")
        self.data = self.media.read_bytes()
        self.sub = self.tmp / "My Movie (2001).cast.eng.vtt"
        self.sub.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n")
        self.lines = []
        self.srv = MediaServer(self.media, [self.sub], allow={"127.0.0.1"}, log=self.lines.append)
        self.srv.start(host="127.0.0.1")
        self.addCleanup(self.srv.stop)
        self.path = f"/{self.srv.token}.mp4"

    def get(self, path=None, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            c.request(method, path or self.path, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def booked(self, n):
        """Wait until the server has booked its n-th request. It does that (counters, recent, log) a moment after
        the client already holds the last byte, so a test must not read that state straight after get()."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sum(" -> " in line for line in list(self.lines)) >= n:
                return
            time.sleep(0.01)
        self.fail(f"the server never booked request {n}: {self.lines}")

    def test_whole_file_with_the_headers_a_cast_receiver_needs(self):
        status, h, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)
        self.assertEqual(h["content-length"], str(len(self.data)))
        self.assertEqual(h["content-type"], "video/mp4")
        self.assertEqual(h["accept-ranges"], "bytes")
        self.assertEqual(h["access-control-allow-origin"], "*")   # the receiver is a web page on Google's origin

    def test_ranges(self):
        n = len(self.data)
        status, h, body = self.get(headers={"Range": "bytes=1000-1999"})
        self.assertEqual((status, h["content-range"], h["content-length"], body),
                         (206, f"bytes 1000-1999/{n}", "1000", self.data[1000:2000]))
        status, h, body = self.get(headers={"Range": "bytes=1500000-"})
        self.assertEqual((status, body), (206, self.data[1500000:]))
        status, h, body = self.get(headers={"Range": "bytes=-10"})
        self.assertEqual((status, h["content-range"], body), (206, f"bytes {n - 10}-{n - 1}/{n}", self.data[-10:]))
        status, h, body = self.get(headers={"Range": f"bytes={n}-"})
        self.assertEqual((status, h["content-range"], body), (416, f"bytes */{n}", b""))
        status, h, body = self.get(headers={"Range": "bytes=0-1,5-9"})            # several ranges: all of it
        self.assertEqual((status, body), (200, self.data))

    def test_head_and_options(self):
        status, h, body = self.get(method="HEAD", headers={"Range": "bytes=10-19"})
        self.assertEqual((status, h["content-length"], body), (206, "10", b""))
        status, h, body = self.get(method="OPTIONS", headers={"Origin": "https://www.gstatic.com",
                                                              "Access-Control-Request-Headers": "range"})
        self.assertEqual(status, 204)
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertEqual(h["access-control-allow-headers"], "range")   # what the receiver asked for is what it is told
        self.assertNotIn("content-length", h)                           # a 204 carries none
        self.assertIn("GET", h["access-control-allow-methods"])

    def test_subtitles_are_served_as_webvtt(self):
        status, h, body = self.get(self.srv.subtitle_paths[0])
        self.assertEqual((status, body), (200, self.sub.read_bytes()))
        self.assertTrue(h["content-type"].startswith("text/vtt"))
        self.assertEqual(h["access-control-allow-origin"], "*")

    def test_only_the_token_path_exists_and_it_does_not_reveal_the_file_name(self):
        self.assertNotIn("Movie", self.srv.url("10.0.0.2"))
        self.assertGreaterEqual(len(self.srv.token), 20)
        self.assertNotEqual(MediaServer(self.media).token, self.srv.token)
        for path in ("/", "/My%20Movie%20(2001).cast.mp4", f"/{self.srv.token}", "/../etc/passwd", "/x.mp4"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get(self.path + "?cache=1")[0], 200)

    def test_anyone_but_the_tv_is_hung_up_on(self):
        srv = MediaServer(self.media, allow={"192.0.2.10"}, log=self.lines.append)   # the TV is somewhere else
        srv.start(host="127.0.0.1")
        self.addCleanup(srv.stop)
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
            c.request("GET", f"/{srv.token}.mp4")
            c.getresponse().read()
        c.close()
        self.assertEqual(srv.refused, {"127.0.0.1": 1})
        self.assertEqual((srv.bytes_served, srv.media_requests), (0, 0))
        self.assertTrue(any("refused" in line for line in self.lines))

    def test_counters_and_log(self):
        self.get(headers={"Range": "bytes=0-99"})
        self.booked(1)
        self.get(headers={"Range": "bytes=1000000-1000999"})
        self.booked(2)
        self.assertEqual(self.srv.bytes_served, 1100)
        self.assertEqual(self.srv.media_requests, 2)
        self.assertEqual(self.srv.reach, 1001000)            # where the latest download got to
        self.assertEqual(self.srv.furthest, 1001000)
        self.assertEqual(self.srv.payload_start, 40)          # ftyp 16 + moov 16 + mdat header 8
        last = self.srv.recent[-1]
        self.assertEqual((last.status, last.first, last.sent, last.aborted), (206, 1000000, 1000, False))
        self.get(headers={"Range": "bytes=10-19"})            # a seek backwards
        self.booked(3)
        self.assertEqual((self.srv.reach, self.srv.furthest), (20, 1001000))
        self.assertTrue(any("Range=bytes=1000000-1000999 -> 206" in line for line in self.lines), self.lines)

    def test_a_tv_hanging_up_mid_download_is_quiet_and_harmless(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
        s.recv(1000)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # reset, not FIN
        s.close()
        deadline = time.monotonic() + 5
        while not self.srv.recent and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.srv.recent[-1].aborted)
        self.assertEqual(self.get(headers={"Range": "bytes=0-9"})[2], self.data[:10])   # still serving

    def test_many_parallel_range_requests(self):
        results, errors = {}, []

        def fetch(i):
            try:
                first = i * 100_000
                results[i] = self.get(headers={"Range": f"bytes={first}-{first + 99_999}"})[2] == \
                    self.data[first:first + 100_000]
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(16)))
        self.assertTrue(all(results.values()))

    def test_keep_alive_serves_several_requests_on_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        for first in (0, 500, 1_000_000):
            c.request("GET", self.path, headers={"Range": f"bytes={first}-{first + 9}"})
            self.assertEqual(c.getresponse().read(), self.data[first:first + 10])
        c.close()

    def test_stop_ends_running_downloads(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.sendall(f"GET {self.path} HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
        s.recv(100)                       # the download has begun, and the client stops reading (a full TV buffer)
        time.sleep(0.2)
        self.srv.stop()
        s.settimeout(5)
        got = 0
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                got += len(d)
        except OSError:
            pass
        s.close()
        self.assertLess(got, len(self.data), "the server must hang up instead of finishing the movie")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.srv.port), timeout=1).close()
        self.srv.stop()                   # twice is fine

    def test_chunks_are_large(self):
        self.assertGreaterEqual(mediaserver.CHUNK, 256 * 1024)
        self.assertLessEqual(mediaserver.CHUNK, 1024 * 1024)


class FakeTimeline:
    """The part of onthefly.Timeline the server uses: 6 s segments, the last one shorter."""

    def __init__(self, duration=27.0, seg_len=6.0):
        self.duration, self.seg_len = duration, seg_len
        self.count = int(-(-duration // seg_len))

    def start(self, i):
        return i * self.seg_len

    def end(self, i):
        return min((i + 1) * self.seg_len, self.duration)

    def index_at(self, t):
        return max(0, min(int(t // self.seg_len), self.count - 1))

    def playlist(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{int(self.seg_len)}", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-PLAYLIST-TYPE:VOD"]
        for i in range(self.count):
            lines += [f"#EXTINF:{self.end(i) - self.start(i):.3f},", f"seg{i:05d}.ts"]
        return ("\n".join(lines + ["#EXT-X-ENDLIST", ""])).encode()


class FakeStream:
    """An OnTheFly double: segments exist once publish()ed, segment() blocks like the real one."""

    def __init__(self, tmp, timeline=None):
        self.tmp = Path(tmp)
        self.timeline = timeline or FakeTimeline()
        self.error = None
        self.asked = []
        self._ready = {}
        self._cond = threading.Condition()

    def publish(self, n, data=None):
        data = os.urandom(5000) if data is None else data
        path = self.tmp / f"seg{n:05d}.ts"
        path.write_bytes(data)
        with self._cond:
            self._ready[n] = path
            self._cond.notify_all()
        return data

    def fail(self, error):
        with self._cond:
            self.error = error
            self._cond.notify_all()

    def begin(self, start_seconds):
        pass

    def wait_ready(self, segments, timeout):
        pass

    def segment(self, n, timeout):
        self.asked.append(n)
        deadline = time.monotonic() + timeout
        with self._cond:
            while n not in self._ready and not self.error:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._ready.get(n)

    def close(self):
        pass


class Streaming(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stream = FakeStream(self.tmp)
        self.lines = []
        self.media = self.tmp / "source.mkv"       # never created: the server must not open it when streaming
        self.srv = MediaServer(self.media, allow={"127.0.0.1"}, log=self.lines.append, stream=self.stream)
        self.srv.SEGMENT_WAIT = 0.4
        self.srv.start(host="127.0.0.1")
        self.addCleanup(self.srv.stop)
        self.base = f"/{self.srv.token}"

    def get(self, path, method="GET", headers=None, timeout=5):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=timeout)
        try:
            c.request(method, path, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def booked(self, n):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if sum(" -> " in line for line in list(self.lines)) >= n:
                return
            time.sleep(0.01)
        self.fail(f"the server never booked request {n}: {self.lines}")

    def raw(self, path):
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
        return s

    def test_urls_and_no_file_in_sight(self):
        self.assertEqual(self.srv.stream_url("10.0.0.2"), f"http://10.0.0.2:{self.srv.port}{self.base}/index.m3u8")
        self.assertIsNone(self.srv.payload_start)
        self.assertEqual((self.srv.reach, self.srv.furthest), (None, 0))
        self.assertEqual(self.get(f"{self.base}.mp4")[0], 404)     # the source file is not offered

    def test_playlist(self):
        status, h, body = self.get(f"{self.base}/index.m3u8")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.stream.timeline.playlist())
        self.assertEqual(h["content-type"], "application/x-mpegURL")
        self.assertEqual(h["content-length"], str(len(body)))
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertIn(b"#EXT-X-ENDLIST", body)
        self.assertEqual(self.get(f"{self.base}/index.m3u8?x=1")[0], 200)
        self.assertEqual(self.get(f"{self.base}/index.m3u8", method="HEAD")[2], b"")

    def test_segment_with_its_headers(self):
        data = self.stream.publish(2)
        status, h, body = self.get(f"{self.base}/seg00002.ts")
        self.assertEqual((status, body), (200, data))
        self.assertEqual(h["content-type"], "video/mp2t")
        self.assertEqual(h["content-length"], str(len(data)))
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertEqual(h["accept-ranges"], "bytes")

    def test_a_segment_that_appears_later_is_served_once_ready(self):
        data = os.urandom(3000)
        threading.Timer(0.25, self.stream.publish, (1, data)).start()
        self.srv.SEGMENT_WAIT = 10
        t0 = time.monotonic()
        status, h, body = self.get(f"{self.base}/seg00001.ts")
        self.assertEqual((status, body), (200, data))
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        self.assertLess(time.monotonic() - t0, 3)

    def test_an_index_past_the_end_is_404_and_so_is_a_misspelt_name(self):
        count = self.stream.timeline.count
        self.stream.publish(count)    # even if a file somehow existed
        for name in (f"seg{count:05d}.ts", "seg99999.ts", "seg1.ts", "seg000001.ts", "seg00001.mp4", "index.m3u9", "seg-0001.ts"):
            self.assertEqual(self.get(f"{self.base}/{name}")[0], 404, name)
        self.assertEqual(self.get("/other-token/index.m3u8")[0], 404)
        self.assertEqual(self.get("/index.m3u8")[0], 404)
        self.assertNotIn(count, self.stream.asked)     # the encoder is not asked for what does not exist

    def test_a_segment_the_encoder_does_not_deliver_is_503_with_retry_after(self):
        self.srv.SEGMENT_WAIT = 0.3
        t0 = time.monotonic()
        status, h, body = self.get(f"{self.base}/seg00003.ts")
        self.assertEqual((status, h["retry-after"], h["content-length"], body), (503, "2", "0", b""))
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertIn(3, self.stream.asked)
        self.booked(1)
        self.assertEqual(self.srv.recent[-1].status, 503)
        self.assertEqual(self.srv.fetched, set())

    def test_a_dead_encoder_is_503_at_once_and_the_log_says_why(self):
        self.stream.fail("ffmpeg: Conversion failed!")
        self.srv.SEGMENT_WAIT = 30
        t0 = time.monotonic()
        status, h, _ = self.get(f"{self.base}/seg00000.ts")
        self.assertEqual((status, h["retry-after"]), (503, "2"))
        self.assertLess(time.monotonic() - t0, 3)
        self.booked(1)
        self.assertTrue(any("503" in line and "Conversion failed" in line for line in self.lines), self.lines)

    def test_only_the_tv_and_only_the_token_path(self):
        self.stream.publish(0)
        srv = MediaServer(self.media, allow={"192.0.2.10"}, log=self.lines.append, stream=self.stream)   # the TV is elsewhere
        srv.start(host="127.0.0.1")
        self.addCleanup(srv.stop)
        for path in (f"/{srv.token}/index.m3u8", f"/{srv.token}/seg00000.ts"):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
                c.request("GET", path)
                c.getresponse().read()
            c.close()
        self.assertEqual(srv.refused, {"127.0.0.1": 2})
        self.assertEqual(srv.media_requests, 0)
        # the other token's server never serves ours, and the routes do not exist without a stream
        self.assertEqual(self.get(f"/{srv.token}/index.m3u8")[0], 404)
        self.assertEqual(self.get(f"/{srv.token}/seg00000.ts")[0], 404)
        plain = MediaServer(make_mp4(self.tmp / "plain.mp4", 1000), allow={"127.0.0.1"}, log=self.lines.append)
        plain.start(host="127.0.0.1")
        self.addCleanup(plain.stop)
        self.assertEqual(plain.stream_url("h"), f"http://h:{plain.port}/{plain.token}/index.m3u8")
        c = http.client.HTTPConnection("127.0.0.1", plain.port, timeout=5)
        c.request("GET", f"/{plain.token}/index.m3u8")
        self.assertEqual(c.getresponse().status, 404)
        c.close()
        self.assertEqual(plain.buffered_until(7.5), 7.5)

    def test_range_on_a_segment(self):
        data = self.stream.publish(1, os.urandom(10_000))
        status, h, body = self.get(f"{self.base}/seg00001.ts", headers={"Range": "bytes=100-199"})
        self.assertEqual((status, h["content-range"], h["content-length"], body), (206, "bytes 100-199/10000", "100", data[100:200]))
        status, h, body = self.get(f"{self.base}/seg00001.ts", headers={"Range": "bytes=-10"})
        self.assertEqual((status, body), (206, data[-10:]))
        status, h, body = self.get(f"{self.base}/seg00001.ts", headers={"Range": "bytes=10000-"})
        self.assertEqual((status, h["content-range"]), (416, "bytes */10000"))
        status, h, body = self.get(f"{self.base}/index.m3u8", headers={"Range": "bytes=0-6"})
        self.assertEqual((status, body), (206, b"#EXTM3U"))

    def test_fetched_and_buffered_until(self):
        for i in range(5):
            self.stream.publish(i)
        srv = self.srv
        self.assertEqual(srv.buffered_until(3.0), 3.0)            # nothing delivered yet
        for n, i in enumerate((0, 1, 3), 1):
            self.get(f"{self.base}/seg{i:05d}.ts")
            self.booked(n)
        self.assertEqual(srv.fetched, {0, 1, 3})
        self.assertEqual(srv.buffered_until(0.0), 12.0)           # 0 and 1, then a gap
        self.assertEqual(srv.buffered_until(11.9), 12.0)
        self.assertEqual(srv.buffered_until(14.0), 14.0)          # segment 2 is missing
        self.assertEqual(srv.buffered_until(20.0), 24.0)          # segment 3 alone
        self.get(f"{self.base}/seg00002.ts")
        self.booked(4)
        self.assertEqual(srv.buffered_until(0.0), 24.0)           # the gap is closed
        self.get(f"{self.base}/seg00004.ts")                      # the short last segment ends at the duration
        self.booked(5)
        self.assertEqual(srv.buffered_until(6.0), 27.0)
        self.assertEqual(srv.buffered_until(100.0), 27.0)         # past the end clamps to the last segment

    def test_a_partial_or_head_request_does_not_count_as_fetched(self):
        self.stream.publish(0, os.urandom(10_000))
        self.get(f"{self.base}/seg00000.ts", headers={"Range": "bytes=0-99"})
        self.booked(1)
        self.get(f"{self.base}/seg00000.ts", method="HEAD")
        self.booked(2)
        self.assertEqual(self.srv.fetched, set())
        self.get(f"{self.base}/seg00000.ts", headers={"Range": "bytes=0-"})   # all of it, as a range
        self.booked(3)
        self.assertEqual(self.srv.fetched, {0})

    def test_a_download_the_tv_hangs_up_is_not_fetched(self):
        self.stream.publish(0, os.urandom(8_000_000))
        s = socket.create_connection(("127.0.0.1", self.srv.port))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.sendall(f"GET {self.base}/seg00000.ts HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
        s.recv(1000)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
        deadline = time.monotonic() + 5
        while not self.srv.recent and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.srv.recent[-1].aborted)
        self.assertEqual(self.srv.fetched, set())
        self.assertEqual(self.srv.buffered_until(1.0), 1.0)

    def test_media_requests_count_the_playlist_and_the_segments(self):
        self.stream.publish(0)
        self.get(f"{self.base}/index.m3u8")
        self.booked(1)
        self.get(f"{self.base}/seg00000.ts")
        self.booked(2)
        self.assertEqual(self.srv.media_requests, 2)
        self.get(f"{self.base}/seg00000.ts", method="HEAD")           # not a download
        self.get(f"{self.base}/seg00077.ts")                          # 404: the TV asked for nothing real
        self.booked(4)
        self.assertEqual(self.srv.media_requests, 2)
        self.get(f"{self.base}/index.m3u8")
        self.booked(5)
        self.assertEqual(self.srv.media_requests, 3)
        self.assertTrue(any("GET playlist" in line for line in self.lines), self.lines)
        self.assertTrue(any("GET segment 0 " in line for line in self.lines), self.lines)
        self.assertEqual(self.srv.bytes_served, 5000 + len(self.stream.timeline.playlist()) * 2)

    def test_stop_ends_a_request_that_is_waiting_for_a_segment(self):
        self.srv.SEGMENT_WAIT = 60
        s = self.raw(f"{self.base}/seg00002.ts")
        deadline = time.monotonic() + 5
        while 2 not in self.stream.asked and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(2, self.stream.asked)
        t0 = time.monotonic()
        self.srv.stop()
        self.assertLess(time.monotonic() - t0, 3)
        s.settimeout(5)
        try:
            self.assertEqual(s.recv(100), b"")       # hung up, nothing sent
        except OSError:
            pass
        s.close()
        deadline = time.monotonic() + 3
        while not self.srv.recent and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.srv.recent and self.srv.recent[-1].aborted)    # the handler thread came back

    def test_a_tv_that_hangs_up_while_waiting_frees_the_thread(self):
        self.srv.SEGMENT_WAIT = 60
        s = self.raw(f"{self.base}/seg00002.ts")
        time.sleep(0.5)
        s.close()
        deadline = time.monotonic() + 5
        while not self.srv.recent and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.srv.recent, "the request kept waiting for a TV that is gone")
        self.assertTrue(self.srv.recent[-1].aborted)
        self.assertEqual(self.srv.fetched, set())
        self.stream.publish(2)
        self.assertEqual(self.get(f"{self.base}/seg00002.ts")[0], 200)      # and the server is fine

    def test_a_fake_tv_loading_at_20_s_fetches_the_playlist_then_segment_3(self):
        for i in range(5):
            self.stream.publish(i)
        tv = FakeReceiver()
        self.addCleanup(tv.close)
        media = {"contentId": self.srv.stream_url("127.0.0.1"), "contentType": "application/x-mpegURL",
                 "streamType": "BUFFERED", "duration": 27.0}
        self.assertTrue(tv._download(media, 20.0), tv.fetch_errors)
        self.assertEqual([f[0] for f in tv.fetches], ["playlist", "seg00003.ts", "seg00004.ts"])
        self.assertTrue(all(f[3] for f in tv.fetches), tv.fetches)
        self.assertTrue(tv.playlist["ended"])
        self.assertEqual([round(seg[0], 3) for seg in tv.playlist["segments"]], [0.0, 6.0, 12.0, 18.0, 24.0])   # summed EXTINF
        self.assertTrue(tv.playlist["segments"][3][2].endswith(f"{self.base}/seg00003.ts"))
        self.booked(3)
        self.assertEqual(self.srv.fetched, {3, 4})
        before = len(tv.fetches)
        self.assertTrue(tv._download(media, 2.0, seek=True))                # a SEEK fetches the segments at the new position
        self.assertEqual([f[0] for f in tv.fetches[before:]], ["seg00000.ts", "seg00001.ts"])


if __name__ == "__main__":
    unittest.main()
