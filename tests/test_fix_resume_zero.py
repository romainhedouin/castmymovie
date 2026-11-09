"""A reload must start where the movie really was: not at the 0 a buffering TV may report, and not before a seek."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_nativecast  # noqa: E402
from castmymovie.castlink import MEDIA_NS  # noqa: E402


class SlowReloadCast(test_nativecast.QuickCast):
    RELOAD_GAP = 1.5          # long enough to press a key inside the gap


class ResumePosition(test_nativecast.Base):
    def sent(self, fake, kind="SEEK"):
        return kind in fake.kinds(MEDIA_NS)

    def test_a_tv_reporting_zero_while_buffering_does_not_send_a_reload_to_the_start(self):
        fake = self.fake(zero_while_buffering=True)
        s = self.session(fake, 400.0)
        s.start(timeout=10)
        time.sleep(0.5)
        fake.buffering()
        self.wait(lambda: "STATE PLAYING -> BUFFERING" in self.log(s), what="the sender to see the buffering")
        stalled_at = fake.session["pos"]
        time.sleep(1.2)                                                  # several polls answered with currentTime 0
        fake.fail()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        again = fake.loads[1]["currentTime"]
        self.assertGreater(again, 399, "a reload at 0 would rewind the whole movie")
        self.assertAlmostEqual(again, stalled_at, delta=0.6, msg="a stalled player does not move on while it stalls")

    def test_a_seek_followed_at_once_by_an_error_reloads_at_the_seek_target(self):
        fake, s = self.started(start=100.0)
        time.sleep(0.3)
        fake.mute = True                                                 # the SEEK is sent but not yet taken in
        s.seek_to(450)
        self.wait(lambda: self.sent(fake), what="the SEEK to reach the TV")
        fake.mute = False
        fake.fail()
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        self.assertAlmostEqual(fake.loads[1]["currentTime"], 450, delta=1)

    def test_a_seek_near_the_end_then_finished_is_the_end_not_a_broken_download(self):
        fake, s = self.started(start=100.0)
        time.sleep(0.3)
        fake.mute = True
        s.seek_to(10_000)                                                # clamped to 595 of 600 s
        self.wait(lambda: self.sent(fake), what="the SEEK to reach the TV")
        fake.mute = False
        fake.finish()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertEqual(s.notice, "The movie finished.")
        self.assertEqual(len(fake.loads), 1, "no reload from hundreds of seconds back")

    def test_a_seek_during_the_reload_gap_is_applied_to_the_reload(self):
        fake = self.fake()
        s = self.session(fake, 100.0, cls=SlowReloadCast)
        s.start(timeout=10)
        time.sleep(0.3)
        fake.fail()
        self.wait(lambda: "RELOAD wanted" in self.log(s), what="the reload to be wanted")
        s.seek_to(500)
        self.wait(lambda: len(fake.loads) == 2, what="the reload")
        self.assertAlmostEqual(fake.loads[1]["currentTime"], 500, delta=1)
        self.wait(lambda: fake.session["state"] == "PLAYING" and abs(fake.position() - 500) < 3, what="playing at 500")

    def test_a_seek_while_the_reload_is_loading_is_applied_once_it_plays(self):
        fake, s = self.started(start=100.0)
        fake.play = False                                                # the reloaded movie loads but does not start
        fake.fail()
        self.wait(lambda: len(fake.loads) == 2, what="the reload")
        self.wait(lambda: fake.session["id"] == 2 and "MEDIA session 2" in self.log(s), what="the new session")
        s.seek_to(500)
        self.wait(lambda: "SEEK" in self.log(s) and "waits" in self.log(s), what="the seek to be held")
        self.assertFalse(self.sent(fake), "there is nothing to seek in yet")
        self.assertAlmostEqual(s.state().position, 500, delta=1, msg="the screen shows where the user went")
        with fake.lock:                                                  # the TV starts playing at last
            fake._set("PLAYING")
            fake._broadcast_media()
        self.wait(lambda: self.sent(fake), what="the held SEEK")
        self.wait(lambda: abs(fake.position() - 500) < 3, what="the TV at 500")


if __name__ == "__main__":
    unittest.main()
