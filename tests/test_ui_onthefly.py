"""The wizard's side of "Cast on the fly": the question, the on-the-fly cast, the way back to a full conversion, the
Encoder row. The encoder itself is a fake (no ffmpeg here); the conversions that do run are tiny real ones."""

import io
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine, ui  # noqa: E402
from common import HAVE_FFMPEG, fixtures  # noqa: E402
from test_ui import TV, FakeNative, Script, found, make_wizard  # noqa: E402

Status = namedtuple("Status", "frontier_s speed slow")
WHEN = "When should the movie start playing?"
WEAK = engine.DEFAULT_MAX_MBPS


class FakeStream:
    """The public surface of onthefly.OnTheFly, with nothing behind it."""

    def __init__(self):
        self.error, self.closed = None, False

    def begin(self, start_seconds):
        pass

    def wait_ready(self, segments, timeout):
        pass

    def segment(self, n, timeout):
        return None

    def status(self):
        return Status(0.0, None, False)

    def close(self):
        self.closed = True


class FakeStreamOutcome:
    """The public surface of onthefly.StreamOutcome."""
    on_the_fly = True
    curve = None

    def __init__(self, video=Path("/movies/Big Movie.mkv"), duration=600.0, subtitles=(), scan="scan", analysis="analysis"):
        self.video, self.duration, self.subtitles = video, duration, list(subtitles)
        self.scan, self.analysis, self.seconds = scan, analysis, 1.5
        self.streams, self.cleaned = [], 0

    def make_stream(self):
        self.streams.append(FakeStream())
        return self.streams[-1]

    def cleanup(self):
        self.cleaned += 1


def render(w, **kw):
    """What the live panel prints, as text."""
    w.console.print(w.render_cast(ui.cast.PlayState(kw.pop("position", 100.0), 600.0, False, False, True), TV,
                                  Path("/m.mp4"), True, **kw))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Question(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fixtures()

    def tearDown(self):
        for f in self.d.glob("*.cast.*"):
            f.unlink(missing_ok=True)

    def test_a_video_re_encode_asks_and_convert_first_is_the_default(self):
        src = self.d / "burst.mkv"
        script = Script(src, WEAK, "full", False, False)
        w, out = make_wizard(script, found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertEqual(script.choices[WHEN], ["Convert first, then play (recommended)",
                                                "Cast on the fly, while it converts (experimental)"])
        self.assertEqual(script.defaults[WHEN], ["full"])
        self.assertTrue((self.d / "burst.cast.mp4").exists(), "\"full\" is today's flow")
        self.assertIn("Verified", out.getvalue())
        self.assertLess(script.asked.index("How good is the Wi-Fi to your TV?"), script.asked.index(WHEN))

    def test_every_video_re_encode_fixture_gets_the_question(self):
        for name in ("uhd.mkv", "hi10.mkv", "interlaced.mkv", "odd.avi"):
            with self.subTest(name):
                script = Script(WEAK, "otf")   # leaves right after the question
                w, _ = make_wizard(script, stream_prepare=lambda sc, an, progress: FakeStreamOutcome())
                w.prepare(self.d / name)
                self.assertIn(WHEN, script.asked)

    def test_it_is_not_asked_for_as_is_remux_or_audio_only_plans(self):
        for name, mode in (("good.mp4", "as-is"), ("plain.mkv", "remux"), ("hevc_ac3.mkv", "re-encode")):
            with self.subTest(name):
                script = Script(WEAK)           # anything else asked would raise
                w, _ = make_wizard(script)
                outcome = w.prepare(self.d / name)
                self.assertNotIn(WHEN, script.asked)
                self.assertIsNotNone(outcome)
                self.assertFalse(getattr(outcome, "on_the_fly", False))
                self.assertEqual(engine.analyze(engine.scan(self.d / name), WEAK).plan.mode, mode)
        self.assertEqual(engine.analyze(engine.scan(self.d / "hevc_ac3.mkv"), WEAK).plan.video, "copy")

    def test_on_the_fly_hands_over_to_stream_prepare_and_returns_its_outcome(self):
        src = self.d / "burst.mkv"
        made, calls = FakeStreamOutcome(video=src), []

        def prepare(sc, an, progress):
            calls.append((sc, an, progress))
            return made

        script = Script(WEAK, "otf")
        w, out = make_wizard(script, stream_prepare=prepare)
        self.assertIs(w.prepare(src), made)
        (sc, an, progress), = calls
        self.assertEqual((sc.path, an.plan.video, an.max_mbps), (src, "encode", WEAK))
        self.assertEqual(progress, w.bar)
        self.assertFalse((self.d / "burst.cast.mp4").exists(), "nothing is converted up front")
        text = out.getvalue()
        self.assertIn("Ready", text)
        self.assertIn("in a few seconds", text)
        self.assertNotIn("This needs a re-encode", text)
        self.assertEqual(script.asked, ["How good is the Wi-Fi to your TV?", WHEN])

    def test_on_the_fly_goes_through_run_to_the_cast_step(self):
        src = self.d / "burst.mkv"
        made = FakeStreamOutcome(video=src)
        script = Script(src, WEAK, "otf", True, TV, "", "done", False)
        w, out = make_wizard(script, found=[found(src)], stream_prepare=lambda sc, an, progress: made)
        self.assertEqual(w.run(), 0)
        self.assertIs(FakeNative.instances[0].stream, made.streams[0])
        self.assertEqual(made.cleaned, 1)
        self.assertIn("Casting to Living Room TV", out.getvalue())

    def test_a_failure_while_preparing_shows_like_any_other_error(self):
        src = self.d / "burst.mkv"

        def prepare(sc, an, progress):
            raise engine.CastError("not enough disk space: need ~9 GB, have 1 GB")

        w, out = make_wizard(Script(src, WEAK, "otf", False), found=[found(src)], stream_prepare=prepare)
        self.assertEqual(w.run(), 0)
        self.assertIn("Something went wrong", out.getvalue())
        self.assertIn("not enough disk space", out.getvalue())
        self.assertEqual(FakeNative.instances, [])

    def test_without_an_injected_function_the_real_one_is_used(self):
        try:
            from castmymovie import onthefly
        except ImportError:
            self.skipTest("onthefly is not there yet")
        w, _ = make_wizard(Script())
        with mock.patch.object(onthefly, "prepare", return_value="outcome") as prepare:
            self.assertEqual(w.stream_prepare("sc", "an", "progress"), "outcome")
        prepare.assert_called_once_with("sc", "an", "progress")


class StreamingCast(unittest.TestCase):
    def cast(self, *answers, outcome=None, keys=("q",)):
        outcome = outcome or FakeStreamOutcome()
        script = Script(*answers)
        w, out = make_wizard(script, keys=keys)
        return w, out, script, outcome

    def test_the_cast_question_is_a_confirm_and_the_way_out_is_offered(self):
        w, out, script, outcome = self.cast(True, TV, "", "done")
        w.offer_cast(outcome)
        self.assertIn("Play it on your TV now?", script.asked)
        self.assertNotIn("Play it on your TV now?", script.choices)     # a confirm, not a select
        self.assertEqual(script.choices["What next?"], ["Cast it again", "Convert it fully, then cast (slower, reliable)",
                                                        "Choose another TV", "Done with this movie"])
        self.assertNotIn("VLC", out.getvalue())

    def test_the_session_gets_a_stream_and_the_source_for_its_title(self):
        w, out, script, outcome = self.cast(True, TV, "0:30", "done")
        w.offer_cast(outcome)
        session, = FakeNative.instances
        self.assertEqual(session.args, (TV, outcome.video, 30.0))
        self.assertEqual(session.duration, 600.0)
        self.assertIs(session.stream, outcome.streams[0])
        self.assertEqual(session.calls, ["stop"])

    def test_every_attempt_gets_a_new_stream(self):
        w, _, script, outcome = self.cast(True, TV, "", "again", "", "again", "", "done")
        w.offer_cast(outcome)
        self.assertEqual(len(FakeNative.instances), 3)
        self.assertEqual(len(outcome.streams), 3)
        self.assertEqual([id(s.stream) for s in FakeNative.instances], [id(s) for s in outcome.streams])
        self.assertEqual(len({id(s) for s in outcome.streams}), 3)
        self.assertEqual(script.asked.count("Which TV?"), 1)   # the TV is remembered

    def test_subtitles_are_still_offered(self):
        vtt = Path("/movies/Big Movie.cast.eng.vtt")
        w, _, script, outcome = self.cast(True, TV, vtt, "", "done", outcome=FakeStreamOutcome(subtitles=[vtt]))
        w.offer_cast(outcome)
        self.assertEqual(FakeNative.instances[0].subtitles, [vtt])

    def test_cleanup_when_the_movie_is_done(self):
        w, _, _, outcome = self.cast(True, TV, "", "done")
        w.offer_cast(outcome)
        self.assertGreaterEqual(outcome.cleaned, 1)

    def test_cleanup_when_not_casting_at_all(self):
        w, _, _, outcome = self.cast(False)
        w.offer_cast(outcome)
        self.assertEqual((outcome.cleaned, FakeNative.instances), (1, []))
        w, _, _, outcome = self.cast(True, None)    # no TV picked
        w.offer_cast(outcome)
        self.assertEqual(outcome.cleaned, 1)

    def test_cleanup_after_a_failed_start_and_the_fully_converted_way_out_is_the_default(self):
        w, out, script, outcome = self.cast(True, TV, "", "done")
        FakeNative.fail = True
        w.offer_cast(outcome)
        self.assertIn("firewall", out.getvalue())
        self.assertEqual(script.defaults["What next?"], ["full"])
        self.assertEqual(FakeNative.instances[0].calls, ["stop"])
        self.assertNotIn("Try with VLC", script.choices["What next?"])
        self.assertGreaterEqual(outcome.cleaned, 1)

    def test_the_default_after_a_good_cast_is_to_cast_again(self):
        w, _, script, outcome = self.cast(True, TV, "", "done")
        w.offer_cast(outcome)
        self.assertEqual(script.defaults["What next?"], ["again"])

    def test_cleanup_on_ctrl_c_at_a_prompt(self):
        w, _, _, outcome = self.cast(True, TV, "", "KeyboardInterrupt")
        with self.assertRaises(KeyboardInterrupt):
            w.offer_cast(outcome)
        self.assertGreaterEqual(outcome.cleaned, 1)

    def test_cleanup_on_ctrl_c_while_starting_and_while_playing(self):
        w, _, _, outcome = self.cast(True, TV, "")
        with mock.patch.object(FakeNative, "start", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                w.offer_cast(outcome)
        self.assertEqual(FakeNative.instances[0].calls, ["stop"])
        self.assertEqual(outcome.cleaned, 1)
        w, _, _, outcome = self.cast(True, TV, "")
        with mock.patch.object(FakeNative, "state", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                w.offer_cast(outcome)
        self.assertEqual(FakeNative.instances[0].calls, ["stop"])
        self.assertEqual(outcome.cleaned, 1)

    def test_cleanup_when_something_unexpected_blows_up(self):
        w, _, _, outcome = self.cast(True, TV, "")
        with mock.patch.object(FakeNative, "state", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                w.offer_cast(outcome)
        self.assertEqual(outcome.cleaned, 1)

    def test_an_ordinary_outcome_is_untouched(self):
        # a converted file has no on_the_fly marker: nothing to clean up, no stream argument, no way out offered
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "m.cast.mp4"
            video.write_bytes(b"x")
            outcome = engine.Outcome(video, True, None, [], 1.0, None, 600.0)
            w, _, script, _ = self.cast(True, TV, "", "done")
            w.offer_cast(outcome)
            self.assertNotIn("Convert it fully, then cast (slower, reliable)", script.choices["What next?"])
            self.assertIsNone(FakeNative.instances[0].stream)


class FullFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "Big Movie.cast.mp4"
        self.video.write_bytes(b"x" * 10)
        self.converted = engine.Outcome(self.video, True, None, [], 42.0, [0, 1000, 2000], 600.0)

    def run_flow(self, *answers, convert=None, reuse=None):
        outcome = FakeStreamOutcome(scan=mock.sentinel.scan, analysis=mock.sentinel.analysis)
        script = Script(*answers)
        w, out = make_wizard(script)
        convert = convert or mock.Mock(return_value=self.converted)
        with mock.patch.object(ui.engine, "convert", convert), \
                mock.patch.object(ui.engine, "reuse_existing", reuse or mock.Mock(return_value=self.converted)):
            w.offer_cast(outcome)
        return w, out, script, outcome, convert

    def test_converting_fully_then_casting_the_file(self):
        w, out, script, outcome, convert = self.run_flow(True, TV, "", "full", "", "done")
        convert.assert_called_once_with(mock.sentinel.scan, mock.sentinel.analysis, w.bar)
        self.assertEqual(script.asked.count("Play it on your TV now?"), 1)     # no starting over
        self.assertEqual(script.asked.count("Which TV?"), 1)
        first, second = FakeNative.instances
        self.assertIs(first.stream, outcome.streams[0])
        self.assertEqual(second.args, (TV, self.video, 0.0))
        self.assertIsNone(second.stream)
        self.assertEqual(second.duration, 600.0)
        self.assertEqual(len(outcome.streams), 1)
        self.assertGreaterEqual(outcome.cleaned, 1)
        self.assertIn("done in 00:42", out.getvalue())                         # the usual "Ready" panel
        # it is an ordinary file now: the way out is gone
        self.assertNotIn("Convert it fully, then cast (slower, reliable)", script.offered["What next?"][1])

    def test_the_stream_is_cleaned_before_the_converted_file_is_cast(self):
        order = []
        w, out, script, outcome, _ = None, None, None, None, None
        outcome = FakeStreamOutcome(scan=1, analysis=2)
        outcome.cleanup = lambda: order.append("cleanup")
        script = Script(True, TV, "", "full", "", "done")
        w, out = make_wizard(script)
        with mock.patch.object(ui.engine, "convert", return_value=self.converted), \
                mock.patch.object(FakeNative, "start", lambda self: order.append(("cast", self.args[1].name))):
            w.offer_cast(outcome)
        self.assertEqual(order[:3], [("cast", "Big Movie.mkv"), "cleanup", ("cast", "Big Movie.cast.mp4")])

    def test_an_existing_converted_file_can_be_reused(self):
        exists = engine.OutputExists(self.video)
        convert = mock.Mock(side_effect=[exists])
        reuse = mock.Mock(return_value=self.converted)
        w, out, script, outcome, _ = self.run_flow(True, TV, "", "full", "use", "", "done",
                                                   convert=convert, reuse=reuse)
        self.assertIn("Big Movie.cast.mp4 already exists.", script.asked)
        reuse.assert_called_once_with(mock.sentinel.scan, mock.sentinel.analysis, w.bar)
        self.assertEqual(FakeNative.instances[1].args[1], self.video)

    def test_an_existing_converted_file_can_be_overwritten(self):
        convert = mock.Mock(side_effect=[engine.OutputExists(self.video), self.converted])
        w, out, script, outcome, _ = self.run_flow(True, TV, "", "full", "redo", "", "done", convert=convert)
        self.assertEqual(convert.call_args_list[1].kwargs, {"overwrite": True})
        self.assertEqual(FakeNative.instances[1].args[1], self.video)

    def test_cancelling_the_overwrite_question_stays_on_the_fly(self):
        convert = mock.Mock(side_effect=[engine.OutputExists(self.video)])
        w, out, script, outcome, _ = self.run_flow(True, TV, "", "full", "cancel", "", "done", convert=convert)
        self.assertEqual(len(FakeNative.instances), 2)
        self.assertIs(FakeNative.instances[1].stream, outcome.streams[1])      # still on the fly, a fresh encoder
        self.assertIn("Convert it fully, then cast (slower, reliable)", script.offered["What next?"][1])

    def test_a_failed_conversion_is_reported_and_the_cast_goes_on(self):
        convert = mock.Mock(side_effect=engine.CastError("not enough disk space: need ~9 GB, have 1 GB"))
        w, out, script, outcome, _ = self.run_flow(True, TV, "", "full", "", "done", convert=convert)
        self.assertIn("not enough disk space", out.getvalue())
        self.assertEqual(len(FakeNative.instances), 2)
        self.assertIsNotNone(FakeNative.instances[1].stream)

    def test_ctrl_c_during_the_conversion_cleans_up(self):
        convert = mock.Mock(side_effect=KeyboardInterrupt)
        outcome = FakeStreamOutcome()
        w, _ = make_wizard(Script(True, TV, "", "full"))
        with mock.patch.object(ui.engine, "convert", convert):
            with self.assertRaises(KeyboardInterrupt):
                w.offer_cast(outcome)
        self.assertGreaterEqual(outcome.cleaned, 1)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class FullFallbackForReal(unittest.TestCase):
    def test_the_movie_really_gets_converted_from_the_scan_and_analysis_of_the_stream(self):
        d = fixtures()
        src = d / "burst.mkv"
        sc = engine.scan(src)
        an = engine.analyze(sc, WEAK)
        outcome = FakeStreamOutcome(video=src, duration=sc.duration, scan=sc, analysis=an)
        w, out = make_wizard(Script(True, TV, "", "full", "", "done"))
        try:
            w.offer_cast(outcome)
            self.assertTrue((d / "burst.cast.mp4").exists())
            self.assertEqual(FakeNative.instances[1].args[1], d / "burst.cast.mp4")
            self.assertIn("Verified", out.getvalue())
        finally:
            for f in d.glob("*.cast.*"):
                f.unlink(missing_ok=True)


class EncoderRow(unittest.TestCase):
    def render(self, status, position=1460.0, colour=False):
        out = io.StringIO()
        console = ui.Console(file=out, theme=ui.THEME, width=130, force_terminal=colour, color_system="standard" if colour else None)
        w, _ = make_wizard(Script())
        w.console = console
        render(w, position=position, encoder=status)
        return out.getvalue()

    def test_row_shows_progress_speed_and_how_far_ahead_it_is(self):
        text = self.render(Status(41 * 60 + 20, 8.4, False))      # the TV is at 24:20
        self.assertIn("Encoder", text)
        self.assertIn("encoded to 41:20 (8.4x)", text)
        self.assertIn("17 min ahead", text)
        self.assertNotIn("slow", text)

    def test_short_leads_are_shown_in_seconds_and_a_lead_never_goes_negative(self):
        self.assertIn("30 s ahead", self.render(Status(130.0, 5.0, False), position=100.0))
        self.assertIn("0 s ahead", self.render(Status(90.0, 5.0, True), position=100.0))

    def test_a_slow_encoder_is_flagged_in_yellow(self):
        text = self.render(Status(300.0, 0.9, True), position=290.0, colour=True)
        self.assertIn("slow", text)
        self.assertIn("\x1b[33m", text)
        self.assertNotIn("slow", self.render(Status(300.0, 1.9, False), position=290.0))

    def test_an_unknown_speed_is_left_out(self):
        text = self.render(Status(300.0, None, False), position=100.0)
        self.assertIn("encoded to 05:00", text)
        self.assertNotIn("(", text.split("Encoder")[1].split("\n")[0])

    def test_no_row_without_an_encoder(self):
        self.assertNotIn("Encoder", self.render(None))

    def test_odd_numbers_do_not_break_the_panel(self):
        nan = float("nan")
        self.assertNotIn("Encoder", self.render(Status(nan, 2.0, False)))
        self.assertIn("Encoder", self.render(Status(100.0, nan, False)))

    def test_the_live_panel_asks_the_session(self):
        w, out = make_wizard(Script(True, TV, "", "done"))
        FakeNative.encoder = Status(41 * 60 + 20, 8.4, False)
        w.offer_cast(FakeStreamOutcome())
        self.assertIn("encoded to 41:20 (8.4x)", out.getvalue())

    def test_sessions_without_an_encoder_get_no_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "m.mp4"
            video.write_bytes(b"x")
            w, out = make_wizard(Script(True, TV, "", "done"))
            w.offer_cast(engine.Outcome(video, True, None, [], 1.0, None, 600.0))
            self.assertNotIn("Encoder", out.getvalue())
            self.assertIn("Casting to", out.getvalue())


if __name__ == "__main__":
    unittest.main()
