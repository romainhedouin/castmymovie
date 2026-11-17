"""Cast on the fly: the segment timeline, the encoder commands, and the encoder that restarts on a seek."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import HAVE_FFMPEG, ff  # noqa: E402

from castmymovie import engine, onthefly  # noqa: E402
from castmymovie.onthefly import OnTheFly, Timeline  # noqa: E402

NTSC = Fraction(24000, 1001)


class TimelineMath(unittest.TestCase):
    def test_segments_are_whole_frames(self):
        t = Timeline(45, NTSC)
        self.assertEqual((t.seg_frames, t.count), (144, 8))
        self.assertAlmostEqual(t.seg_len, 6.006)
        self.assertAlmostEqual(t.start(3), 18.018)
        self.assertAlmostEqual(t.end(7), 45)  # the last one is cut short by the end of the movie

    def test_index_at_survives_float_noise_at_a_boundary(self):
        t = Timeline(600, NTSC)
        self.assertEqual([t.index_at(x) for x in (0, 6.0, 6.006, 18.018, 18.01, 1e6, -3)], [0, 0, 1, 3, 2, t.count - 1, 0])

    def test_float_frame_rates_are_accepted(self):
        self.assertEqual(Timeline(60, 25.0).seg_frames, 150)
        self.assertEqual(Timeline(60, 23.976).fps, NTSC)

    def test_a_movie_of_exact_segments_has_no_empty_last_one(self):
        self.assertEqual(Timeline(60, 25).count, 10)
        self.assertEqual(Timeline(0.5, 25).count, 1)

    def test_playlist_is_a_complete_vod_with_exact_lengths(self):
        text = Timeline(45, NTSC).playlist().decode()
        lines = text.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", lines)
        self.assertIn("#EXT-X-TARGETDURATION:6", lines)
        self.assertEqual(lines[-1], "#EXT-X-ENDLIST")
        extinf = [float(x[8:-1]) for x in lines if x.startswith("#EXTINF")]
        self.assertEqual(len(extinf), 8)
        self.assertEqual(extinf[:7], [6.006] * 7)
        self.assertAlmostEqual(sum(extinf), 45, places=3)
        self.assertEqual([x for x in lines if x.endswith(".ts")][:2], ["seg00000.ts", "seg00001.ts"])


def plan(**kw):
    return engine.Plan(**{"video": "encode", "audio": "copy", "video_idx": 0, "audio_idx": 1, **kw})


def info(audio="aac", w=1280, h=720):
    return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": w, "height": h},
                        {"index": 1, "codec_type": "audio", "codec_name": audio, "channels": 2}]}


class Commands(unittest.TestCase):
    def test_the_mp4_command_is_what_it_was(self):
        cmd = engine.build_cmd(Path("/m/a.mkv"), Path("/m/a.part"), plan(audio="encode", audio_ch=2, audio_out_kbps=192),
                               info("dts"), 6.0)
        self.assertEqual(cmd[9:], [
            "-i", "/m/a.mkv", "-map", "0:0", "-map", "0:1", "-sn", "-dn",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", "5208k", "-bufsize", "5208k",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", "-f", "mp4", "/m/a.part"])

    def test_the_first_run_starts_at_the_beginning_of_everything(self):
        cmd = engine.build_hls_cmd(Path("/m/a.mkv"), None, Path("/t"), plan(), info(), 6.0, NTSC, 144)
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-output_ts_offset", cmd)
        self.assertEqual(cmd[cmd.index("-start_number") + 1], "0")
        self.assertEqual(cmd[cmd.index("-g") + 1], "144")
        self.assertEqual(cmd[cmd.index("-r") + 1], "24000/1001")
        self.assertEqual(cmd[cmd.index("-map", cmd.index("-map") + 1) + 1], "0:1")  # no sidecar: the audio is the source's

    def test_a_restart_seeks_both_inputs_and_shifts_its_timestamps(self):
        cmd = engine.build_hls_cmd(Path("/m/a.mkv"), Path("/t/audio.mka"), Path("/t"), plan(), info(), 6.0, NTSC, 144, 3)
        at = f"{3 * 144 * 1001 / 24000:.6f}"
        self.assertEqual([i for i, x in enumerate(cmd) if x == "-ss"], [cmd.index("-i") - 2, cmd.index("-i", cmd.index("-i") + 1) - 2])
        self.assertEqual(cmd.count(at), 2)
        self.assertAlmostEqual(float(cmd[cmd.index("-output_ts_offset") + 1]), 18.018 + 2 / float(NTSC), places=5)
        self.assertEqual(cmd[cmd.index("-start_number") + 1], "3")
        self.assertIn("-copypriorss", cmd)
        self.assertEqual(cmd[cmd.index("-map", cmd.index("-map") + 1) + 1], "1:0")  # the sidecar's only track

    def test_a_sidecar_that_starts_late_is_seeked_by_its_delay_less(self):
        def seeks(start_index, offset):
            cmd = engine.build_hls_cmd(Path("/m/a.mkv"), Path("/t/audio.mka"), Path("/t"), plan(), info(), 6.0, NTSC, 144,
                                       start_index, offset)
            first, second = cmd.index("-i"), cmd.index("-i", cmd.index("-i") + 1)
            return [cmd[i - 1] if cmd[i - 2] == "-ss" else None for i in (first, second)]
        self.assertEqual(seeks(0, 1.0), [None, "-1.000000"])              # the first run: before the sidecar's start
        self.assertEqual(seeks(3, 1.0), ["18.018000", "17.018000"])       # a restart: the same moment of the movie
        self.assertEqual(seeks(3, 20.0), ["18.018000", "-1.982000"])      # a restart inside the delay
        self.assertEqual(seeks(3, 18.018), ["18.018000", None])           # exactly at the sidecar's start
        self.assertEqual(seeks(0, 0.0), [None, None])                     # no delay: as before

    def test_without_a_sidecar_the_offset_changes_nothing(self):
        args = (Path("/m/a.mkv"), None, Path("/t"), plan(), info(), 6.0, NTSC, 144)
        for i in (0, 3):
            self.assertEqual(engine.build_hls_cmd(*args, i, 1.0), engine.build_hls_cmd(*args, i))

    def test_the_offset_is_where_the_sidecar_starts_on_the_sources_clock(self):
        for sidecar, source, want in (("1.000000", "0.000000", 1.0), ("1.894000", "1.400000", 0.494),
                                      ("0.000000", "0.000000", 0.0), (None, None, 0.0), ("1.4003", "1.400000", 0.0)):
            with mock.patch.object(engine, "probe", return_value={"format": {"start_time": sidecar} if sidecar else {}}):
                got = engine.audio_start_offset(Path("/t/audio.mka"), {"format": {"start_time": source} if source else {}})
            self.assertEqual(got, want, (sidecar, source))

    def test_frame_rates_snap_to_the_standard_ones(self):
        for avg, want in (("24000/1001", NTSC), ("23976/1000", NTSC), ("25/1", Fraction(25)), ("2997/100", Fraction(30000, 1001)),
                          ("0/0", Fraction(24))):
            self.assertEqual(engine.cfr_rate({"avg_frame_rate": avg}), want, avg)

    def test_audio_is_converted_first_unless_a_segment_carries_it(self):
        self.assertFalse(engine.needs_audio_pass(plan(), info("aac")))
        self.assertFalse(engine.needs_audio_pass(plan(), info("mp3")))
        self.assertTrue(engine.needs_audio_pass(plan(), info("opus")))
        self.assertTrue(engine.needs_audio_pass(plan(), info("flac")))
        self.assertTrue(engine.needs_audio_pass(plan(audio="encode"), info("dts")))
        self.assertFalse(engine.needs_audio_pass(engine.Plan(video="encode", audio="none"), info()))


AWKWARD = (Fraction(239, 10), Fraction(249, 10), Fraction(2987, 100), Fraction(8641, 420))  # 6 x fps rounds down


def hls_cuts(seg_frames: int, fps: Fraction, hls_time: Fraction, count: int) -> list:
    """The keyframes ffmpeg's HLS muxer starts the segments at: segment n at the first one n x hls_time or more past
    the start. The keyframes are seg_frames apart (the GOP)."""
    cuts, k = [0], 0
    while len(cuts) < count:
        k += 1
        if k * seg_frames / fps >= len(cuts) * hls_time:
            cuts.append(k)
    return cuts


class SegmentCuts(unittest.TestCase):
    def test_a_rate_where_six_seconds_rounds_down_has_segments_under_six_seconds(self):
        t = Timeline(60, Fraction(239, 10))
        self.assertEqual((t.seg_frames, t.count), (143, 11))
        self.assertAlmostEqual(t.seg_len, 5.98326, places=5)
        self.assertAlmostEqual(t.start(10), 10 * 143 / 23.9)
        self.assertIn("#EXT-X-TARGETDURATION:6", t.playlist().decode())

    def test_ffmpeg_cuts_at_every_keyframe_so_segment_i_starts_where_the_playlist_says(self):
        for fps in engine.STANDARD_RATES + list(AWKWARD):
            t = Timeline(3 * 3600, fps)
            cmd = engine.build_hls_cmd(Path("/m/a.mkv"), None, Path("/t"), plan(), info(), 6.0, t.fps, t.seg_frames)
            hls_time = Fraction(cmd[cmd.index("-hls_time") + 1])
            self.assertEqual(hls_cuts(t.seg_frames, t.fps, hls_time, t.count), list(range(t.count)), fps)


# ---------------------------------------------------------------- the restart policy, with a fake encoder

FAKE = r"""
import sys, time
from pathlib import Path
work, start, count, log = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
pause = float(sys.argv[5]) if len(sys.argv) > 5 else 0.04
with log.open("a") as f:
    f.write(f"{start}\n")
for i in range(start, count):
    time.sleep(pause)
    (work / f"seg{i:05d}.ts.tmp").write_bytes(b"x" * 100)
    (work / f"seg{i:05d}.ts.tmp").rename(work / f"seg{i:05d}.ts")
    print("speed=9.0x", flush=True)
time.sleep(30)
"""

# like ffmpeg on SIGTERM: the segment it is writing is renamed in, cut short, and it exits with 255
FFMPEG_LIKE = r"""
import signal, sys, time
from pathlib import Path
work, start, count, log = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
pause = float(sys.argv[5])
with log.open("a") as f:
    f.write(f"{start}\n")
current = None
def finish_early(*_):
    try:
        current.rename(current.with_suffix(""))
    except (AttributeError, OSError):
        pass
    sys.exit(255)
signal.signal(signal.SIGTERM, finish_early)
for i in range(start, count):
    current = work / f"seg{i:05d}.ts.tmp"
    current.write_bytes(b"cut")
    time.sleep(pause)
    current.write_bytes(b"whole")
    current.rename(work / f"seg{i:05d}.ts")
    current = None
    print("speed=9.0x", flush=True)
time.sleep(30)
"""


class FakeEncoder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-otf-"))
        self.work, self.log = self.dir / "seg", self.dir / "launches"
        self.timeline = Timeline(600, 25)  # 100 segments
        self.streams = []

    def tearDown(self):
        for s in self.streams:
            s.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, script=FAKE, count=100, *extra):
        s = OnTheFly(self.timeline, lambda i: [sys.executable, "-c", script, str(self.work), str(i), str(count), str(self.log),
                                               *map(str, extra)], self.work)
        self.streams.append(s)
        return s

    def launches(self):
        return [int(x) for x in self.log.read_text().split()] if self.log.exists() else []

    def test_the_first_segments_come_and_the_status_follows(self):
        s = self.make()
        s.begin(0)
        s.wait_ready(2, 10)
        self.assertTrue((self.work / "seg00001.ts").exists())
        time.sleep(0.5)
        st = s.status()
        self.assertGreaterEqual(st.frontier_s, 12.0)
        self.assertEqual((st.speed, st.slow), (9.0, False))

    def test_start_in_the_middle_begins_at_that_segment(self):
        s = self.make()
        s.begin(300)
        s.wait_ready(1, 10)
        self.assertEqual(self.launches(), [50])
        self.assertFalse((self.work / "seg00000.ts").exists())

    def test_a_request_just_ahead_of_the_encoder_waits_instead_of_restarting(self):
        s = self.make()
        s.begin(0)
        self.assertIsNotNone(s.segment(6, 10))
        self.assertEqual(self.launches(), [0])

    def test_a_request_far_ahead_restarts_the_encoder_there_once(self):
        s = self.make()
        s.begin(0)
        s.wait_ready(1, 10)
        self.assertIsNotNone(s.segment(60, 10))
        self.assertEqual(self.launches(), [0, 60])

    def test_the_next_segment_asked_first_does_not_make_the_encoder_flap(self):
        s = self.make()
        s.begin(0)
        s.wait_ready(1, 10)
        got = {}
        threads = [threading.Thread(target=lambda n=n, d=d: (time.sleep(d), got.__setitem__(n, s.segment(n, 10))))
                   for n, d in ((71, 0.0), (70, 0.1))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertTrue(all(got.get(n) for n in (70, 71)), got)
        self.assertEqual(self.launches(), [0, 70])

    def test_a_segment_already_written_is_handed_out_without_touching_the_encoder(self):
        s = self.make()
        s.begin(0)
        s.wait_ready(3, 10)
        self.assertIsNotNone(s.segment(1, 1))
        self.assertEqual(self.launches(), [0])

    def test_the_encoder_stops_where_an_earlier_run_already_got_to(self):
        s = self.make()
        self.work.mkdir(parents=True)
        (self.work / "seg00005.ts").write_bytes(b"done")
        s.begin(2)
        deadline = time.monotonic() + 10
        while s._proc.poll() is None and time.monotonic() < deadline:
            s.status()
            time.sleep(0.05)
        self.assertIsNotNone(s._proc.poll())
        self.assertIsNone(s.error)
        self.assertTrue(all((self.work / f"seg{i:05d}.ts").exists() for i in (2, 3, 4, 5)))

    def wait_for_exit(self, s):
        deadline = time.monotonic() + 10
        while s._proc.poll() is None and time.monotonic() < deadline:
            s.status()
            time.sleep(0.05)
        self.assertIsNotNone(s._proc.poll())
        time.sleep(0.2)  # the reader thread books the exit

    def test_a_run_stopped_at_a_finished_segment_is_no_error_and_leaves_it_whole(self):
        s = self.make(FFMPEG_LIKE, 100, 0.1)
        self.work.mkdir(parents=True)
        (self.work / "seg00005.ts").write_bytes(b"done")
        s.begin(12)
        self.wait_for_exit(s)
        self.assertIsNone(s.error)
        self.assertEqual((self.work / "seg00005.ts").read_bytes(), b"done")
        self.assertEqual([(self.work / f"seg{i:05d}.ts").read_bytes() for i in (2, 3, 4)], [b"whole"] * 3)

    def test_a_restart_leaves_no_segment_cut_short(self):
        s = self.make(FFMPEG_LIKE, 100, 0.3)
        s.begin(0)
        s.wait_ready(1, 10)
        self.assertIsNotNone(s.segment(60, 10))  # the first run is half way through a segment when it is stopped
        self.assertEqual(self.launches(), [0, 60])
        self.assertEqual({p.read_bytes() for p in self.work.glob("seg*.ts")}, {b"whole"})
        self.assertTrue(all(int(p.name[3:8]) > 60 for p in self.work.glob("*.tmp")))  # only the new run's
        self.assertIsNone(s.error)

    def test_a_request_that_is_abandoned_returns_at_once_and_restarts_nothing(self):
        s = self.make(FAKE, 100, 0.3)
        s.begin(0)
        s.wait_ready(1, 10)
        t0, calls = time.monotonic(), []
        self.assertIsNone(s.segment(60, 30, lambda: calls.append(1) or len(calls) > 2))
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(self.launches(), [0])
        self.assertEqual(s._pending, set())

    def test_an_encoder_that_dies_is_reported_and_requests_fail_fast(self):
        s = self.make("import sys; sys.stderr.write('boom: no such codec'); sys.exit(3)")
        s.begin(0)
        with self.assertRaises(engine.CastError) as ctx:
            s.wait_ready(1, 10)
        self.assertIn("boom: no such codec", str(ctx.exception))
        self.assertIn("exit 3", s.error)
        t0 = time.monotonic()
        self.assertIsNone(s.segment(4, 30))
        self.assertLess(time.monotonic() - t0, 2)

    def test_a_missing_encoder_binary_is_an_error_not_a_crash(self):
        s = OnTheFly(self.timeline, lambda i: ["/nonexistent/ffmpeg"], self.work)
        self.streams.append(s)
        s.begin(0)
        self.assertIn("could not start ffmpeg", s.error)
        self.assertIsNone(s.segment(0, 1))

    def test_a_request_gives_up_after_its_timeout(self):
        s = self.make("import time; time.sleep(30)")
        s.begin(0)
        t0 = time.monotonic()
        self.assertIsNone(s.segment(1, 0.5))
        self.assertLess(time.monotonic() - t0, 3)

    def test_close_kills_the_encoder_and_removes_the_folder(self):
        s = self.make()
        s.begin(0)
        s.wait_ready(1, 10)
        proc = s._proc
        s.close()
        s.close()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(self.work.exists())
        self.assertIsNone(s.segment(50, 5))

    def test_a_close_wakes_a_request_that_is_waiting(self):
        s = self.make("import time; time.sleep(30)")
        s.begin(0)
        out = []
        t = threading.Thread(target=lambda: out.append(s.segment(1, 30)))
        t.start()
        time.sleep(0.3)
        s.close()
        t.join(5)
        self.assertEqual(out, [None])


class ThroughTheServer(unittest.TestCase):
    """A slow encoder behind the real media server: a request is cut into nothing, so a seek restarts the encoder."""

    def setUp(self):
        from castmymovie.mediaserver import MediaServer
        self.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-otf-"))
        self.work, self.log = self.dir / "seg", self.dir / "launches"
        self.stream = OnTheFly(Timeline(600, 25), lambda i: [sys.executable, "-c", FAKE, str(self.work), str(i), "100",
                                                             str(self.log), "0.3"], self.work)
        self.srv = MediaServer(self.dir / "movie.mkv", allow={"127.0.0.1"}, stream=self.stream)
        self.srv.SEGMENT_WAIT = 4.0
        self.srv.start(host="127.0.0.1")

    def tearDown(self):
        import shutil
        self.srv.stop()
        self.stream.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def get(self, n):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            c.request("GET", f"/{self.srv.token}/seg{n:05d}.ts")
            return c.getresponse().status
        finally:
            c.close()

    def launches(self):
        return [int(x) for x in self.log.read_text().split()]

    def test_a_seek_far_ahead_restarts_the_encoder(self):
        self.stream.begin(0)
        self.stream.wait_ready(1, 10)
        t0 = time.monotonic()
        self.assertEqual(self.get(60), 200)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(self.launches(), [0, 60])

    def test_a_seek_back_restarts_the_encoder_which_stops_where_the_first_run_began(self):
        self.stream.begin(300)
        self.stream.wait_ready(1, 10)
        self.assertEqual(self.get(49), 200)
        self.assertEqual(self.launches(), [50, 49])
        self.assertEqual(self.get(50), 200)
        deadline = time.monotonic() + 5
        while self.stream._proc.poll() is None and time.monotonic() < deadline:
            self.stream.status()
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertIsNotNone(self.stream._proc.poll())
        self.assertIsNone(self.stream.error)

    def test_the_next_segment_asked_first_does_not_make_the_encoder_flap(self):
        self.stream.begin(0)
        self.stream.wait_ready(1, 10)
        got = {}
        threads = [threading.Thread(target=lambda n=n, d=d: (time.sleep(d), got.__setitem__(n, self.get(n))))
                   for n, d in ((71, 0.0), (70, 0.1))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(got, {70: 200, 71: 200})
        self.assertEqual(self.launches(), [0, 70])


class Sweeping(unittest.TestCase):
    def test_only_folders_of_dead_processes_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead, mine, other = root / "castmymovie-otf-4000000-abc", root / f"castmymovie-otf-{os.getpid()}-abc", root / "keep-me"
            for d in (dead, mine, other):
                (d / "seg-x").mkdir(parents=True)
            with mock.patch.object(onthefly.tempfile, "gettempdir", return_value=tmp):
                onthefly.sweep_stale()
            self.assertEqual([dead.exists(), mine.exists(), other.exists()], [False, True, True])


# ---------------------------------------------------------------- the real thing

def probe_first_pts(path: Path, kind: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.split()
    return float(out[0].strip(","))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RealEncoder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-otf-"))
        cls.src = cls.dir / "movie.mkv"
        # 80 s of 24 fps video with Opus sound: needs an audio pass, and is long enough for a seek past the look-ahead
        ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=80", "-f", "lavfi", "-i", "sine=frequency=440:duration=80",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "800k", "-ac", "2", "-c:a", "libopus", "-b:a", "64k", str(cls.src))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        sc = engine.scan(self.src)
        self.an = engine.analyze(sc, 0.3)  # a cap far below the clip's peak: the video has to be re-encoded
        self.assertEqual(self.an.plan.video, "encode")
        self.out = onthefly.prepare(sc, self.an)
        self.stream = None

    def tearDown(self):
        if self.stream:
            self.stream.close()
        self.out.cleanup()

    def test_the_audio_is_converted_first_and_the_cleanup_removes_everything(self):
        self.assertTrue(self.out.on_the_fly)
        self.assertTrue(self.out.audio_path.exists())
        a = engine.probe(self.out.audio_path)["streams"][0]
        self.assertEqual(a["codec_name"], "aac")
        self.assertEqual((self.out.plan.audio, self.out.plan.audio_out_kbps), ("copy", 192))
        self.out.cleanup()
        self.out.cleanup()
        self.assertFalse(self.out.folder.exists())

    def test_segments_are_a_whole_number_of_frames_with_video_and_audio(self):
        self.stream = s = self.out.make_stream()
        s.begin(0)
        s.wait_ready(2, 60)
        seg = s.segment(1, 5)
        streams = engine.probe(seg)["streams"]
        self.assertEqual(sorted(x["codec_type"] for x in streams), ["audio", "video"])
        self.assertEqual({x["codec_name"] for x in streams}, {"h264", "aac"})
        length = float(engine.probe(seg)["format"]["duration"])
        self.assertAlmostEqual(length, s.timeline.seg_len, delta=0.15)
        self.assertEqual(s.timeline.count, 14)

    def test_a_seek_far_ahead_restarts_and_the_timelines_join(self):
        self.stream = s = self.out.make_stream()
        fast = s.make_cmd
        s.make_cmd = lambda i: (lambda c: c[:c.index("-i")] + ["-readrate", "10"] + c[c.index("-i"):])(fast(i))  # not all at once
        far = 12
        with mock.patch.object(onthefly, "LOOKAHEAD", 4):
            s.begin(0)
            s.wait_ready(3, 60)
            seg = s.segment(far, 60)
        self.assertIsNotNone(seg)
        self.assertEqual(s._run_start, far)
        t, reorder = s.timeline, 2 / float(s.timeline.fps)
        for i, kind in ((2, "v"), (far, "v"), (far, "a")):
            pts = probe_first_pts(s._path(i), kind)
            self.assertAlmostEqual(pts, t.start(i) + reorder, delta=0.12, msg=f"segment {i} {kind}")
        self.assertIsNone(s.error)

    def test_a_seek_back_that_catches_up_with_an_earlier_run_stops_quietly_and_cuts_nothing_short(self):
        self.stream = s = self.out.make_stream()
        fast = s.make_cmd
        s.make_cmd = lambda i: (lambda c: c[:c.index("-r")] + ["-vf", "realtime=speed=8"] + c[c.index("-r"):])(fast(i))
        s.begin(s.timeline.start(4))
        s.wait_ready(1, 60)
        time.sleep(0.3)                                    # the first run is part way into segment 5
        self.assertIsNotNone(s.segment(2, 60))
        self.assertEqual(s._run_start, 2)
        deadline = time.monotonic() + 60
        while s._proc.poll() is None and time.monotonic() < deadline:
            s.status()
            time.sleep(0.05)
        time.sleep(0.3)
        self.assertIsNone(s.error)                         # stopped by us at segment 4: not a failure
        self.assertEqual(s._cursor, 4)
        self.assertEqual(list(s.workdir.glob("*.tmp")), [])
        for p in sorted(s.workdir.glob("seg*.ts")):        # every segment on disk is a whole one
            length = float(engine.probe(p)["format"]["duration"])
            self.assertAlmostEqual(length, s.timeline.seg_len, delta=0.15, msg=p.name)

    def test_a_movie_cast_from_the_middle_starts_there(self):
        self.stream = s = self.out.make_stream()
        s.begin(40)
        s.wait_ready(1, 60)
        self.assertEqual(s._run_start, s.timeline.index_at(40))
        self.assertAlmostEqual(probe_first_pts(s._path(s._run_start), "v"), s.timeline.start(s._run_start) + 2 / 24, delta=0.12)

    def test_the_encoder_runs_to_the_end_and_the_last_segment_is_short(self):
        self.stream = s = self.out.make_stream()
        s.begin(0)
        last = s.segment(s.timeline.count - 1, 90)
        self.assertIsNotNone(last)
        length = float(engine.probe(last)["format"]["duration"])
        self.assertAlmostEqual(length, s.timeline.duration - s.timeline.start(s.timeline.count - 1), delta=0.3)

    def test_stopping_leaves_nothing_in_the_temp_folder(self):
        s = self.out.make_stream()
        s.begin(0)
        s.wait_ready(1, 60)
        work = s.workdir
        s.close()
        self.assertFalse(work.exists())


def first_beep_after_silence(path: Path) -> float:
    """When the first beep that follows a silence starts, on the segment's own timestamps."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-copyts", "-i", str(path), "-map", "0:a",
                          "-af", "silencedetect=noise=-30dB:d=0.05", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(err.split("silence_end: ", 1)[1].split()[0])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class DelayedAudio(unittest.TestCase):
    """A rip whose AC3 sound starts 1 s after the picture: the audio pass keeps the delay, and so must the segments."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-otf-"))
        cls.src = cls.dir / "movie.mkv"
        # a 0.1 s beep every 2 s of the sound, which starts at 1 s: beeps at 1, 3, 5... s of the movie
        ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=20", "-itsoffset", "1",
           "-f", "lavfi", "-i", r"aevalsrc=if(lt(mod(t\,2)\,0.1)\,0.8*sin(2*PI*1000*t)\,0):s=48000:d=19",
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "800k", "-c:a", "ac3", str(cls.src))
        sc = engine.scan(cls.src)
        cls.out = onthefly.prepare(sc, engine.analyze(sc, 0.3))

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.out.cleanup()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.stream = self.out.make_stream()

    def tearDown(self):
        self.stream.close()

    def test_the_audio_pass_measures_the_delay(self):
        self.assertAlmostEqual(self.out.audio_offset, 1.0, delta=0.01)

    def test_the_first_segment_keeps_the_delay(self):
        s = self.stream
        s.begin(0)
        s.wait_ready(1, 60)
        seg = s._path(0)
        self.assertAlmostEqual(probe_first_pts(seg, "a") - probe_first_pts(seg, "v"), 1.0, delta=0.05)

    def test_a_run_from_the_middle_plays_the_sound_of_that_moment(self):
        s = self.stream
        s.begin(12)
        s.wait_ready(1, 60)
        i, reorder = s._run_start, 2 / float(s.timeline.fps)
        self.assertEqual(i, 2)
        self.assertAlmostEqual(first_beep_after_silence(s._path(i)), 13 + reorder, delta=0.05)  # not 14: a beep early


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AwkwardRate(unittest.TestCase):
    """23.9 fps: a segment is 143 frames, 5.983 s, a bit under 6 s."""

    def test_every_segment_is_written_and_starts_where_the_playlist_says(self):
        with tempfile.TemporaryDirectory(prefix="castmymovie-test-otf-") as tmp:
            d = Path(tmp)
            ff("-f", "lavfi", "-i", "testsrc2=size=64x36:rate=239/10:duration=20", "-c:v", "libx264", "-preset",
               "ultrafast", str(d / "movie.mkv"))
            sc = engine.scan(d / "movie.mkv")
            t = Timeline(sc.duration, engine.cfr_rate(sc.video))
            self.assertEqual((t.fps, t.seg_frames, t.count), (Fraction(239, 10), 143, 4))
            for start in (0, 2):  # the first run, and a restart after a seek
                out = d / f"from{start}"
                out.mkdir()
                subprocess.run(engine.build_hls_cmd(sc.path, None, out, plan(audio="none", audio_idx=None), sc.info, 6.0,
                                                    t.fps, t.seg_frames, start), stdout=subprocess.DEVNULL, check=True)
                self.assertEqual(sorted(p.name for p in out.glob("seg*.ts")),
                                 [t.segment_name(i) for i in range(start, t.count)])
                for i in range(start, t.count):
                    self.assertAlmostEqual(probe_first_pts(out / t.segment_name(i), "v"), t.start(i) + 2 / float(t.fps),
                                           delta=0.02, msg=f"run from {start}, segment {i}")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    """The real encoder, the real server and the real sender against the fake TV: what a cast on the fly does."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-otf-"))
        cls.src = cls.dir / "movie.mkv"
        ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=80", "-f", "lavfi", "-i", "sine=frequency=440:duration=80",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "800k", "-ac", "2", "-c:a", "libopus", "-b:a", "64k", str(cls.src))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_play_from_the_middle_then_seek_back_and_far_ahead(self):
        from castmymovie import cast
        from fakereceiver import FakeReceiver
        from test_nativecast import QuickCast
        sc = engine.scan(self.src)
        out = onthefly.prepare(sc, engine.analyze(sc, 0.3))
        stream = out.make_stream()
        fake = FakeReceiver()
        session = QuickCast(cast.Receiver("Fake TV", "127.0.0.1", port=fake.port), out.video, 20.0, tls=False,
                            duration=out.duration, stream=stream)
        try:
            session.start(timeout=30)
            names = [f[0] for f in fake.fetches]
            self.assertEqual(names[:2], ["playlist", "seg00003.ts"])      # 20 s is in the 4th segment
            self.assertTrue(all(f[-1] for f in fake.fetches), fake.fetches)
            self.assertFalse((stream.workdir / "seg00000.ts").exists())  # nothing before the start was encoded
            n = len(fake.fetches)
            session.seek_to(70.0)                                         # far past the encoder: it restarts there
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not any(f[0] == "seg00011.ts" for f in fake.fetches[n:]):
                time.sleep(0.1)
            self.assertTrue(any(f[0] == "seg00011.ts" and f[-1] for f in fake.fetches[n:]), fake.fetches)
            self.assertIsNone(stream.error)
            self.assertIsNotNone(session.encoder_status())
            self.assertTrue(session.state().alive)
        finally:
            session.stop()
            fake.close()
            out.cleanup()
        self.assertFalse(stream.workdir.exists())


if __name__ == "__main__":
    unittest.main()
