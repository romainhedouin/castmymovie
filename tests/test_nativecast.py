"""The native sender against a fake TV (tests/fakereceiver.py) on 127.0.0.1: start, controls, and recovery."""

import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine, nativecast  # noqa: E402
from castmymovie.castlink import MEDIA_NS, RECV_NS  # noqa: E402
from castmymovie.mediaserver import MediaServer  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402
from test_mediaserver import make_mp4  # noqa: E402


class QuickCast(nativecast.NativeCast):
    """The real thing with its clocks sped up, so recovery can be tested in seconds."""
    PING_EVERY = 0.5
    POLL_EVERY = 0.3
    DEAD_AFTER = 1.5
    CONNECT_TIMEOUT = 2.0
    REPLY_TIMEOUT = 2.0
    LAUNCH_TIMEOUT = 2.0
    BACKOFF = (0.1, 0.2, 0.4)
    GIVE_UP_AFTER = 3.0
    RELOAD_GAP = 0.2


class FakeTimeline:
    """The part of onthefly.Timeline the server reads: 6 s segments, a short last one."""
    seg_len = 6.0

    def __init__(self, duration=600.0):
        self.duration, self.count = duration, int(-(-duration // 6))

    def start(self, i):
        return i * 6.0

    def end(self, i):
        return min((i + 1) * 6.0, self.duration)

    def segment_name(self, i):
        return f"seg{i:05d}.ts"

    def index_at(self, t):
        return min(max(int(t // 6), 0), self.count - 1)

    def playlist(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-TARGETDURATION:6"]
        for i in range(self.count):
            lines += [f"#EXTINF:{self.end(i) - self.start(i):.3f},", f"seg{i:05d}.ts"]
        return ("\n".join(lines + ["#EXT-X-ENDLIST", ""])).encode()


class FakeStream:
    """An onthefly.OnTheFly that encodes nothing: the same public surface, with every call recorded."""

    def __init__(self, folder, duration=600.0, ready_error=None, begin_error=None):
        self.timeline, self.error, self.folder = FakeTimeline(duration), None, Path(folder)
        self.ready_error, self.begin_error = ready_error, begin_error
        self.began, self.waited, self.closed, self.events = [], [], 0, []

    def begin(self, start_seconds):
        if self.begin_error:
            raise self.begin_error
        self.began.append(start_seconds)

    def wait_ready(self, segments, timeout):
        self.waited.append((segments, timeout))
        if self.ready_error:
            raise self.ready_error

    def segment(self, n, timeout):
        path = self.folder / self.timeline.segment_name(n)
        path.write_bytes(bytes([n % 256]) * 1000)
        return path

    def status(self):
        return SimpleNamespace(frontier_s=1234.0, speed=8.4, slow=False)

    def close(self):
        self.closed += 1
        self.events.append("stream closed")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.media = make_mp4(self.tmp / "Night of the Fake TV.cast.mp4")
        self.fakes, self.sessions = [], []
        # every test writes its own log, and must never touch the real one
        patcher = mock.patch.object(nativecast.tempfile, "gettempdir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for s in self.sessions:
            s.stop()
        for f in self.fakes:
            f.close()

    def fake(self, **kw):
        f = FakeReceiver(self.media, **kw)
        self.fakes.append(f)
        return f

    def session(self, fake, start=100.0, cls=QuickCast, port=None, **kw):
        rec = cast.Receiver("Fake TV", "127.0.0.1", port=port or fake.port)
        s = cls(rec, self.media, start, tls=False, duration=600.0, **kw)
        self.sessions.append(s)
        return s

    def started(self, start=100.0, **kw):
        fake = self.fake(**kw)
        s = self.session(fake, start)
        s.start(timeout=10)
        return fake, s

    def wait(self, predicate, timeout=8.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def log(self, s):
        return s.log_path.read_text()


class Start(Base):
    def test_launch_load_and_play(self):
        fake, s = self.started(start=100.0)
        self.assertEqual(fake.kinds(RECV_NS)[:2], ["GET_STATUS", "LAUNCH"])
        load = fake.loads[0]
        media = load["media"]
        self.assertEqual((media["contentType"], media["streamType"]), ("video/mp4", "BUFFERED"))
        self.assertEqual((load["currentTime"], load["autoplay"], media["duration"]), (100.0, True, 600.0))
        self.assertEqual(media["metadata"]["title"], "Night of the Fake TV.cast.mp4")
        self.assertNotIn("Night", media["contentId"])                    # the URL never shows the file name
        self.assertTrue(media["contentId"].startswith(f"http://127.0.0.1:{s.server.port}/"))
        self.assertTrue(fake.fetches and all(ok for *_, ok in fake.fetches), fake.fetches)  # the bytes the TV got are right
        self.assertEqual(fake.fetches[0][1], 206)
        st = s.state()
        self.assertTrue(st.alive and not st.paused and not st.buffering)
        self.assertEqual(st.length, 600.0)
        self.assertGreaterEqual(st.position, 100.0)

    def test_an_already_running_media_player_is_reused(self):
        fake, s = self.started(app_running=True)
        self.assertNotIn("LAUNCH", fake.kinds())
        self.assertEqual(len(fake.loads), 1)

    def test_every_run_is_a_different_sender(self):
        fake = self.fake()
        self.assertNotEqual(self.session(fake).sender, self.session(fake).sender)

    def test_subtitles_are_offered_and_switched_on(self):
        vtt = self.tmp / "Night of the Fake TV.cast.eng.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
        fake = self.fake()
        s = self.session(fake, subtitles=[vtt])
        s.start(timeout=10)
        load = fake.loads[0]
        track = load["media"]["tracks"][0]
        self.assertEqual((track["type"], track["trackContentType"], track["language"]), ("TEXT", "text/vtt", "en"))
        self.assertEqual(load["activeTrackIds"], [1])
        status, body = fetch(track["trackContentId"])
        self.assertEqual((status, body), (200, vtt.read_bytes()))

    def test_unreachable_tv(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead = sock.getsockname()[1]
        s = self.session(None, port=dead)
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertIn("Could not reach Fake TV", str(cm.exception))
        self.assertIn("same Wi-Fi", str(cm.exception))
        self.assertIn(str(s.log_path), str(cm.exception))

    def test_tv_refuses_to_open_its_player(self):
        fake = self.fake(launch_error="NOT_ALLOWED")
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=5)
        self.assertIn("refused to open its Cast media player (NOT_ALLOWED)", str(cm.exception))

    def test_tv_that_never_opens_its_player(self):
        fake = self.fake(launch=False)
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=5)
        self.assertIn("did not open its Cast media player within 2 s", str(cm.exception))   # QuickCast.LAUNCH_TIMEOUT

    def test_load_failed_without_a_download_points_at_the_firewall(self):
        fake = self.fake(load_error="LOAD_FAILED")
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=5)
        msg = str(cm.exception)
        self.assertIn("LOAD_FAILED", msg)
        self.assertIn("never downloaded", msg)
        self.assertIn("accept incoming network connections", msg)
        self.assertIn("STOP", fake.kinds(RECV_NS), "the TV must leave the Cast screen")

    def test_load_failed_after_a_download_points_at_the_format(self):
        fake = self.fake(fail_load_after_fetch=True)
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=5)
        msg = str(cm.exception)
        self.assertIn("could not play this movie (LOAD_FAILED 104)", msg)
        self.assertIn("the TV probably cannot decode this video or audio format", msg)
        self.assertNotIn("VLC", msg)

    def test_a_tv_that_never_comes_to_download_explains_the_firewall(self):
        fake = self.fake(fetch=False)
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=2)
        msg = str(cm.exception)
        self.assertIn("never came to download it", msg)
        self.assertIn("Allow", msg)
        self.assertIn("STOP", fake.kinds(MEDIA_NS) + fake.kinds(RECV_NS))

    def test_a_tv_that_downloads_but_never_plays_blames_the_wifi(self):
        fake = self.fake(play=False)
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=2)
        self.assertIn("did not start playing within 2 s", str(cm.exception))

    def test_a_tv_fetching_from_another_address_is_named(self):
        def refuse(srv, client):  # as if the TV's downloads came from 10.9.9.9, not the address it was found at
            srv.refused["10.9.9.9"] = 1
            return False
        fake = self.fake()
        with mock.patch.object(nativecast.MediaServer, "admit", refuse):
            with self.assertRaises(engine.CastError) as cm:
                self.session(fake).start(timeout=5)
        self.assertIn("never downloaded any of it", str(cm.exception))
        self.assertIn("turned away 10.9.9.9", str(cm.exception))

    def test_stop_is_idempotent_and_safe_after_a_failure(self):
        fake = self.fake(load_error="LOAD_FAILED")
        s = self.session(fake)
        with self.assertRaises(engine.CastError):
            s.start(timeout=5)
        s.stop()
        s.stop()
        self.assertFalse(s.state().alive)
        never_started = self.session(fake)
        never_started.stop()                  # before start(): nothing to do, no error
        never_started.stop()


class OnTheFly(Base):
    """A cast of a movie that is still being converted: the TV gets an HLS playlist, the encoder is the stream."""

    def make_stream(self, **kw):
        return FakeStream(self.tmp, **kw)

    def streamed(self, start=100.0, **kw):
        self.stream = self.make_stream()
        fake = self.fake(**kw)
        s = self.session(fake, start, stream=self.stream)
        return fake, s

    def stub_session(self, stream):
        """A session that was never started, with a stand-in server: for what does not need a TV."""
        s = self.session(None, port=1, stream=stream)
        s._tv_ip, s._transport, s._app_session = "127.0.0.1", "web-7", "app-1"
        self.sent = []
        s._send = lambda dest, ns, payload: self.sent.append(payload) or 7
        s.server = SimpleNamespace(url=lambda host, path=None: f"http://{host}:1/tok.mp4",
                                   stream_url=lambda host: f"http://{host}:1/tok/index.m3u8", subtitle_paths=[],
                                   token="tok", bytes_served=5000, reach=None, payload_start=None, media_requests=0,
                                   buffered_until=lambda pos: pos + 30.0, stop=lambda: None)
        return s

    def test_start_begins_the_encoder_waits_for_two_segments_and_plays_the_playlist(self):
        fake, s = self.streamed(start=100.0)
        s.start(timeout=10)
        self.assertEqual(self.stream.began, [100.0])
        self.assertEqual(self.stream.waited, [(2, 60.0)])
        media = fake.loads[0]["media"]
        self.assertEqual((media["contentType"], media["streamType"]), ("application/x-mpegURL", "BUFFERED"))
        self.assertTrue(media["contentId"].endswith("/index.m3u8"))
        self.assertEqual([name for name, *_ in fake.fetches], ["playlist", "seg00016.ts", "seg00017.ts"])
        self.assertTrue(all(ok for *_, ok in fake.fetches), fake.fetches)   # 100 s is in the 17th segment
        self.assertEqual(media["duration"], 600.0)
        self.assertEqual(media["metadata"]["title"], "Night of the Fake TV.cast.mp4")
        self.assertTrue(s.state().alive)
        text = self.log(s)
        self.assertIn("converted on the fly", text)
        self.assertNotIn("bytes,", text.split("\n")[0])                 # no file size: there is no file
        self.assertIn("the playlist and segments of the conversion", text)
        self.assertNotIn("movie data starts at byte", text)

    def test_the_load_names_the_hls_segment_formats(self):
        s = self.stub_session(stream=self.make_stream())
        s._load(100.0)
        media = self.sent[0]["media"]
        self.assertEqual(media["contentUrl"], "http://127.0.0.1:1/tok/index.m3u8")
        self.assertEqual(media["contentId"], media["contentUrl"])
        self.assertEqual((media["contentType"], media["streamType"]), (nativecast.HLS_CONTENT_TYPE, "BUFFERED"))
        self.assertEqual((media["hlsSegmentFormat"], media["hlsVideoSegmentFormat"]), ("ts", "mpeg2_ts"))
        self.assertEqual(media["duration"], 600.0)
        self.assertEqual((self.sent[0]["currentTime"], self.sent[0]["autoplay"]), (100.0, True))

    def test_a_segment_format_can_be_overridden_or_dropped(self):
        s = self.stub_session(stream=self.make_stream())
        with mock.patch.dict(os.environ, {"CASTMYMOVIE_HLS_SEGMENT_FORMAT": "ts_aac",
                                          "CASTMYMOVIE_HLS_VIDEO_SEGMENT_FORMAT": ""}):
            s._load(0.0)
        media = self.sent[-1]["media"]
        self.assertEqual(media["hlsSegmentFormat"], "ts_aac")
        self.assertNotIn("hlsVideoSegmentFormat", media, "an empty value leaves the field out")
        with mock.patch.object(nativecast, "HLS_SEGMENT_FORMAT", ""), \
                mock.patch.object(nativecast, "HLS_VIDEO_SEGMENT_FORMAT", "fmp4"):
            s._load(0.0)
        media = self.sent[-1]["media"]
        self.assertNotIn("hlsSegmentFormat", media)
        self.assertEqual(media["hlsVideoSegmentFormat"], "fmp4")
        with mock.patch.dict(os.environ, {"CASTMYMOVIE_HLS_SEGMENT_FORMAT": "ts"}):
            s._load(0.0)
        self.assertEqual(self.sent[-1]["media"]["hlsSegmentFormat"], "ts", "the variable beats the constant")

    def test_a_file_cast_has_no_hls_fields(self):
        s = self.stub_session(stream=None)
        with mock.patch.dict(os.environ, {"CASTMYMOVIE_HLS_SEGMENT_FORMAT": "ts"}):
            s._load(0.0)
        media = self.sent[0]["media"]
        self.assertEqual((media["contentType"], media["contentUrl"]), ("video/mp4", "http://127.0.0.1:1/tok.mp4"))
        self.assertFalse([k for k in media if k.startswith("hls")])

    def test_the_load_works_against_the_real_server_playlist(self):
        stream = self.make_stream()
        server = MediaServer(self.media, allow={"127.0.0.1"}, stream=stream)
        server.start()
        self.addCleanup(server.stop)
        s = self.stub_session(stream=stream)
        s.server = server
        s._load(0.0)
        url = self.sent[0]["media"]["contentUrl"]
        self.assertEqual(url, server.stream_url("127.0.0.1"))
        self.assertEqual(fetch(url), (200, stream.timeline.playlist()))

    def test_the_link_sample_says_how_far_ahead_the_tv_is(self):
        s = self.stub_session(stream=self.make_stream())
        s._pb = nativecast.Playback("PAUSED", 100.0, 0.0)
        sample = s.link_sample()
        self.assertEqual(sample.ahead, 30.0)                          # buffered_until(100) = 130
        self.assertEqual(sample.bytes_out, 5000)
        self.assertIsNone(sample.offset)
        self.assertIsNone(sample.retx)
        self.assertIsNone(sample.rtt_ms)
        s.server.buffered_until = lambda pos: pos - 3.0               # nothing fetched at the position: never negative
        self.assertEqual(s.link_sample().ahead, 0.0)

    def test_the_position_used_for_ahead_keeps_running_while_playing(self):
        s = self.stub_session(stream=self.make_stream())
        asked = []
        s.server.buffered_until = lambda pos: asked.append(pos) or pos
        s._pb = nativecast.Playback("PLAYING", 100.0, time.monotonic() - 10)
        s.link_sample()
        self.assertAlmostEqual(asked[0], 110.0, delta=0.5)

    def test_a_file_cast_has_no_ahead(self):
        s = self.stub_session(stream=None)
        self.assertIsNone(s.link_sample().ahead)

    def test_a_converter_that_does_not_get_ready_fails_the_start_and_is_closed(self):
        stream = self.make_stream(ready_error=engine.CastError("The converter stopped: Conversion failed!"))
        s = self.session(self.fake(), stream=stream)
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertEqual(str(cm.exception), "The converter stopped: Conversion failed!")
        self.assertEqual(stream.closed, 1)
        self.assertIsNone(s.server, "no server for a movie that cannot play")
        self.assertIn("START FAILED: The converter stopped", self.log(s))
        self.assertFalse(s.state().alive)

    def test_a_converter_that_cannot_even_start_fails_the_start(self):
        stream = self.make_stream(begin_error=FileNotFoundError(2, "No such file or directory"))
        s = self.session(self.fake(), stream=stream)
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertIn("Could not start the converter (No such file or directory)", str(cm.exception))
        self.assertEqual(stream.closed, 1)

    def test_no_conversion_starts_for_an_address_that_is_not_the_tv(self):
        stream = self.make_stream()
        s = self.session(self.fake(), stream=stream)
        s.receiver = cast.Receiver("Fake TV", "8.8.8.8", port=1)
        with self.assertRaises(engine.CastError):
            s.start(timeout=5)
        self.assertEqual((stream.began, stream.closed), ([], 1))

    def test_an_encoder_that_dies_ends_the_cast_as_failed_and_says_why(self):
        fake, s = self.streamed()
        s.start(timeout=10)
        self.stream.error = "Error while encoding: Conversion failed!"
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertTrue(s.failed)
        self.assertIn("The conversion stopped (Error while encoding: Conversion failed!)", s.notice)
        self.assertIn("The movie stopped at about 01:4", s.notice)
        self.assertIn("STOP", fake.kinds(RECV_NS), "the TV leaves the Cast screen")
        self.assertIn("END: The conversion stopped", self.log(s))

    def test_an_encoder_error_with_control_characters_is_printed_safely(self):
        fake, s = self.streamed()
        s.start(timeout=10)
        self.stream.error = "bad\x1b]0;pwned\x07 thing"
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertNotIn("\x1b", s.notice)

    def test_an_encoder_that_dies_while_starting_is_a_failed_start(self):
        fake, s = self.streamed(fetch=False)                          # the TV never plays: the start is still pending
        self.stream.error = "ffmpeg died"
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertIn("The conversion stopped (ffmpeg died)", str(cm.exception))
        self.assertEqual(self.stream.closed, 1)

    def test_a_healthy_encoder_leaves_the_cast_alone(self):
        fake, s = self.streamed()
        s.start(timeout=10)
        time.sleep(0.8)
        self.assertTrue(s.state().alive)
        self.assertFalse(s.failed)

    def test_stop_closes_the_stream_once_and_after_the_server(self):
        fake, s = self.streamed()
        s.start(timeout=10)
        stop_server = s.server.stop
        s.server.stop = lambda: (self.stream.events.append("server stopped"), stop_server())
        s.stop()
        s.stop()
        self.assertEqual(self.stream.closed, 1)
        self.assertEqual(self.stream.events, ["server stopped", "stream closed"])
        self.assertIn("ENCODER stopped at 20:34, 8.4x", self.log(s))

    def test_stop_before_start_still_closes_the_stream(self):
        s = self.session(self.fake(), stream=self.make_stream())
        s.stop()
        s.stop()
        self.assertEqual(s.stream.closed, 1)

    def test_a_file_cast_has_no_encoder_to_report(self):
        s = self.session(self.fake())
        self.assertIsNone(s.encoder_status())
        self.assertIsNone(s.stream)

    def test_encoder_status_is_the_streams(self):
        stream = self.make_stream()
        s = self.session(self.fake(), stream=stream)
        self.assertEqual(s.encoder_status(), stream.status())

    def test_the_buffer_figure_is_what_the_tv_has_fetched(self):
        fake, s = self.streamed(start=100.0)
        s.start(timeout=10)
        self.wait(lambda: s.server.fetched == {16, 17}, what="the server to book the TV's two segments")
        sample = s.link_sample()
        self.assertGreaterEqual(sample.bytes_out, 2000 + len(self.stream.timeline.playlist()))
        self.assertIsNone(sample.offset)
        self.assertTrue(0 < sample.ahead <= 8.0, sample.ahead)       # the segments end at 108 s, the TV plays from ~100
        self.assertAlmostEqual(sample.ahead, 108.0 - s.state().position, delta=0.5)


class Controls(Base):
    def test_position_is_interpolated_between_status_reports(self):
        fake = self.fake()
        s = self.session(fake, cls=type("SlowPoll", (QuickCast,), {"POLL_EVERY": 60.0}))
        s.start(timeout=10)
        a = s.state().position
        time.sleep(1.0)
        b = s.state().position
        self.assertAlmostEqual(b - a, 1.0, delta=0.3)                 # no status arrived, yet the clock moves
        self.assertAlmostEqual(b, fake.position(), delta=0.5)

    def test_pause_play_and_seek(self):
        fake, s = self.started()
        s.toggle_pause()
        self.assertTrue(s.state().paused, "shown at once, before the TV confirms")
        self.wait(lambda: fake.session["state"] == "PAUSED", what="the TV to pause")
        self.wait(lambda: abs(s.state().position - fake.position()) < 0.05, what="the TV's paused position")
        a = s.state().position
        time.sleep(0.6)
        self.assertAlmostEqual(s.state().position, a, delta=0.05)     # frozen while paused
        s.seek_to(300)
        self.wait(lambda: abs(fake.position() - 300) < 0.5, what="the TV to seek")
        self.assertEqual(fake.session["state"], "PAUSED", "a seek keeps the play/pause state")
        self.assertAlmostEqual(s.state().position, 300, delta=0.5)
        self.assertNotIn("resumeState", [m for ns, t, m in fake.seen if t == "SEEK"][0])
        s.toggle_pause()
        self.wait(lambda: fake.session["state"] == "PLAYING", what="the TV to play")
        s.seek_by(-30)
        self.wait(lambda: abs(fake.position() - 270) < 1.5, what="the relative seek")
        s.seek_to(10_000)                                              # clamped to just before the end
        self.wait(lambda: fake.position() >= 590, what="the clamped seek")
        self.assertLessEqual(fake.position(), 596)
        s.seek_to(-50)
        self.wait(lambda: fake.position() < 2, what="the seek to the start")
        self.assertTrue(any(r.startswith(f"bytes={int(len(self.media.read_bytes()) * 300 / 600)}")
                            for r, *_ in fake.fetches), "a seek makes the TV fetch from the new offset")

    def test_stop_closes_the_tv_player_and_the_server(self):
        fake, s = self.started()
        port = s.server.port
        s.stop()
        self.assertIn("STOP", fake.kinds(MEDIA_NS))
        self.assertIn("STOP", fake.kinds(RECV_NS))
        self.assertIsNone(fake.app, "the TV left the Cast screen")
        self.assertFalse(s.state().alive)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        s.stop()
        s.toggle_pause()                                               # controls after stop are ignored, not errors
        s.seek_by(10)

    def test_counters_for_the_player_panel(self):
        fake, s = self.started()
        # the TV's second download may still be writing: let the server book both before reading its counters twice
        self.wait(lambda: len(s.server.recent) == 2, what="the server to book both downloads")
        sample = s.link_sample()
        self.assertGreater(sample.bytes_out, 0)
        self.assertIsNone(sample.retx)
        self.assertIsNone(sample.rtt_ms)
        self.assertEqual(sample.offset, s.server.reach - 40)            # media bytes past the MP4 header
        tv = s.tv_status()
        self.assertEqual((tv.state, tv.duration), ("PLAYING", 600.0))

    def test_the_log_tells_the_story_without_heartbeats(self):
        fake, s = self.started()
        time.sleep(1.2)                                                 # a few heartbeats go by
        s.stop()
        text = self.log(s)
        for needle in ("TX receiver-0 receiver {\"type\":\"LAUNCH\"", "\"type\":\"LOAD\"", "STATE BUFFERING -> PLAYING",
                       "STARTED", "HTTP 127.0.0.1 GET movie Range=bytes=0-65535", "STOPPED"):
            self.assertIn(needle, text)
        self.assertNotIn("PING", text)
        self.assertNotIn("PONG", text)
        self.assertIn("PING", fake.kinds(), "heartbeats were sent, just not logged")


class Recovery(Base):
    def test_a_dropped_connection_is_reattached_without_reloading(self):
        fake, s = self.started()
        fake.drop()
        self.wait(lambda: fake.kinds(RECV_NS).count("GET_STATUS") >= 2, what="a reconnect")
        self.wait(lambda: s.notice == "TV reconnected", what="the reconnected notice")
        time.sleep(0.5)
        self.assertEqual(len(fake.loads), 1, "playback must not be touched")
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 1)
        self.assertTrue(s.state().alive)
        self.assertIn("RE-ATTACHED", self.log(s))
        s.toggle_pause()                                                # and the controls still work
        self.wait(lambda: fake.session["state"] == "PAUSED", what="pause after the reconnect")

    def test_a_silent_tv_counts_as_a_dropped_connection(self):
        fake, s = self.started()
        fake.mute = True
        self.wait(lambda: "nothing from the TV" in self.log(s), what="the dead-link timeout")
        fake.mute = False
        self.wait(lambda: s.notice == "TV reconnected", what="the reconnect")
        self.assertEqual(len(fake.loads), 1)

    def test_a_tv_error_reloads_at_the_last_position_not_the_start(self):
        fake, s = self.started(start=100.0)
        fake.wait_for(lambda: fake.position() > 101)
        s.seek_to(250)
        self.wait(lambda: abs(fake.position() - 250) < 1, what="the seek")
        time.sleep(0.5)
        fake.fail()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        again = fake.loads[1]["currentTime"]
        self.assertGreater(again, 249)                                   # where it was, not 0, not the 100 s start
        self.assertLess(again, 253)
        self.wait(lambda: s.notice.startswith("Reloaded after a TV error at 04:1"), what="the notice")
        self.assertTrue(s.state().alive)
        self.assertGreater(s.state().position, 249)

    def test_reloads_are_limited_to_three_in_ten_minutes(self):
        now = {"t": 0.0}
        fake = self.fake()
        s = self.session(fake, clock=lambda: now["t"])
        s.start(timeout=10)
        for n in range(2, 5):                                            # three errors, three reloads
            self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
            fake.fail()
            self.wait(lambda: len(fake.loads) == n, what=f"reload {n - 1}")
        now["t"] = 599.0                                                 # still inside the 10 minutes
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        fake.fail()
        self.wait(lambda: not s.state().alive, what="giving up")
        self.assertEqual(len(fake.loads), 4)
        self.assertIn("gave up after 3 reloads", s.notice)
        self.assertIn("cast again and start there", s.notice)

    def test_the_reload_budget_refills_after_ten_minutes(self):
        now = {"t": 0.0}
        fake = self.fake()
        s = self.session(fake, clock=lambda: now["t"])
        s.start(timeout=10)
        for n in range(2, 5):
            self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
            fake.fail()
            self.wait(lambda: len(fake.loads) == n, what=f"reload {n - 1}")
        now["t"] = 601.0
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        fake.fail()
        self.wait(lambda: len(fake.loads) == 5, what="a fourth reload, ten minutes later")
        self.assertTrue(s.state().alive)

    def test_a_forgotten_media_session_is_reloaded(self):
        fake, s = self.started(start=200.0)
        fake.forget()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        self.assertGreaterEqual(fake.loads[1]["currentTime"], 200.0)

    def test_a_player_gone_while_disconnected_is_relaunched_at_the_position(self):
        fake, s = self.started(start=200.0)
        time.sleep(0.4)
        with fake.lock:
            fake.drop()
            fake.app, fake.session = None, None                         # the TV's player died with the link
        self.wait(lambda: len(fake.loads) == 2, what="a relaunch and reload")
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 2)
        self.assertGreaterEqual(fake.loads[1]["currentTime"], 200.0)
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing again")
        self.assertTrue(s.state().alive)

    def test_another_sender_taking_over_ends_the_session_without_a_fight(self):
        fake, s = self.started()
        fake.interrupt()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device", s.notice)
        s.stop()
        self.assertEqual(len(fake.loads), 1)
        self.assertNotIn("STOP", fake.kinds(), "never stop someone else's cast")

    def test_closing_the_player_on_the_tv_is_respected(self):
        fake, s = self.started(start=100.0)
        fake.close_app()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("The TV closed the player at 01:4", s.notice)
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 1)

    def test_the_end_of_the_movie_ends_the_session(self):
        fake, s = self.started(start=580.0)
        fake.finish()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertEqual(s.notice, "The movie finished.")
        self.assertEqual(len(fake.loads), 1)

    def test_finished_long_before_the_end_is_a_broken_download(self):
        fake, s = self.started(start=100.0)
        fake.finish()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        self.assertGreaterEqual(fake.loads[1]["currentTime"], 100.0)
        self.assertTrue(s.state().alive)

    def test_a_tv_gone_for_good_is_given_up_on(self):
        fake, s = self.started()
        fake.close()                                                    # the TV is switched off
        self.wait(lambda: not s.state().alive, timeout=10, what="giving up")
        self.assertIn("Lost the TV", s.notice)
        self.assertIn("RECONNECT", self.log(s))

    def test_the_tv_buffering_is_shown_as_buffering(self):
        fake, s = self.started()
        fake.buffering()
        self.wait(lambda: s.state().buffering, what="buffering")
        self.assertEqual(len(fake.loads), 1, "buffering is the TV's job: no reload")


class Interface(unittest.TestCase):
    def test_the_cast_offers_the_session_interface(self):
        for cls in (nativecast.NativeCast,):
            for name in ("start", "stop", "state", "toggle_pause", "seek_to", "seek_by", "__enter__", "__exit__"):
                self.assertTrue(callable(getattr(cls, name, None)), f"{cls.__name__}.{name}")
        for name in ("tv_status", "link_sample", "encoder_status"):
            self.assertTrue(callable(getattr(nativecast.NativeCast, name)))
        self.assertIsInstance(nativecast.NativeCast.notice, property)


def fetch(url):
    import http.client
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    c = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        c.request("GET", parts.path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


if __name__ == "__main__":
    unittest.main()
