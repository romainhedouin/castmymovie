"""Ctrl-C or a kill while a cast is starting must still stop the session: no worker, server or player left behind."""

import shutil
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast  # noqa: E402
import test_nativecast  # noqa: E402
import test_ui  # noqa: E402

CANCELS = (("Ctrl-C", KeyboardInterrupt), ("a kill", lambda: SystemExit(143)))


class NativeStart(test_nativecast.Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cancel_once_the_tv_has_the_movie(self, fake, session, exc):
        """start() is now only waiting for the TV to play, as it does for a slow TV: the user gives up."""
        wait = session._event.wait

        def waiting(timeout=None):
            if fake.fetches:
                raise exc()
            return wait(timeout)
        session._event.wait = waiting

    def test_cancelling_the_start_stops_the_session(self):
        for what, exc in CANCELS:
            with self.subTest(what):
                fake = self.fake(play=False)
                session = self.session(fake, 0.0)
                self.cancel_once_the_tv_has_the_movie(fake, session, exc)
                wizard, _ = test_ui.make_wizard(test_ui.Script())
                with self.assertRaises(type(exc())):
                    wizard.cast_to(session, cast.Receiver("Fake TV", "127.0.0.1", port=fake.port), self.media)
                self.assertTrue(fake.fetches, "the cast had really begun")
                self.assertFalse(session._thread.is_alive())
                self.assertFalse(session.state().alive)
                with self.assertRaises(OSError):                       # the HTTP server is closed
                    socket.create_connection(("127.0.0.1", session.server.port), timeout=2).close()
                self.assertTrue(fake.wait_for(lambda: fake.app is None), "the TV's player was left open")
                self.assertIn("STOPPED", self.log(session))


class FakeStart(unittest.TestCase):
    def test_a_cancelled_start_is_stopped_once_and_the_cancel_is_not_hidden(self):
        for what, exc in CANCELS:
            with self.subTest(what):
                wizard, _ = test_ui.make_wizard(test_ui.Script())
                session = test_ui.FakeNative(test_ui.TV, Path("/tmp/movie.mp4"))
                session.start = mock.Mock(side_effect=exc())
                with self.assertRaises(type(exc())):
                    wizard.cast_to(session, test_ui.TV, Path("/tmp/movie.mp4"))
                self.assertEqual(session.calls, ["stop"])

    def test_a_failed_start_is_still_reported_and_stopped_once(self):
        wizard, out = test_ui.make_wizard(test_ui.Script())
        session = test_ui.FakeNative(test_ui.TV, Path("/tmp/movie.mp4"))
        test_ui.FakeNative.fail = True
        self.assertFalse(wizard.cast_to(session, test_ui.TV, Path("/tmp/movie.mp4")))
        self.assertEqual(session.calls, ["stop"])
        self.assertIn("never downloaded the movie", out.getvalue())

    def test_a_cancelled_start_does_not_start_the_telemetry(self):
        made = []
        wizard, _ = test_ui.make_wizard(test_ui.Script(), tele=lambda curve: made.append(curve))
        session = test_ui.FakeNative(test_ui.TV, Path("/tmp/movie.mp4"))
        session.start = mock.Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            wizard.cast_to(session, test_ui.TV, Path("/tmp/movie.mp4"))
        self.assertEqual(made, [])

    def test_a_cancel_right_after_the_start_is_stopped_too(self):
        def tele(curve):
            raise KeyboardInterrupt   # the telemetry is set up between start() and the player loop

        wizard, _ = test_ui.make_wizard(test_ui.Script(), tele=tele)
        session = test_ui.FakeNative(test_ui.TV, Path("/tmp/movie.mp4"))
        with self.assertRaises(KeyboardInterrupt):
            wizard.cast_to(session, test_ui.TV, Path("/tmp/movie.mp4"))
        self.assertEqual(session.calls, ["stop"])


if __name__ == "__main__":
    unittest.main()
