"""The Mac's own network failing at (re)load time (its Wi-Fi mid-rejoin) is a lost link, never an internal error."""

import errno
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine, nativecast  # noqa: E402
from test_nativecast import Base  # noqa: E402


def unreachable(session, times=None):
    """Make the session's local address lookup fail with ENETUNREACH `times` times (forever if None), then work."""
    real, left = session._local_ip, [times]

    def local_ip():
        if left[0] is None or left[0] > 0:
            if left[0] is not None:
                left[0] -= 1
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        return real()

    session._local_ip = local_ip


class NetworkUnreachableAtLoad(Base):
    def assertNoInternalError(self, s):
        self.assertNotIn("internal error", s.notice)
        self.assertNotIn("BUG", self.log(s))

    def test_load_reports_an_unreachable_network_as_a_lost_link(self):
        s = self.session(self.fake())
        unreachable(s)
        with self.assertRaises(nativecast._LinkLost) as cm:
            s._load(5.0)
        self.assertIn("Network is unreachable", str(cm.exception))

    def test_a_reload_waits_out_the_network_coming_back(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                fake, s = self.started()
                self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
                unreachable(s, failures)
                fake.fail()
                self.wait(lambda: len(fake.loads) == 2, what="the reload once the network is back")
                self.wait(lambda: s.notice.startswith("Reloaded after a TV error"), what="the reloaded notice")
                self.assertTrue(s.state().alive)
                self.assertEqual(self.log(s).count("no route to the TV"), failures)
                self.assertNoInternalError(s)
                self.wait(lambda: fake.session["state"] == "PLAYING", what="playing again")

    def test_a_failed_attempt_does_not_use_up_the_reload_budget(self):
        fake, s = self.started()
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        unreachable(s, 2)
        for n in range(2, 5):                       # a blip, then two real TV errors: three reloads in all
            fake.fail()
            self.wait(lambda: len(fake.loads) == n, what=f"reload {n - 1}")
            self.wait(lambda: fake.session["state"] == "PLAYING", what="playing again")
        self.assertTrue(s.state().alive)
        self.assertNoInternalError(s)

    def test_the_first_load_waits_out_the_network_too(self):
        fake = self.fake()
        s = self.session(fake)
        unreachable(s, 2)
        s.start(timeout=10)
        self.assertEqual(len(fake.loads), 1)
        self.assertEqual(fake.loads[0]["currentTime"], 100.0)
        self.assertTrue(s.state().alive)
        self.assertNotIn("RELOAD", self.log(s), "the first LOAD is retried as such, not as a TV error")
        self.assertNoInternalError(s)

    def test_start_fails_cleanly_when_the_network_never_comes_back(self):
        s = self.session(self.fake())
        unreachable(s)
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=2)
        self.assertNotIn("internal error", str(cm.exception))
        self.assertNoInternalError(s)

    def test_a_network_that_never_comes_back_ends_the_cast_as_a_lost_tv(self):
        fake, s = self.started()
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        unreachable(s)
        fake.fail()
        self.wait(lambda: "no route to the TV" in self.log(s), what="the failed reload")
        fake.close()                                # the Wi-Fi is down: the TV is out of reach as well
        self.wait(lambda: not s.state().alive, timeout=10, what="giving up")
        self.assertIn("Lost the TV", s.notice)
        self.assertNoInternalError(s)


if __name__ == "__main__":
    unittest.main()
