"""Whether HEVC is kept is the TV's business, not this Mac's: the TV decodes the video. It used to hang on the Mac's
Chrome version (a leftover from casting through Chrome), so an old Chrome forced a slow re-encode for nothing, and no
Chrome kept HEVC even for a TV that cannot play it. The wizard now asks, and the answer reaches the engine as
force_h264."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine  # noqa: E402
from common import HAVE_FFMPEG, audio_src, ff, fixtures, video_src  # noqa: E402
from test_ui import TV, FakeNative, Script, found, make_wizard  # noqa: E402
from test_ui_onthefly import FakeStreamOutcome  # noqa: E402

CODEC = "This movie is HEVC (H.265). Can your TV play HEVC?"
WIFI = "How good is the Wi-Fi to your TV?"
WHEN = "When should the movie start playing?"
WEAK = engine.DEFAULT_MAX_MBPS


def video_codec(path):
    return next(s for s in engine.probe(Path(path))["streams"] if s["codec_type"] == "video")["codec_name"]


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fixtures()

    def tearDown(self):
        for f in self.d.glob("*.cast.*"):
            f.unlink(missing_ok=True)

    def test_keeping_hevc_does_not_depend_on_this_macs_chrome_or_system(self):
        # a recent Chrome, one older than 107, none at all, another system: the TV decodes it either way
        for chrome, platform in ((157, "darwin"), (100, "darwin"), (None, "darwin"), (157, "linux")):
            with self.subTest(chrome=chrome, platform=platform), \
                    mock.patch.object(engine, "chrome_major", create=True, return_value=chrome), \
                    mock.patch.object(sys, "platform", platform):
                sc = engine.scan(self.d / "hevc_ac3.mkv")
                kept = engine.analyze(sc, engine.NO_CAP_MBPS)
                self.assertEqual(kept.plan.video, "copy", kept.plan.video_why)
                self.assertTrue(next(c for c in kept.checks if c.name == "Video codec").ok)
                forced = engine.analyze(sc, engine.NO_CAP_MBPS, force_h264=True)
                self.assertEqual(forced.plan.video, "encode")
                self.assertEqual(forced.plan.video_why, ["H.264 chosen (TV without HEVC)"])

    def test_a_kept_hevc_output_reused_when_h264_is_wanted_is_flagged(self):
        sc = engine.scan(self.d / "hevc_ac3.mkv")
        kept = engine.convert(sc, engine.analyze(sc))
        self.assertEqual(video_codec(kept.video), "hevc")
        an = engine.analyze(sc, force_h264=True)
        with self.assertRaises(engine.OutputExists):
            engine.convert(sc, an)
        reused = engine.reuse_existing(sc, an)
        self.assertEqual(reused.verification.problems, ["video is HEVC, not H.264: convert again (overwrite)"])
        redone = engine.convert(sc, an, overwrite=True)
        self.assertEqual(video_codec(redone.video), "h264")
        self.assertTrue(redone.verification.ok, redone.verification.problems)
        # an H.264 output plays on any TV, so reusing it when HEVC could have been kept is fine
        self.assertTrue(engine.reuse_existing(sc, engine.analyze(sc)).verification.ok)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Wizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fixtures()
        cls.t = Path(tempfile.mkdtemp(prefix="castmymovie-hevc-"))
        # HEVC that no TV setting can keep (4:2:2): it is re-encoded anyway, so there is nothing to ask
        ff(*video_src("320x180", 2), *audio_src(dur=2), "-c:v", "libx265", "-x265-params", "log-level=error",
           "-pix_fmt", "yuv422p", "-crf", "30", "-c:a", "aac", str(cls.t / "hevc422.mkv"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.t, True)

    def tearDown(self):
        for f in [*self.d.glob("*.cast.*"), *self.t.glob("*.cast.*")]:
            f.unlink(missing_ok=True)

    def test_an_hevc_movie_asks_first_and_keeps_hevc_by_default(self):
        src = self.d / "hevc_ac3.mkv"
        script = Script(src, "hevc", WEAK, True, TV, "", "done", False)
        w, out = make_wizard(script, found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertEqual(script.asked[1:3], [CODEC, WIFI])     # before the presets, so their previews are right
        self.assertEqual(script.defaults[CODEC], ["hevc"])
        self.assertEqual(script.choices[CODEC], [
            "Yes, keep HEVC (Google TV, Chromecast with Google TV, Chromecast Ultra)",
            "No, convert to H.264 (older Chromecasts; a slow re-encode)"])
        self.assertTrue(all("remux + audio" in c for c in script.choices[WIFI][:-1]), script.choices[WIFI])
        self.assertNotIn(WHEN, script.asked)
        self.assertEqual(video_codec(self.d / "hevc_ac3.cast.mp4"), "hevc")
        self.assertEqual(FakeNative.instances[0].args[1], self.d / "hevc_ac3.cast.mp4")

    def test_answering_h264_reaches_the_engine_as_force_h264(self):
        src = self.d / "hevc_ac3.mkv"
        script = Script("h264", WEAK, "full")
        w, out = make_wizard(script)
        with mock.patch.object(engine, "analyze", wraps=engine.analyze) as analyze:
            outcome = w.prepare(src)
        asked = [c.kwargs.get("force_h264", False) for c in analyze.call_args_list]
        self.assertEqual(asked, [False] + [True] * (len(asked) - 1))   # the first call only checks HEVC can be kept
        self.assertTrue(all("re-encode (slow)" in c for c in script.choices[WIFI][:-1]), script.choices[WIFI])
        self.assertEqual(video_codec(outcome.video), "h264")
        self.assertTrue(outcome.verification.ok, outcome.verification.problems)
        self.assertIn("H.264 chosen (TV without HEVC)", out.getvalue())

    def test_on_the_fly_gets_the_h264_choice_too(self):
        got = []
        script = Script("h264", WEAK, "otf")
        w, _ = make_wizard(script, stream_prepare=lambda sc, an, progress: got.append(an) or FakeStreamOutcome())
        w.prepare(self.d / "hevc_ac3.mkv")
        self.assertEqual((got[0].plan.video, got[0].plan.video_why), ("encode", ["H.264 chosen (TV without HEVC)"]))

    def test_reusing_a_kept_hevc_file_after_choosing_h264_says_so(self):
        src = self.d / "hevc_ac3.mkv"
        make_wizard(Script("hevc", WEAK))[0].prepare(src)                        # an earlier run kept HEVC
        w, out = make_wizard(Script("h264", WEAK, "full", "use"))
        w.prepare(src)
        self.assertIn("video is HEVC, not H.264: convert again (overwrite)", out.getvalue())
        w, out = make_wizard(Script("h264", WEAK, "full", "redo"))
        self.assertEqual(video_codec(w.prepare(src).video), "h264")
        self.assertIn("Verified", out.getvalue())

    def test_no_question_when_there_is_no_hevc_to_keep(self):
        for src in (self.d / "good.mp4", self.d / "plain.mkv", self.d / "uhd.mkv", self.t / "hevc422.mkv"):
            with self.subTest(src.name):
                script = Script(WEAK, "otf") if src.name in ("uhd.mkv", "hevc422.mkv") else Script(WEAK)
                w, _ = make_wizard(script, stream_prepare=lambda sc, an, progress: FakeStreamOutcome())
                w.prepare(src)
                self.assertNotIn(CODEC, script.asked)


if __name__ == "__main__":
    unittest.main()
