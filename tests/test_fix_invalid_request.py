"""A TV that refuses our status polls or LOAD in unexpected ways must not make the native sender spin or wait in vain."""

import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine, nativecast  # noqa: E402
from castmymovie.castlink import MEDIA_NS  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402
from test_nativecast import Base  # noqa: E402


class OddReceiver(FakeReceiver):
    """The fake TV with a few odd habits, each opt-in:
    strict_ids      GET_STATUS naming a media session that is not the current one gets INVALID_REQUEST
    refuse_polls    every media GET_STATUS gets INVALID_REQUEST, with or without a session id
    load_reply      a function of the request id: what a LOAD (from the `load_reply_from`-th on) gets, and nothing else"""

    def __init__(self, media=None, strict_ids=False, refuse_polls=False, load_reply=None, load_reply_from=0, **kw):
        self.strict_ids, self.refuse_polls = strict_ids, refuse_polls
        self.load_reply, self.load_reply_from = load_reply, load_reply_from
        super().__init__(media, **kw)

    def polls(self, session_id=None):
        """How many media GET_STATUS the TV has been sent (only those naming this media session, if given)."""
        return sum(1 for ns, kind, msg in self.seen
                   if ns == MEDIA_NS and kind == "GET_STATUS" and (session_id is None or msg.get("mediaSessionId") == session_id))

    def _on(self, conn, src, dst, ns, msg):
        if ns == MEDIA_NS and self.app and msg.get("type") == "GET_STATUS" and self._refuses(msg):
            self._send(conn, self.app["transportId"], src, MEDIA_NS,
                       {"type": "INVALID_REQUEST", "reason": "INVALID_MEDIA_SESSION_ID", "requestId": msg.get("requestId", 0)})
            return
        super()._on(conn, src, dst, ns, msg)

    def _refuses(self, msg):
        if self.refuse_polls:
            return True
        current = self.session["id"] if self.session else None
        return self.strict_ids and "mediaSessionId" in msg and msg["mediaSessionId"] != current

    def _load(self, conn, src, msg, reply):
        if self.load_reply and len(self.loads) >= self.load_reply_from:
            self.loads.append(msg)
            self._send(conn, self.app["transportId"], src, MEDIA_NS, self.load_reply(msg.get("requestId", 0)))
            return
        super()._load(conn, src, msg, reply)


class Odd(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def odd(self, **kw):
        fake = OddReceiver(self.media, **kw)
        self.fakes.append(fake)
        return fake


class RefusedPolls(Odd):
    def test_a_vanished_session_answered_with_invalid_request_is_reloaded_not_polled_in_a_loop(self):
        fake = self.odd(strict_ids=True)
        s = self.session(fake, start=200.0)
        s.start(timeout=10)
        started = time.monotonic()
        fake.forget()                                   # the TV loses the session; polls naming it now get INVALID_REQUEST
        self.wait(lambda: len(fake.loads) == 2, what="a reload")
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing again")
        time.sleep(1.0)
        elapsed = time.monotonic() - started
        self.assertTrue(s.state().alive)
        self.assertGreaterEqual(fake.loads[1]["currentTime"], 200.0)
        self.assertLessEqual(fake.polls(1), 4, "the refused session id must not be asked about again and again")
        self.assertLessEqual(fake.polls(), elapsed / s.POLL_EVERY + 4, "polls must stay at the normal pace")

    def test_a_renumbered_session_is_followed_without_a_reload(self):
        fake = self.odd(strict_ids=True)
        s = self.session(fake)
        s.start(timeout=10)
        with fake.lock:
            fake.session["id"] = 99                     # the same movie under a new media session id
        self.wait(lambda: s._media_id == 99, what="the new session id")
        time.sleep(0.8)
        self.assertEqual(len(fake.loads), 1)
        self.assertTrue(s.state().alive)
        self.assertLessEqual(fake.polls(1), 4)

    def test_a_tv_refusing_every_poll_is_polled_at_the_normal_pace_and_ends_in_a_message(self):
        fake = self.odd(refuse_polls=True)
        s = self.session(fake)
        s.start(timeout=10)
        started = time.monotonic()
        self.wait(lambda: not s.state().alive, timeout=15, what="the session to give up")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(len(fake.loads), 2, "it must try reloading before giving up")
        self.assertIn("kept failing", s.notice)
        self.assertLessEqual(fake.polls(), elapsed / s.POLL_EVERY + 6, "polls must stay at the normal pace")


class LoadReplies(Odd):
    def test_load_failed_with_request_id_zero_fails_at_once(self):
        fake = self.odd(load_reply=lambda rid: {"type": "LOAD_FAILED", "requestId": 0, "detailedErrorCode": 104})
        began = time.monotonic()
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=20)
        self.assertLess(time.monotonic() - began, 5, "it must not wait for the generic timeout")
        self.assertIn("LOAD_FAILED 104", str(cm.exception))
        self.assertEqual(len(fake.loads), 1)

    def test_load_cancelled_at_start_says_another_device_took_over(self):
        fake = self.odd(load_reply=lambda rid: {"type": "LOAD_CANCELLED", "requestId": rid})
        began = time.monotonic()
        with self.assertRaises(engine.CastError) as cm:
            self.session(fake).start(timeout=20)
        self.assertLess(time.monotonic() - began, 5)
        msg = str(cm.exception)
        self.assertIn("Another device started casting to the TV", msg)
        self.assertNotIn("incoming network connections", msg, "this is not the firewall")
        self.assertEqual(len(fake.loads), 1, "no fight: the movie is not loaded again")
        self.assertNotIn("STOP", fake.kinds(), "never stop someone else's cast")

    def test_load_cancelled_on_a_reload_ends_the_cast_without_a_fight(self):
        fake = self.odd(load_reply=lambda rid: {"type": "LOAD_CANCELLED", "requestId": rid}, load_reply_from=1)
        s = self.session(fake, start=100.0)
        s.start(timeout=10)
        fake.fail()                                     # a playback error: the sender reloads, and that LOAD is cancelled
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device started casting to the TV. The movie stopped at 01:4", s.notice)
        s.stop()
        self.assertEqual(len(fake.loads), 2)
        self.assertNotIn("STOP", fake.kinds(), "never stop someone else's cast")


class WhichRequestIsMeant(unittest.TestCase):
    """The replies are matched to our LOAD by request id, except when the TV does not say which one it means."""

    def cast(self, outstanding=7, started=False):
        s = nativecast.NativeCast(nativecast.Receiver("TV", "127.0.0.1", port=1), Path("x.mp4"), tls=False)
        s._load_rid = outstanding
        if started:
            s._started.set()
        self.addCleanup(s.log.close)
        return s

    def test_a_load_failed_for_an_older_request_is_not_about_the_load_in_flight(self):
        s = self.cast(started=True)
        s._on_media_error({"type": "LOAD_FAILED", "requestId": 3})     # our LOAD #3 was replaced by #7
        self.assertIsNone(s._reload_reason)
        self.assertEqual(s._load_rid, 7)

    def test_a_load_failed_with_no_request_id_is_about_the_load_in_flight(self):
        s = self.cast(started=True)
        s._on_media_error({"type": "LOAD_FAILED", "requestId": 0})
        self.assertIn("refused the reload", s._reload_reason)
        self.assertIsNone(s._load_rid)

    def test_nothing_is_in_flight_nothing_is_failed(self):
        s = self.cast(outstanding=None, started=True)
        s._on_media_error({"type": "LOAD_FAILED", "requestId": 0})
        s._on_media_error({"type": "LOAD_CANCELLED", "requestId": 0})
        self.assertIsNone(s._reload_reason)

    def test_our_own_reload_cancelling_our_earlier_load_is_not_a_takeover(self):
        s = self.cast(started=True)
        s._on_media_error({"type": "LOAD_CANCELLED", "requestId": 3})
        self.assertIsNone(s._reload_reason)
        self.assertEqual(s._load_rid, 7)


if __name__ == "__main__":
    unittest.main()
