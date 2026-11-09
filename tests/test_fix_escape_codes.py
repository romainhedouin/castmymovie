"""Text from the network never reaches the terminal with control characters in it.

A device on the Wi-Fi chooses the name it advertises over mDNS, and the TV chooses the error text it reports.
Rich lets ESC through, so `\\x1b]0;...` (retitle the window) or `\\x1b[2J` (clear the screen) would arrive intact.
"""

import io
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine, nativecast, ui  # noqa: E402
from fakereceiver import FakeReceiver  # noqa: E402
from test_mediaserver import make_mp4  # noqa: E402

EVIL = "\x1b]0;pwned\x07\x1b[2J\x9b31m\x9d52;c;x\x9c\x00\x7f"
CONTROL = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f]")  # any control character but the newline


def controls_in(text):
    return sorted({hex(ord(c)) for c in CONTROL.findall(text)})


class QuickCast(nativecast.NativeCast):
    PING_EVERY, POLL_EVERY, DEAD_AFTER = 0.5, 0.3, 1.5
    CONNECT_TIMEOUT = REPLY_TIMEOUT = LAUNCH_TIMEOUT = 2.0
    BACKOFF = (0.1, 0.2, 0.4)
    RELOAD_GAP = 0.2


class EvilReceiver(FakeReceiver):
    """A TV that puts control characters in everything it says about a failure."""

    def __init__(self, *args, idle_error=False, **kw):
        super().__init__(*args, **kw)
        self.idle_error = idle_error   # the load ends as IDLE + ERROR (no LOAD_FAILED message)

    def _load(self, conn, src, msg, reply):
        if self.load_error:
            self.loads.append(msg)
            reply({"type": self.load_error, "detailedErrorCode": EVIL + "104", "reason": EVIL + "WHY"})
            return
        super()._load(conn, src, msg, reply)

    def _media_status(self):
        out = super()._media_status()
        for entry in out:
            if entry.get("idleReason"):
                entry.update(detailedErrorCode=EVIL + "104", reason=EVIL + "WHY",
                             extendedStatus={"playerState": "LOADING", "media": {"contentId": "x"}})
        return out

    def _start_playing(self, session, msg, rid):
        if not self.idle_error:
            return super()._start_playing(session, msg, rid)
        self._download(session["media"], session["pos"])
        with self.lock:
            self.session["state"], self.session["idle"] = "IDLE", "ERROR"
            self._broadcast_media()


class CleanText(unittest.TestCase):
    def test_c0_c1_del_and_esc_are_removed(self):
        self.assertEqual(cast.clean_text("\x1b]0;x\x07TV\x9b2J\x00\x7f\x85\x9d"), "]0;xTV2J")
        self.assertEqual(controls_in(cast.clean_text(EVIL)), [])

    def test_ordinary_text_is_kept(self):
        for text in ("Living Room TV", "Salon d'Éloïse (2)", "客厅电视", "Fernseher 📺", "👨‍👩‍👧 TV", "a  b"):
            self.assertEqual(cast.clean_text(text), text)

    def test_tabs_and_newlines_become_spaces(self):
        self.assertEqual(cast.clean_text("Living\nRoom\tTV\r\n"), "Living Room TV")

    def test_none_is_empty_and_other_values_are_text(self):
        self.assertEqual(cast.clean_text(None), "")
        self.assertEqual(cast.clean_text(104), "104")
        self.assertEqual(cast.clean_text({"a": "\x1b"}), "{'a': '\\x1b'}")   # the repr escapes it; never raw


class Receivers(unittest.TestCase):
    def test_name_model_and_id_from_mdns_are_clean(self):
        props = {"fn": "Living \x1b]0;pwned\x07Room\x1b[2J", "md": "Chromecast\x9b31m", "id": "ab\x1bcd", "ca": "5"}
        r = cast.receiver_from_service(props, ["192.168.8.63"], 8009)
        self.assertEqual(controls_in(f"{r.name}{r.model}{r.uid}{r.label}"), [])
        self.assertTrue(r.name.startswith("Living ") and "Room" in r.name)
        self.assertEqual((r.model, r.uid), ("Chromecast31m", "abcd"))

    def test_a_name_of_only_control_characters_falls_back_to_the_model(self):
        r = cast.receiver_from_service({"fn": "\x1b\x07\x9b", "md": "Chromecast"}, ["10.0.0.5"], 8009)
        self.assertEqual(r.name, "Chromecast")
        r = cast.receiver_from_service({"fn": "\x1b\x07"}, ["10.0.0.5"], 8009)
        self.assertEqual(r.name, "10.0.0.5")

    def test_a_clean_receiver_is_unchanged(self):
        props = {"fn": "Living Room TV", "md": "MiTV-MOEU0", "id": "0123456789abcdef", "ca": "4101"}
        r = cast.receiver_from_service(props, ["192.168.8.63"], 8009)
        self.assertEqual((r.name, r.model, r.uid, r.host), ("Living Room TV", "MiTV-MOEU0", "0123456789abcdef",
                                                          "192.168.8.63"))


class WhatTheTvSaysInAnError(unittest.TestCase):
    """The worker's decisions, without a connection."""

    def cast(self):
        return nativecast.NativeCast(cast.Receiver("TV", "127.0.0.1"), Path("movie.cast.mp4"))

    def ended(self, call, *args):
        with self.assertRaises(nativecast._Ended) as cm:
            call(*args)
        return cm.exception.message

    def test_an_error_before_the_movie_plays_shows_the_code_or_reason_only(self):
        s = self.cast()
        entry = {"detailedErrorCode": EVIL + "104", "extendedStatus": {"playerState": "LOADING", "media": {}}}
        msg = self.ended(s._on_idle, "ERROR", entry)
        self.assertEqual(controls_in(msg), [])
        self.assertIn("the TV reported ERROR ", msg)
        self.assertIn("104", msg)
        self.assertNotIn("playerState", msg)                            # extendedStatus is an object, not text
        msg = self.ended(s._on_idle, "ERROR", {"reason": EVIL + "WHY", "extendedStatus": {"playerState": "IDLE"}})
        self.assertIn("WHY", msg)
        self.assertNotIn("playerState", msg)
        msg = self.ended(s._on_idle, "ERROR", {"extendedStatus": {"playerState": "IDLE"}})
        self.assertIn("(the TV reported ERROR)", msg)

    def test_an_unknown_idle_reason_is_clean_before_and_after_the_movie_starts(self):
        s = self.cast()
        msg = self.ended(s._on_idle, "\x1b[2JBOGUS", {})
        self.assertEqual(controls_in(msg), [])
        self.assertIn("BOGUS", msg)
        s._started.set()
        s._on_idle("\x1b[2JBOGUS", {})
        self.assertEqual(controls_in(s._reload_reason), [])
        self.assertIn("BOGUS", s._reload_reason)

    def test_a_refused_load_shows_a_clean_code(self):
        s = self.cast()
        s._load_rid = 7
        msg = self.ended(s._on_media_error, {"type": "LOAD_FAILED", "requestId": 7, "detailedErrorCode": EVIL + "104"})
        self.assertEqual(controls_in(msg), [])
        self.assertIn("LOAD_FAILED ", msg)
        self.assertIn("104", msg)
        s._started.set()
        s._load_rid = 8
        s._on_media_error({"type": "LOAD_FAILED", "requestId": 8, "reason": EVIL + "WHY"})
        self.assertEqual(controls_in(s._reload_reason), [])
        self.assertIn("WHY", s._reload_reason)


class OnTheWire(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.media = make_mp4(self.tmp / "Night of the Fake TV.cast.mp4")
        patcher = mock.patch.object(nativecast.tempfile, "gettempdir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, **kw):
        fake = EvilReceiver(self.media, **kw)
        self.addCleanup(fake.close)
        s = QuickCast(cast.Receiver("Fake TV", "127.0.0.1", port=fake.port), self.media, 100.0, tls=False,
                      duration=600.0)
        self.addCleanup(s.stop)
        return fake, s

    def wait(self, predicate, timeout=10.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def panel_text(self, s, receiver):
        """What the live panel prints, as plain text (Rich's own styling escapes left out)."""
        console = ui.Console(file=io.StringIO(), record=True, force_terminal=True, width=130, theme=ui.THEME)
        wizard = ui.Wizard(console=console, prompt=None, keys=None)
        console.print(wizard.render_cast(s.state(), receiver, self.media, False, notice=s.notice))
        return console.export_text(styles=False)

    def test_a_launch_error_reason_is_clean(self):
        fake, s = self.start(launch_error=EVIL + "NOT_ALLOWED")
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertEqual(controls_in(str(cm.exception)), [])
        self.assertIn("NOT_ALLOWED", str(cm.exception))

    def test_a_refused_load_is_clean(self):
        fake, s = self.start(load_error="LOAD_FAILED")
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        self.assertEqual(controls_in(str(cm.exception)), [])
        self.assertIn("LOAD_FAILED ", str(cm.exception))

    def test_an_error_state_with_text_in_every_field_is_clean(self):
        fake, s = self.start(idle_error=True)
        with self.assertRaises(engine.CastError) as cm:
            s.start(timeout=5)
        msg = str(cm.exception)
        self.assertEqual(controls_in(msg), [])
        self.assertIn("the TV reported ERROR ", msg)
        self.assertNotIn("playerState", msg)

    def test_a_cast_the_tv_keeps_failing_ends_with_a_clean_notice_and_panel(self):
        fake, s = self.start()
        s.start(timeout=10)
        fake.load_error = "LOAD_FAILED"                                  # every reload is refused, with text in it
        self.wait(lambda: fake.session and fake.session["state"] == "PLAYING", what="playing")
        fake.fail()
        self.wait(lambda: not s.state().alive, what="giving up")
        self.assertIn("kept failing", s.notice)
        self.assertEqual(controls_in(s.notice), [])
        text = self.panel_text(s, cast.receiver_from_service({"fn": "Den \x1b]0;pwned\x07TV"}, ["127.0.0.1"], 8009))
        self.assertIn("kept failing", text)
        self.assertEqual(controls_in(text), [])

    def test_another_device_taking_over_ends_with_a_clean_notice(self):
        fake, s = self.start()
        s.start(timeout=10)
        fake.interrupt()
        self.wait(lambda: not s.state().alive, what="the session to end")
        self.assertIn("Another device", s.notice)
        self.assertEqual(controls_in(s.notice), [])


class ThePanel(unittest.TestCase):
    def test_a_receiver_name_never_reaches_the_screen_with_control_characters(self):
        r = cast.receiver_from_service({"fn": "Den \x1b]0;pwned\x07\x1b[2JTV"}, ["10.0.0.5"], 8009)
        console = ui.Console(file=io.StringIO(), record=True, force_terminal=True, width=100, theme=ui.THEME)
        wizard = ui.Wizard(console=console, prompt=None, keys=None)
        panel = wizard.render_cast(cast.PlayState(10.0, 100.0, False, False, True), r, Path("m.mp4"), False)
        console.print(panel)
        plain = console.export_text(styles=False)
        self.assertEqual(controls_in(plain), [])
        self.assertIn("Casting to Den ", plain)


if __name__ == "__main__":
    unittest.main()
