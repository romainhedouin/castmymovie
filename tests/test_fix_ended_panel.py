"""A cast that ends by itself: what its last panel says, and what the wizard offers next."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast  # noqa: E402
from test_nativecast import Base  # noqa: E402
from test_ui import TV, FakeNative, Script, make_wizard  # noqa: E402

KEY_HELP = ("space pause", "q stop", "Ctrl-C")


class EndedNative(FakeNative):
    """A native session that is over by the time the player looks, as NativeCast reports it: not alive, and the TV's
    player idle, which the session counts as buffering."""
    failed, notice_text = False, "The movie finished."

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.notice = self.notice_text

    def state(self):
        return cast.PlayState(self.pos, 600.0, False, True, False)


def ended(failed, notice):
    return type("Ended", (EndedNative,), {"failed": failed, "notice_text": notice})


def last_panel(case, session, interactive=True):
    w, out = make_wizard(Script())
    if not interactive:
        w.keys = None
    w.live_controls(session, TV, Path("/m/Night.cast.mp4"))     # already over: draws once and returns
    text = out.getvalue()
    case.assertIn("Casting to Living Room TV", text)
    return text


class EndedPanel(Base):
    """The last panel of a real native session that ended, against the fake TV."""

    def assertEnded(self, s, why):
        for interactive in (True, False):
            text = last_panel(self, s, interactive)
            self.assertIn(why, text)
            self.assertIn("ended", text)
            self.assertNotIn("buffering", text)
            self.assertNotIn("playing", text)
            for help_ in KEY_HELP:
                self.assertNotIn(help_, text)

    def test_the_movie_finished(self):
        fake, s = self.started(start=580.0)
        fake.finish()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertTrue(s.state().buffering, "the TV is idle then: this is what the panel must not show")
        self.assertEnded(s, "The movie finished.")
        self.assertFalse(s.failed)

    def test_another_device_took_over(self):
        fake, s = self.started()
        fake.interrupt()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertEnded(s, "Another device started casting")
        self.assertFalse(s.failed)

    def test_it_gave_up_after_too_many_reloads(self):
        fake = self.fake()
        s = self.session(fake, clock=lambda: 0.0)
        s.start(timeout=10)
        for n in range(2, 5):
            self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
            fake.fail()
            self.wait(lambda: len(fake.loads) == n, what=f"reload {n - 1}")
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        fake.fail()
        self.wait(lambda: not s.state().alive, what="giving up")
        self.assertEnded(s, "gave up after 3 reloads")
        self.assertTrue(s.failed)

    def test_it_gave_up_on_a_tv_that_went_away(self):
        fake, s = self.started()
        fake.close()
        self.wait(lambda: not s.state().alive, timeout=10, what="giving up")
        self.assertEnded(s, "Lost the TV")
        self.assertTrue(s.failed)

    def test_the_player_closed_on_the_tv_is_not_a_failure(self):
        fake, s = self.started()
        fake.close_app()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertFalse(s.failed)

    def test_a_cast_that_is_still_going_keeps_its_controls(self):
        fake, s = self.started()
        self.assertFalse(s.failed)
        w, out = make_wizard(Script(), keys=("q",))
        w.live_controls(s, TV, self.media)
        text = out.getvalue()
        self.assertIn("space pause", text)
        self.assertIn("q stop", text)
        self.assertNotIn("ended", text)


class Panel(unittest.TestCase):
    def test_a_playing_or_paused_panel_is_unchanged(self):
        w, _ = make_wizard(Script())

        def render(**kw):
            st = cast.PlayState(**{"position": 30.0, "length": 600.0, "paused": False, "buffering": False,
                                   "alive": True, **kw})
            w.console.print(w.render_cast(st, TV, Path("/m/Night.mp4"), True))
            return w.console.file.getvalue()
        self.assertIn("playing", render())
        self.assertIn("space", render())
        self.assertIn("paused", render(paused=True))
        self.assertIn("buffering…", render(buffering=True))

    def test_an_ended_panel_keeps_the_time_it_stopped_at_whatever_the_backend_says_about_buffering(self):
        for buffering in (True, False):           # NativeCast counts an idle TV as buffering, another player might not
            w, out = make_wizard(Script())
            st = cast.PlayState(1234.0, 6000.0, False, buffering, False)
            w.console.print(w.render_cast(st, TV, Path("/m/Night.mp4"), True, notice="The movie finished."))
            text = out.getvalue()
            self.assertIn("20:34", text)
            self.assertIn("1:40:00", text)
            self.assertIn("ended", text)
            self.assertNotIn("buffering", text)
            self.assertNotIn("space pause", text)


class WhatNext(unittest.TestCase):
    """After a cast that started, then ended by itself. The menu is the one a start failure gets."""

    def run_wizard(self, session_cls, *answers):
        outcome = SimpleNamespace(video=Path("/m/Night.cast.mp4"), subtitles=[], duration=600.0, curve=None)
        script = Script(*answers)
        w, out = make_wizard(script)
        w.caster = lambda *a, **k: session_cls(*a, **k)
        w.offer_cast(outcome)
        return w, script, out.getvalue()

    def test_a_cast_that_gave_up_offers_to_cast_again(self):
        gave_up = ended(True, "The TV kept failing, so I gave up after 3 reloads in 10 minutes.")
        _, script, text = self.run_wizard(gave_up, True, TV, "", "done")
        self.assertEqual(script.offered["What next?"][0], ["Cast it again", "Choose another TV", "Done with this movie"])
        self.assertEqual(script.defaults["What next?"][0], "again")
        self.assertIn("gave up after 3 reloads", text)
        self.assertNotIn("buffering", text)

    def test_a_finished_movie_is_not_treated_as_an_error(self):
        _, script, text = self.run_wizard(ended(False, "The movie finished."), True, TV, "", "done")
        self.assertEqual(script.offered["What next?"][0], ["Cast it again", "Choose another TV", "Done with this movie"])
        self.assertEqual(script.defaults["What next?"][0], "again")
        self.assertIn("The movie finished.", text)
        self.assertNotIn("buffering", text)

    def test_another_device_taking_over_is_not_a_failure(self):
        took_over = ended(False, "Another device started casting to the TV. The movie stopped at 03:10.")
        _, script, _ = self.run_wizard(took_over, True, TV, "", "done")
        self.assertEqual(script.defaults["What next?"], ["again"])


class Defaults(unittest.TestCase):
    """The scripted prompter remembers the default it was given, so a test can check the one the user sees."""

    def test_the_first_choices_have_the_defaults_the_wizard_promises(self):
        gave_up = ended(True, "The TV kept failing, so I gave up after 3 reloads in 10 minutes.")
        outcome = SimpleNamespace(video=Path("/m/Night.cast.mp4"), subtitles=[], duration=600.0, curve=None)
        script = Script(True, TV, "", "done")
        w, _ = make_wizard(script, receivers=[TV])
        w.caster = lambda *a, **k: gave_up(*a, **k)
        w.offer_cast(outcome)
        self.assertEqual(script.defaults["Which TV?"], [TV])
        self.assertEqual(script.defaults["What next?"], ["again"])

    def test_a_failed_start_defaults_to_again_every_time(self):
        outcome = SimpleNamespace(video=Path("/m/Night.cast.mp4"), subtitles=[], duration=600.0, curve=None)
        script = Script(True, TV, "", "again", "", "done")
        w, _ = make_wizard(script)
        FakeNative.fail = True
        w.offer_cast(outcome)
        self.assertEqual(script.defaults["What next?"][0], "again")
        self.assertEqual(script.defaults["What next?"][1], "again")


if __name__ == "__main__":
    unittest.main()
