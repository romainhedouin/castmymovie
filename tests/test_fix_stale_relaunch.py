"""Media session ids belong to one app instance: a relaunched player numbers them from 1 again."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie.castlink import MEDIA_NS  # noqa: E402
from fakereceiver import APP  # noqa: E402
from test_nativecast import Base  # noqa: E402


def lose_the_player(fake, **new_app):
    """The TV's player dies while we are away (and, with new_app, someone else's replaces it)."""
    with fake.lock:
        fake.drop()
        fake.app, fake.session = (dict(APP, **new_app) if new_app else None), None
        fake.next_id = 1


class RelaunchedPlayer(Base):
    def test_a_relaunched_player_that_numbers_sessions_from_1_is_picked_up(self):
        fake, s = self.started(start=200.0, reset_session_ids_on_launch=True)
        self.assertEqual(fake.session["id"], 1)
        time.sleep(0.4)
        lose_the_player(fake)
        self.wait(lambda: len(fake.loads) == 2, what="a relaunch and reload")
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing again")
        self.assertEqual(fake.session["id"], 1)                          # the new session reuses the old number
        self.wait(lambda: s.notice.startswith("Reloaded after a TV error"), what="the reload to be recognised")
        self.assertFalse(s.state().buffering)
        self.assertGreaterEqual(s.state().position, 200.0)
        s.toggle_pause()                                                 # the keys reach the new session
        self.wait(lambda: fake.session["state"] == "PAUSED", what="pause on the relaunched player")
        self.assertNotIn("DROPPED", self.log(s))

    def test_the_end_of_the_movie_is_seen_on_a_relaunched_player(self):
        fake, s = self.started(start=200.0, reset_session_ids_on_launch=True)
        time.sleep(0.4)
        lose_the_player(fake)
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING" and len(fake.loads) == 2, what="playing again")
        self.wait(lambda: s.notice.startswith("Reloaded after a TV error"), what="the reload to be recognised")
        fake.interrupt()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device started casting", s.notice)

    def test_a_player_replaced_by_another_instance_while_away_is_picked_up(self):
        fake, s = self.started(start=200.0, reset_session_ids_on_launch=True)
        time.sleep(0.4)
        lose_the_player(fake, transportId="web-9", sessionId="app-2")   # found, not launched by us
        self.wait(lambda: len(fake.loads) == 2, what="a reload into the new player")
        self.assertEqual(fake.kinds().count("LAUNCH"), 1)
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing again")
        self.assertEqual(fake.session["id"], 1)
        self.wait(lambda: s.notice.startswith("Reloaded after a TV error"), what="the reload to be recognised")
        s.toggle_pause()
        self.wait(lambda: fake.session["state"] == "PAUSED", what="pause on the new player")


class ReplacedSession(Base):
    """The original purpose of the stale set: the last words of the session our LOAD replaced."""

    def test_a_late_status_from_the_replaced_session_is_ignored(self):
        fake, s = self.started(start=200.0)
        fake.fail()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        self.wait(lambda: fake.session["state"] == "PLAYING" and s._media_id == 2, what="the reloaded session")
        late = {"mediaSessionId": 1, "playerState": "PLAYING", "currentTime": 5.0, "playbackRate": 1,
                "media": fake.session["media"]}
        fake._broadcast(MEDIA_NS, {"type": "MEDIA_STATUS", "requestId": 0, "status": [late]})
        time.sleep(0.2)                                                  # less than a poll: nothing sets it right again
        self.assertEqual(s._media_id, 2)
        self.assertNotIn("is now 1", self.log(s))

    def test_the_replaced_session_is_not_taken_for_the_answer_to_our_load(self):
        s = self.session(self.fake())                                    # not started: nothing else touches it
        late = {"mediaSessionId": 1, "playerState": "PLAYING", "currentTime": 5.0}
        s._stale, s._media_id, s._load_rid = {1}, None, 99
        self.assertIsNone(s._pick([late], rid=0))
        s._stale = set()
        self.assertIs(s._pick([late], rid=0), late)                      # without the set it would be adopted


if __name__ == "__main__":
    unittest.main()
