"""The "Enter an IP address…" prompt only takes addresses on the home network: the native cast serves the movie to it."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine, nativecast  # noqa: E402
from test_mediaserver import make_mp4  # noqa: E402
from test_ui import TV, Script, make_wizard  # noqa: E402

ACCEPTED = ["192.168.8.63", "192.168.0.1", "10.0.0.5", "10.255.255.254", "172.16.0.9", "172.31.255.1",
            "169.254.12.34", "127.0.0.1"]
REJECTED = [
    "8.8.8.8", "1.1.1.1", "142.250.74.110", "172.15.0.1", "172.32.0.1", "11.0.0.1", "192.169.0.1",  # internet
    "100.64.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1", "240.0.0.1", "192.0.2.1", "203.0.113.5",  # not a TV
    "tv.local", "google.com", "livingroom-tv", "localhost",                                          # names
    "", "   ", "abc", "192.168.1", "192.168.1.256", "192.168.1.1.1", "192.168.8.63:8009", "192.168.8.63/24",
    "010.0.0.1", "192.168.008.063", "3232235777", "0xC0.0xA8.0x08.0x3F", "192.168.8.63 8.8.8.8",
    "[192.168.8.63]", "::1", "fe80::1", "fd00::1", "192.168.8.63\n8.8.8.8", "192.168.８.63",
]


class LanAddress(unittest.TestCase):
    def test_private_link_local_and_loopback_addresses_are_accepted_as_they_are(self):
        for a in ACCEPTED:
            with self.subTest(a):
                self.assertEqual(cast.lan_address(a), a)

    def test_everything_else_is_refused(self):
        for a in REJECTED:
            with self.subTest(a):
                self.assertIsNone(cast.lan_address(a))

    def test_surrounding_spaces_are_ignored(self):
        self.assertEqual(cast.lan_address("  192.168.8.63 "), "192.168.8.63")


class TypedAddress(unittest.TestCase):
    """pick_receiver() with the TV missing from the list: choose "Enter an IP address…" and type one."""

    def pick(self, *answers):
        script = Script(*answers)
        wizard, out = make_wizard(script, receivers=[])
        return wizard.pick_receiver(), script, out.getvalue()

    def test_a_home_address_becomes_the_receiver_on_the_default_port(self):
        receiver, script, _ = self.pick("ip", " 192.168.8.63 ")
        self.assertEqual((receiver.host, receiver.name, receiver.port), ("192.168.8.63", "192.168.8.63", 8009))
        self.assertEqual(script.asked, ["What now?", "IP address of the TV:"])

    def test_a_public_address_is_refused_by_name_and_the_question_is_asked_again(self):
        receiver, script, out = self.pick("ip", "8.8.8.8", "192.168.8.63")
        self.assertEqual(receiver.host, "192.168.8.63")
        self.assertEqual(script.asked, ["What now?", "IP address of the TV:", "IP address of the TV:"])
        self.assertIn("8.8.8.8 is not an address on your home network", out)

    def test_a_hostname_is_refused_and_the_question_is_asked_again(self):
        receiver, script, out = self.pick("ip", "tv.example.com", "10.1.2.3")
        self.assertEqual(receiver.host, "10.1.2.3")
        self.assertIn("tv.example.com is not an address on your home network", out)

    def test_garbage_is_refused_again_and_again_without_searching_for_TVs_each_time(self):
        calls = []
        script = Script("ip", "!!", "999.1.1.1", "192.168.1", "172.16.0.2")
        wizard, out = make_wizard(script, receivers=lambda: calls.append(1) or [])
        self.assertEqual(wizard.pick_receiver().host, "172.16.0.2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out.getvalue().count("is not an address on your home network"), 3)

    def test_markup_in_what_was_typed_is_shown_as_typed(self):
        receiver, _, out = self.pick("ip", "[bold]x[/bold]", "192.168.8.63")
        self.assertEqual(receiver.host, "192.168.8.63")
        self.assertIn("[bold]x[/bold] is not an address", out)

    def test_an_empty_answer_goes_back_to_the_list(self):
        receiver, script, _ = self.pick("ip", "8.8.8.8", "", None)
        self.assertIsNone(receiver)
        self.assertEqual(script.asked, ["What now?", "IP address of the TV:", "IP address of the TV:", "What now?"])

    def test_a_found_TV_is_still_offered_and_picked_as_before(self):
        script = Script(TV)
        wizard, _ = make_wizard(script, receivers=[TV])
        self.assertIs(wizard.pick_receiver(), TV)


class RefusalNamesTheAddress(unittest.TestCase):
    """A Receiver that did not come from the prompt (an odd mDNS answer) must not get the movie either."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.media = make_mp4(self.tmp / "m.cast.mp4", payload_size=1000)
        patcher = mock.patch.object(nativecast.tempfile, "gettempdir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, host, resolves_to=None):
        session = nativecast.NativeCast(cast.Receiver("TV", host), self.media, tls=False, duration=60.0)
        self.addCleanup(session.stop)
        with mock.patch.object(nativecast.socket, "gethostbyname", return_value=resolves_to or host), \
                mock.patch.object(nativecast, "MediaServer") as server, \
                mock.patch.object(nativecast.threading, "Thread") as thread:
            with self.assertRaises(engine.CastError) as ctx:
                session.start(timeout=1)
        server.assert_not_called()   # nothing was served, and no connection to the address was tried
        thread.assert_not_called()
        return str(ctx.exception)

    def test_a_public_address_is_refused_by_start_and_named(self):
        message = self.refused("8.8.8.8")
        self.assertIn("8.8.8.8", message)
        self.assertIn("not an address on your home network", message)

    def test_a_name_that_resolves_to_a_public_address_names_both(self):
        message = self.refused("tv.example.com", resolves_to="93.184.216.34")
        self.assertIn("tv.example.com", message)
        self.assertIn("93.184.216.34", message)


if __name__ == "__main__":
    unittest.main()
