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


# ---------------------------------------------------------------- the restart policy, with a fake encoder

FAKE = r"""
import sys, time
from pathlib import Path
work, start, count, log = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
with log.open("a") as f:
    f.write(f"{start}\n")
for i in range(start, count):
    time.sleep(0.04)
    (work / f"seg{i:05d}.ts.tmp").write_bytes(b"x" * 100)
    (work / f"seg{i:05d}.ts.tmp").rename(work / f"seg{i:05d}.ts")
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

    def make(self, script=FAKE, count=100):
        s = OnTheFly(self.timeline, lambda i: [sys.executable, "-c", script, str(self.work), str(i), str(count), str(self.log)],
                     self.work)
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
