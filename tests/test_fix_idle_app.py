"""A TV that lists its idle screen (Backdrop) while nothing is cast: that is not "another app on the TV"."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import nativecast  # noqa: E402
from castmymovie.castlink import RECV_NS  # noqa: E402
from fakereceiver import IDLE_APP  # noqa: E402
from test_nativecast import Base  # noqa: E402

FLAGGED = dict(IDLE_APP, appId="0F5096E8", displayName="Ambient")   # only isIdleScreen says it is the idle screen
BARE = {k: v for k, v in IDLE_APP.items() if k != "isIdleScreen"}   # only the Backdrop appId says so
YOUTUBE = {"appId": "233637DE", "displayName": "YouTube", "isIdleScreen": False, "transportId": "web-9",
           "sessionId": "yt-1", "namespaces": []}


class Cases:
    """The tests, run once per way a TV can show its idle screen (`idle`)."""

    def player_dies_with_the_link(self, fake, replacement=None):
        with fake.lock:
            fake.drop()
            fake.app, fake.session = replacement, None

    def test_a_player_gone_during_a_reconnect_is_relaunched_at_the_position(self):
        fake, s = self.started(start=200.0, idle_app=self.idle)
        time.sleep(0.4)
        self.player_dies_with_the_link(fake)
        self.wait(lambda: len(fake.loads) == 2, what="a relaunch and reload")
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 2)
        self.assertGreaterEqual(fake.loads[1]["currentTime"], 200.0)
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing again")
        self.assertTrue(s.state().alive)
        self.assertNotIn("Another app", s.notice)

    def test_a_real_app_on_the_tv_still_ends_the_cast_after_a_reconnect(self):
        fake, s = self.started(start=100.0, idle_app=self.idle)
        self.player_dies_with_the_link(fake, replacement=dict(YOUTUBE))
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another app is on the TV now", s.notice)
        self.assertEqual(len(fake.loads), 1)
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 1, "never push someone else's app off the TV")

    def test_a_real_app_taking_over_while_connected_ends_the_cast(self):
        fake, s = self.started(start=100.0, idle_app=self.idle)
        with fake.lock:
            fake.app, fake.session = dict(YOUTUBE), None
            fake._broadcast(RECV_NS, fake._receiver_status())
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another app took over the TV", s.notice)
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 1)

    def test_the_user_closing_the_player_is_told_so_not_that_another_app_came(self):
        fake, s = self.started(start=100.0, idle_app=self.idle)
        fake.close_app()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("The TV closed the player at 01:4", s.notice)
        self.assertNotIn("Another app", s.notice)
        self.assertEqual(fake.kinds(RECV_NS).count("LAUNCH"), 1)


class IdleScreenFlag(Cases, Base):
    idle = FLAGGED


class IdleScreenBackdropId(Cases, Base):
    idle = BARE


class RunningApps(unittest.TestCase):
    def test_only_the_idle_screen_is_ignored(self):
        apps = [dict(IDLE_APP), dict(FLAGGED), dict(BARE), dict(YOUTUBE), {"appId": "CC1AD845", "transportId": "t"}]
        left = nativecast._running_apps({"status": {"applications": apps}})
        self.assertEqual([a["appId"] for a in left], ["233637DE", "CC1AD845"])
        self.assertEqual(nativecast._running_apps({}), [])
        self.assertEqual(nativecast._running_apps({"status": {}}), [])


if __name__ == "__main__":
    unittest.main()
