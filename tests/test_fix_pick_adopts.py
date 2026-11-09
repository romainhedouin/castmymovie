"""Our first LOAD lands on a media player that is already running another sender's movie: the old session's
last words (under our LOAD's request id, without `media`) must not be taken for ours."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine  # noqa: E402
from castmymovie.castlink import MEDIA_NS  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402
from test_nativecast import Base  # noqa: E402


class BareErrorReceiver(FakeReceiver):
    """A TV that fails our LOAD with a bare IDLE + ERROR status (no `media`, our request id) and no LOAD_FAILED."""

    def _start_playing(self, session, msg, rid):
        with self.lock:
            self.session["state"], self.session["idle"] = "IDLE", "ERROR"
            status = self._media_status()
            status[0].pop("media")
            self._broadcast(MEDIA_NS, {"type": "MEDIA_STATUS", "requestId": rid, "status": status})


class OtherSenderWasPlaying(Base):
    def test_the_old_sessions_last_words_are_not_ours(self):
        fake = self.fake(other_sender=True)
        s = self.session(fake, start=100.0)
        s.start(timeout=10)
        self.assertEqual(len(fake.loads), 1)
        self.wait(lambda: fake.session["state"] == "PLAYING" and s.state().alive, what="our movie to play")
        self.assertEqual(fake.session["id"], 2)                  # the old session was 1
        self.assertNotIn("Another device", s.notice)
        log = self.log(s)
        self.assertIn("MEDIA session 2", log)
        self.assertNotIn("MEDIA session 1", log)
        self.assertNotIn("Another device", log)
        self.assertGreaterEqual(s.state().position, 100.0)

    def test_a_takeover_in_the_middle_of_our_cast_still_ends_it(self):
        fake = self.fake(other_sender=True)
        s = self.session(fake)
        s.start(timeout=10)
        fake.interrupt()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device", s.notice)
        s.stop()
        self.assertEqual(len(fake.loads), 1)
        self.assertNotIn("STOP", fake.kinds(), "never stop someone else's cast")

    def test_another_sender_loading_its_own_movie_in_the_middle_of_our_cast_ends_it(self):
        fake = self.fake(other_sender=True)
        s = self.session(fake)
        s.start(timeout=10)
        with fake.lock:                                            # their LOAD: a new session about their movie
            fake.session = {"id": 3, "state": "PLAYING", "idle": None, "pos": 0.0, "at": 0.0,
                            "media": {"contentId": "http://192.0.2.9:8009/theirs.mp4", "contentType": "video/mp4"}}
            fake._broadcast_media()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device", s.notice)
        s.stop()
        self.assertEqual(len(fake.loads), 1)
        self.assertNotIn("STOP", fake.kinds(), "never stop someone else's cast")


class OurLoadFailed(Base):
    def test_a_bare_error_status_is_still_reported_as_the_load_failing(self):
        fake = BareErrorReceiver(self.media)
        self.fakes.append(fake)
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=5)
        msg = str(cm.exception)
        self.assertIn("could not load the movie (the TV reported ERROR)", msg)
        self.assertNotIn("Another device", msg)


if __name__ == "__main__":
    unittest.main()
