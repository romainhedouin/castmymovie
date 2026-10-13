import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine, finder, telemetry, ui  # noqa: E402
from common import HAVE_FFMPEG, fixtures  # noqa: E402

TV = cast.Receiver("Living Room TV", "192.168.8.63", "MiTV-MOEU0")
TV2 = cast.Receiver("Bedroom TV", "192.168.8.70")


class Script(ui.Prompter):
    """Answers prompts from a list, in order. Remembers what was asked and which choices were offered."""

    def __init__(self, *answers):
        self.answers, self.asked, self.choices = list(answers), [], {}

    def _next(self, message):
        self.asked.append(message)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {message!r}")
        return self.answers.pop(0)

    def select(self, message, choices, default=None, filterable=False):
        self.choices[message] = [t if isinstance(t, str) else "".join(x[1] for x in t) for t, _ in choices]
        a = self._next(message)
        if a == "KeyboardInterrupt":
            raise KeyboardInterrupt
        return a

    def confirm(self, message, default=True):
        return self._next(message)

    def text(self, message, default=""):
        return self._next(message)

    def path(self, message):
        return self._next(message)


class FakeCaster:
    instances, fail = [], False

    def __init__(self, vlc, receiver, media, start=0.0):
        self.args = (vlc, receiver, media, start)
        self.calls, self.pos, self.paused = [], start, False
        FakeCaster.instances.append(self)

    def start(self):
        if FakeCaster.fail:
            raise engine.CastError("TV said no")

    def state(self):
        self.pos += 5
        return cast.PlayState(self.pos, 600.0, self.paused, False, True)

    def toggle_pause(self):
        self.calls.append("pause")
        self.paused = not self.paused

    def seek_by(self, d):
        self.calls.append(("seek", d))

    def stop(self):
        self.calls.append("stop")


class FakeKeys:
    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, timeout=0.5):
        return self.seq.pop(0) if self.seq else "q"


class FakeTele:
    """Stands in for the background telemetry thread: serves a fixed snapshot."""

    def __init__(self, snap):
        self.latest, self.position, self.started, self.stopped = snap, 0.0, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


GOOD = telemetry.Snapshot(tv_state="PLAYING", tv_position=100.0, buffer_s=31.0, delivered_mbps=1.9,
                          needed_mbps=1.5, retx_pct=0.1, rtt_ms=19.0)


def make_wizard(script, found=(), receivers=(TV,), vlc="/fake/VLC", keys=("q",), tele=None):
    out = io.StringIO()
    console = ui.Console(file=out, theme=ui.THEME, width=130, force_terminal=False)
    FakeCaster.instances, FakeCaster.fail = [], False
    discover = receivers if callable(receivers) else (lambda: list(receivers))
    w = ui.Wizard(console=console, prompt=script, keys=FakeKeys(keys), find_videos=lambda: list(found),
                  discover=discover, find_vlc=lambda: vlc, caster=FakeCaster,
                  telemetry=(lambda session, receiver, curve: tele(curve)) if tele else None)
    return w, out


def found(path):
    return finder.Found(Path(path), Path(path).stat().st_size, 0)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fixtures()

    def tearDown(self):
        for f in [*self.d.glob("*.cast.*"), *self.d.glob("Movie*")]:
            f.unlink(missing_ok=True)

    def test_full_happy_path_pick_convert_cast_with_controls(self):
        src = self.d / "plain.mkv"
        script = Script(src, 6.0, True, TV, "1:30", "done", False)
        w, out = make_wizard(script, found=[found(src)], keys=["space", "right", "down", "q"])
        self.assertEqual(w.run(), 0)
        text = out.getvalue()
        self.assertTrue((self.d / "plain.cast.mp4").exists())
        self.assertIn("Pick a movie", text)
        self.assertIn("Choose the quality", text)
        self.assertIn("Plan: remux", text)
        self.assertIn("Verified", text)
        self.assertIn("Casting to Living Room TV", text)
        cs = FakeCaster.instances[0]
        self.assertEqual(cs.args[1:], (TV, self.d / "plain.cast.mp4", 90.0))
        self.assertEqual(cs.calls, ["pause", ("seek", 10), ("seek", -120), "stop"])

    def test_every_arrow_key_has_its_step(self):
        src = self.d / "plain.mkv"
        script = Script(src, 6.0, True, TV, "", "done", False)
        w, out = make_wizard(script, found=[found(src)], keys=["left", "right", "down", "up", "q"])
        w.run()
        self.assertEqual(FakeCaster.instances[0].calls, [("seek", -10), ("seek", 10), ("seek", -120), ("seek", 120), "stop"])
        self.assertIn("±10 s", out.getvalue())
        self.assertIn("±2 min", out.getvalue())

    def test_panel_shows_buffer_estimate_and_link_health(self):
        src = self.d / "plain.mkv"
        made = []

        def tele(curve):
            made.append(curve)
            return FakeTele(GOOD)

        w, out = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)], tele=tele)
        w.run()
        text = out.getvalue()
        self.assertIn("Buffer", text)
        self.assertIn("≈ 30 s", text)            # 31 s is rounded to the nearest 5 and flagged as an estimate
        self.assertIn("(estimate)", text)
        self.assertIn("comfortable", text)
        self.assertIn("1.9 Mbit/s (needs 1.5)", text)
        self.assertIn("19 ms", text)
        self.assertIn("0.1% retransmits", text)
        self.assertIn("● good", text)
        self.assertTrue(made and len(made[0]) > 3, "the movie's bitrate curve must reach the telemetry")

    def test_panel_tells_the_truth_when_the_tv_is_buffering(self):
        src = self.d / "plain.mkv"
        snap = telemetry.Snapshot(tv_state="BUFFERING", buffer_s=2.0, delivered_mbps=0.3, retx_pct=6.0)
        w, out = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)],
                             tele=lambda curve: FakeTele(snap))
        w.run()
        text = out.getvalue()
        self.assertIn("buffering…", text)
        self.assertIn("critical", text)
        self.assertIn("● poor", text)

    def test_panel_says_refilling_after_a_seek_instead_of_critical(self):
        src = self.d / "plain.mkv"
        snap = telemetry.Snapshot(tv_state="PLAYING", buffer_s=2.0, refilling=True, rtt_ms=20.0)
        w, out = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)],
                             tele=lambda curve: FakeTele(snap))
        w.run()
        text = out.getvalue()
        self.assertIn("refilling", text)
        self.assertNotIn("critical", text)
        self.assertNotIn("● poor", text)

    def test_panel_without_any_readings_says_it_is_measuring(self):
        src = self.d / "plain.mkv"
        w, out = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)],
                             tele=lambda curve: FakeTele(telemetry.Snapshot()))
        w.run()
        self.assertIn("Measuring the link", out.getvalue())

    def test_telemetry_is_started_and_always_stopped(self):
        src = self.d / "plain.mkv"
        holder = []

        def tele(curve):
            holder.append(FakeTele(GOOD))
            return holder[-1]

        w, _ = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)], tele=tele)
        w.run()
        self.assertTrue(holder[0].started and holder[0].stopped)

    def test_no_telemetry_for_a_cast_without_a_process(self):
        # default factory + FakeCaster (no .proc): the panel must still render, just without health rows
        src = self.d / "plain.mkv"
        w, out = make_wizard(Script(src, 6.0, True, TV, "", "done", False), found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertNotIn("Buffer", out.getvalue())
        self.assertIn("Casting to Living Room TV", out.getvalue())

    def test_quality_menu_previews_what_each_choice_will_do(self):
        src = self.d / "burst.mkv"
        script = Script(src, engine.NO_CAP_MBPS, False, False)
        w, out = make_wizard(script, found=[found(src)])
        w.run()
        labels = script.choices["How good is the Wi-Fi to your TV?"]
        self.assertTrue(any(l.startswith("Weak Wi-Fi") and "re-encode" in l for l in labels), labels)
        self.assertTrue(any(l.startswith("Original") and "remux" in l for l in labels), labels)
        self.assertIn("Custom…", labels)

    def test_brackets_in_names_are_shown_literally(self):
        import shutil
        src = self.d / "Movie [1080p] [BluRay].mkv"
        shutil.copy(self.d / "plain.mkv", src)
        w, out = make_wizard(Script(src, 6.0, False, False), found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertGreaterEqual(out.getvalue().count("Movie [1080p] [BluRay]"), 2)
        src.unlink()

    def test_custom_bitrate_validates_input(self):
        src = self.d / "plain.mkv"
        script = Script(src, "custom", "abc", "-3", "12.5", False, False)
        w, out = make_wizard(script, found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertIn("Enter a number", out.getvalue())
        self.assertIn("12.5 Mbit/s", out.getvalue())

    def test_already_compatible_movie_is_not_touched(self):
        src = self.d / "good.mp4"
        w, out = make_wizard(Script(src, 6.0, False, False), found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertIn("nothing to convert", out.getvalue())
        self.assertFalse((self.d / "good.cast.mp4").exists())

    def test_audio_track_question_only_when_there_are_several(self):
        src = self.d / "dual.mkv"
        sc = engine.scan(src)
        fra = next(a for a in sc.audios if a["tags"]["language"] == "fra")["index"]
        script = Script(src, fra, 6.0, False, False)
        w, out = make_wizard(script, found=[found(src)])
        w.run()
        self.assertIn("several audio tracks", " ".join(script.asked))
        self.assertIn("[fra]", out.getvalue())  # language tags must survive Rich markup
        self.assertEqual(engine.probe(self.d / "dual.cast.mp4")["streams"][1]["tags"]["language"], "fra")

    def test_existing_output_can_be_reused(self):
        src = self.d / "plain.mkv"
        w, _ = make_wizard(Script(src, 6.0, False, False), found=[found(src)])
        w.run()
        mtime = (self.d / "plain.cast.mp4").stat().st_mtime_ns
        script = Script(src, 6.0, "use", False, False)
        w, out = make_wizard(script, found=[found(src)])
        self.assertEqual(w.run(), 0)
        self.assertEqual((self.d / "plain.cast.mp4").stat().st_mtime_ns, mtime)
        self.assertTrue(any("already exists" in m for m in script.asked), script.asked)

    def test_typed_path_and_bad_path(self):
        src = self.d / "plain.mkv"
        script = Script("__path__", "/no/such/file.mkv", "__path__", f"'{src}'", 6.0, False, False)
        w, out = make_wizard(script)
        self.assertEqual(w.run(), 0)
        self.assertIn("That is not a file", out.getvalue())
        self.assertTrue((self.d / "plain.cast.mp4").exists())

    def test_no_vlc_explains_how_to_get_it(self):
        src = self.d / "plain.mkv"
        w, out = make_wizard(Script(src, 6.0, False), found=[found(src)], vlc=None)
        self.assertEqual(w.run(), 0)
        self.assertIn("brew install --cask vlc", out.getvalue())

    def test_no_tv_found_then_search_again(self):
        src = self.d / "plain.mkv"
        calls = []

        def discover():
            calls.append(1)
            return [] if len(calls) == 1 else [TV]

        w, out = make_wizard(Script(src, 6.0, True, "again", TV, "", "done", False), found=[found(src)], receivers=discover)
        self.assertEqual(w.run(), 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("No Cast devices found", out.getvalue())

    def test_start_time_is_validated(self):
        src = self.d / "plain.mkv"
        w, out = make_wizard(Script(src, 6.0, True, TV, "nonsense", "45:10", "done", False), found=[found(src)])
        w.run()
        self.assertIn("hours:minutes:seconds", out.getvalue())
        self.assertEqual(FakeCaster.instances[0].args[3], 2710.0)

    def test_failed_cast_shows_help_and_can_retry_on_another_tv(self):
        src = self.d / "plain.mkv"
        script = Script(src, 6.0, True, TV, "", "tv", TV2, "", "done", False)
        w, out = make_wizard(script, found=[found(src)], receivers=[TV, TV2])

        def flaky(self):
            if self.args[1] is TV:
                raise engine.CastError("TV said no")
        with mock.patch.object(FakeCaster, "start", flaky):
            self.assertEqual(w.run(), 0)
        self.assertIn("TV said no", out.getvalue())
        self.assertIn("same Wi-Fi", out.getvalue())
        self.assertEqual([c.args[1] for c in FakeCaster.instances], [TV, TV2])

    def test_enter_ip_manually(self):
        src = self.d / "plain.mkv"
        w, out = make_wizard(Script(src, 6.0, True, "ip", "192.168.8.99", "", "done", False), found=[found(src)], receivers=[])
        w.run()
        self.assertEqual(FakeCaster.instances[0].args[1].host, "192.168.8.99")

    def test_ctrl_c_exits_cleanly(self):
        w, out = make_wizard(Script("KeyboardInterrupt"), found=[])
        self.assertEqual(w.run(), 130)
        self.assertIn("Cancelled", out.getvalue())

    def test_missing_ffmpeg_is_explained(self):
        w, out = make_wizard(Script())
        with mock.patch.object(engine, "have_ffmpeg", return_value=False):
            self.assertEqual(w.run(), 1)
        self.assertIn("brew install ffmpeg", out.getvalue())


if __name__ == "__main__":
    unittest.main()
