import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine  # noqa: E402
from common import HAVE_FFMPEG, fixtures  # noqa: E402


def pipeline(src, mbps=6.0, overwrite=False, **kw):
    sc = engine.scan(Path(src))
    an = engine.analyze(sc, mbps, **kw)
    return sc, an, engine.convert(sc, an, overwrite=overwrite)


def streams(path, kind):
    return [s for s in engine.probe(Path(path))["streams"] if s["codec_type"] == kind]


class PureLogic(unittest.TestCase):
    def test_peak_uses_worst_window(self):
        s = engine.StreamStats()
        for sec in range(30):
            s.add(sec, 1_000_000 if 10 <= sec < 20 else 100_000)
        self.assertAlmostEqual(s.peak_bps(10), 8_000_000)
        self.assertAlmostEqual(s.avg_bps(30), (10 * 1_000_000 + 20 * 100_000) * 8 / 30)

    def test_peak_short_clip_uses_whole_clip(self):
        s = engine.StreamStats()
        for sec in range(4):
            s.add(sec, 500_000)
        self.assertAlmostEqual(s.peak_bps(10), 4_000_000)

    def test_language_matching(self):
        audios = [{"index": 1, "tags": {"language": "eng"}}, {"index": 2, "tags": {"language": "fra"}}]
        self.assertEqual(engine.pick_audio(audios, "fr")["index"], 2)
        self.assertEqual(engine.pick_audio(audios, "fre")["index"], 2)
        self.assertEqual(engine.pick_audio(audios, "de")["index"], 1)
        self.assertIsNone(engine.pick_audio([], "en"))

    def test_formatting(self):
        self.assertEqual(engine.fmt_dur(9285), "2:34:45")
        self.assertEqual(engine.fmt_dur(65), "01:05")
        self.assertEqual(engine.fmt_size(2_110_000_000), "2.11 GB")

    def test_output_path_is_next_to_the_input_and_never_equal_to_it(self):
        self.assertEqual(engine.output_path(Path("/x/a.mkv")), Path("/x/a.cast.mp4"))
        for name in ("a.mp4", "a.cast.mp4", "a.b.c.mkv"):
            self.assertNotEqual(engine.output_path(Path("/x") / name), Path("/x") / name)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fixtures()

    def tearDown(self):  # keep tests independent: drop everything the engine generated
        for f in [*self.d.glob("*.cast.*"), self.d / "twice.mkv", self.d / "broken.mkv"]:
            f.unlink(missing_ok=True)

    def test_good_file_is_left_alone(self):
        sc, an, out = pipeline(self.d / "good.mp4")
        self.assertFalse(an.plan.changes)
        self.assertEqual((out.video, out.converted), (self.d / "good.mp4", False))
        self.assertFalse((self.d / "good.cast.mp4").exists())

    def test_missing_faststart_triggers_remux(self):
        self.assertFalse(engine.mp4_is_faststart(self.d / "nofaststart.mp4"))
        sc, an, out = pipeline(self.d / "nofaststart.mp4")
        self.assertEqual(an.plan.mode, "remux")
        self.assertTrue(engine.mp4_is_faststart(out.video))

    def test_mkv_remux_copies_streams(self):
        sc, an, out = pipeline(self.d / "plain.mkv")
        self.assertEqual(an.plan.mode, "remux")
        self.assertEqual(streams(out.video, "video")[0]["codec_name"], "h264")
        self.assertEqual(streams(out.video, "audio")[0]["codec_name"], "aac")
        self.assertTrue(out.verification.ok, out.verification.problems)

    @unittest.skipUnless(sys.platform == "darwin", "HEVC is only trusted on macOS")
    def test_hevc_kept_audio_converted(self):
        sc, an, out = pipeline(self.d / "hevc_ac3.mkv")
        v, a = streams(out.video, "video")[0], streams(out.video, "audio")[0]
        self.assertEqual((v["codec_name"], v["codec_tag_string"]), ("hevc", "hvc1"))
        self.assertEqual((a["codec_name"], a["channels"]), ("aac", 6))

    def test_force_h264(self):
        sc, an, out = pipeline(self.d / "hevc_ac3.mkv", force_h264=True)
        self.assertEqual(streams(out.video, "video")[0]["codec_name"], "h264")

    def test_ten_bit_h264_becomes_8_bit(self):
        sc, an, out = pipeline(self.d / "hi10.mkv")
        v = streams(out.video, "video")[0]
        self.assertEqual((v["codec_name"], v["pix_fmt"]), ("h264", "yuv420p"))

    def test_uhd_is_downscaled_to_1080p(self):
        sc, an, out = pipeline(self.d / "uhd.mkv")
        v = streams(out.video, "video")[0]
        self.assertEqual((v["width"], v["height"]), (1920, 1080))

    def test_interlaced_is_deinterlaced(self):
        sc = engine.scan(self.d / "interlaced.mkv")
        self.assertIn(sc.video.get("field_order"), ("tt", "bb", "tb", "bt"))
        an = engine.analyze(sc)
        self.assertTrue(an.plan.deinterlace)
        out = engine.convert(sc, an)
        self.assertEqual(streams(out.video, "video")[0].get("field_order", "progressive"), "progressive")

    def test_hdr_is_flagged(self):
        an = engine.analyze(engine.scan(self.d / "hdr.mkv"))
        self.assertTrue(any(c.name == "HDR" for c in an.checks))

    def test_bitrate_cap_and_override(self):
        sc = engine.scan(self.d / "burst.mkv")
        self.assertGreater(sc.video_peak_bps(), 6e6, "fixture should exceed the cap")
        capped = engine.analyze(sc, 6.0)
        self.assertEqual(capped.plan.video, "encode")
        self.assertIn("cap bitrate to 6 Mbit/s", capped.plan.video_why)
        out = engine.convert(sc, capped)
        new = engine.scan_bitrates(out.video, sc.duration)
        self.assertLessEqual(new[0].peak_bps(), 6e6 * engine.PEAK_TOLERANCE)
        uncapped = engine.analyze(sc, engine.NO_CAP_MBPS)
        self.assertEqual(uncapped.plan.video, "copy")  # only the MKV container needs fixing

    def test_outcome_carries_the_bitrate_curve(self):
        sc, an, out = pipeline(self.d / "plain.mkv")
        curve = out.curve
        self.assertGreaterEqual(len(curve), int(sc.duration))
        self.assertTrue(all(b >= a for a, b in zip(curve, curve[1:])), "cumulative means never decreasing")
        self.assertAlmostEqual(curve[-1], out.video.stat().st_size, delta=out.video.stat().st_size * 0.1)
        sc, an, out = pipeline(self.d / "good.mp4")                      # as-is files get a curve too
        self.assertGreater(out.curve[-1], 0)

    def test_presets_change_the_plan(self):
        sc = engine.scan(self.d / "burst.mkv")
        modes = {m: engine.analyze(sc, m).plan.mode for m in (6.0, 10.0, engine.NO_CAP_MBPS)}
        self.assertEqual(modes[6.0], "re-encode")
        self.assertEqual(modes[engine.NO_CAP_MBPS], "remux")

    def test_subtitles_exported_as_vtt(self):
        sc, an, out = pipeline(self.d / "subs.mkv")
        vtt = self.d / "subs.cast.eng.vtt"
        self.assertEqual(out.subtitles, [vtt])
        self.assertIn("Hello there", vtt.read_text())
        self.assertFalse(streams(out.video, "subtitle"))

    def test_audio_index_picks_second_track(self):
        sc = engine.scan(self.d / "dual.mkv")
        fra = next(a for a in sc.audios if a["tags"]["language"] == "fra")
        out = engine.convert(sc, engine.analyze(sc, audio_index=fra["index"]))
        got = streams(out.video, "audio")
        self.assertEqual((len(got), got[0]["tags"].get("language")), (1, "fra"))

    def test_lang_picks_second_track(self):
        sc, an, out = pipeline(self.d / "dual.mkv", lang="fr")
        self.assertEqual(streams(out.video, "audio")[0]["tags"].get("language"), "fra")

    def test_refuses_to_overwrite_then_allows_it(self):
        shutil.copy(self.d / "plain.mkv", self.d / "twice.mkv")
        sc = engine.scan(self.d / "twice.mkv")
        an = engine.analyze(sc)
        engine.convert(sc, an)
        with self.assertRaises(engine.OutputExists):
            engine.convert(sc, an)
        engine.convert(sc, an, overwrite=True)
        reused = engine.reuse_existing(sc, an)
        self.assertEqual((reused.video, reused.converted, reused.verification.ok),
                         (self.d / "twice.cast.mp4", False, True))

    def test_failed_encode_leaves_no_partial_file(self):
        bad = self.d / "broken.mkv"
        bad.write_bytes((self.d / "plain.mkv").read_bytes()[:3000])
        with self.assertRaises(engine.CastError):
            pipeline(bad)
        self.assertEqual(list(self.d.glob("broken.cast.mp4*")), [])

    def test_cancel_midway_cleans_up(self):
        class Boom(engine.NullProgress):
            def update(self, done):
                if done > 1:
                    raise KeyboardInterrupt

        sc = engine.scan(self.d / "uhd.mkv")
        an = engine.analyze(sc)
        with self.assertRaises(KeyboardInterrupt):
            engine.convert(sc, an, progress=Boom)
        self.assertEqual(list(self.d.glob("uhd.cast.mp4*")), [])

    def test_missing_ffprobe_target(self):
        with self.assertRaises(engine.CastError):
            engine.scan(self.d / "nope.mkv")

    def test_relative_names_that_look_like_urls_or_options_still_scan(self):
        with tempfile.TemporaryDirectory() as t:
            for name in ("Movie: Part 1.mp4", "-dash.mp4"):
                shutil.copy(self.d / "good.mp4", Path(t) / name)
            here = os.getcwd()
            os.chdir(t)
            self.addCleanup(os.chdir, here)
            for name in ("Movie: Part 1.mp4", "-dash.mp4"):
                sc = engine.scan(Path(name))
                self.assertTrue(sc.path.is_absolute(), sc.path)
                self.assertGreater(sc.duration, 0)

    def test_an_audio_pick_on_an_already_compatible_file_is_honoured(self):
        sc = engine.scan(self.d / "dual_aac.mp4")
        fra = next(a for a in sc.audios if a["tags"]["language"] == "fra")
        an = engine.analyze(sc, audio_index=fra["index"])
        self.assertEqual(an.plan.mode, "remux")           # otherwise VLC plays the file's default track
        out = engine.convert(sc, an)
        got = streams(out.video, "audio")
        self.assertEqual((len(got), got[0]["tags"].get("language")), (1, "fra"))

    def test_odd_dimensions_are_made_even_for_the_encoder(self):
        sc = engine.scan(self.d / "odd.avi")
        self.assertEqual((sc.video["width"], sc.video["height"]), (321, 241))
        out = engine.convert(sc, engine.analyze(sc))
        v = streams(out.video, "video")[0]
        self.assertEqual((v["codec_name"], v["width"] % 2, v["height"] % 2), ("h264", 0, 0))
        self.assertTrue(out.verification.ok, out.verification.problems)

    def test_webm_audio_is_converted_when_the_output_becomes_mp4(self):
        sc = engine.scan(self.d / "vorbis.webm")
        self.assertFalse(engine.analyze(sc, 6.0).plan.changes)             # left alone, still a WebM
        an = engine.analyze(sc, 0.2)                                       # a cap forces a rewrite into MP4
        self.assertEqual((an.plan.video, an.plan.audio), ("encode", "encode"))
        self.assertEqual(streams(engine.convert(sc, an).video, "audio")[0]["codec_name"], "aac")

    def test_a_stream_ffprobe_cannot_name_is_not_a_crash(self):
        info = {"format": {"format_name": "matroska,webm"},
                "streams": [{"index": 0, "codec_type": "video", "width": 640, "height": 360},
                            {"index": 1, "codec_type": "audio", "channels": 2}]}   # no codec_name, as ffprobe prints them
        an = engine.analyze(engine.Scan(Path("x.mkv"), info, {}, 60.0, None))
        self.assertEqual((an.plan.video, an.plan.audio), ("encode", "encode"))


if __name__ == "__main__":
    unittest.main()
