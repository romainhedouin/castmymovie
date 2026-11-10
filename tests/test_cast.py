import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast  # noqa: E402

class Pure(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(cast.parse_time(""), 0.0)
        self.assertEqual(cast.parse_time("  "), 0.0)
        self.assertEqual(cast.parse_time("90"), 90.0)
        self.assertEqual(cast.parse_time("12:30"), 750.0)
        self.assertEqual(cast.parse_time("1:02:03"), 3723.0)
        for bad in ("abc", "1:2:3:4", "-5", "1:xx", "1.5", "1:²", "①"):   # str.isdigit() accepts the last two
            self.assertIsNone(cast.parse_time(bad), bad)

    def test_discovery_cancels_its_browser_before_closing(self):
        events = []

        class Zc:
            def close(self):
                events.append("close")

        class Browser:
            def __init__(self, zc, type_, listener):
                events.append("browse")

            def cancel(self):
                events.append("cancel")      # closing Zeroconf alone leaves the browser thread running

        fake = types.SimpleNamespace(Zeroconf=Zc, ServiceBrowser=Browser, ServiceListener=object)
        with mock.patch.dict(sys.modules, {"zeroconf": fake}):
            self.assertEqual(cast.discover(timeout=0), [])
        self.assertEqual(events, ["browse", "cancel", "close"])

    def test_receiver_from_service(self):
        r = cast.receiver_from_service({"fn": "Living Room TV", "md": "MiTV-MOEU0", "id": "abc", "ca": "463365"},
                                       ["fe80::1", "192.168.8.63"], 8009)
        self.assertEqual((r.name, r.host, r.model, r.uid), ("Living Room TV", "192.168.8.63", "MiTV-MOEU0", "abc"))
        self.assertEqual(r.label, "Living Room TV  (MiTV-MOEU0, 192.168.8.63)")

    def test_audio_only_devices_and_empty_records_are_skipped(self):
        self.assertIsNone(cast.receiver_from_service({"fn": "Speaker", "ca": "4"}, ["10.0.0.2"], 8009))
        self.assertIsNone(cast.receiver_from_service({"fn": "Ghost"}, [], 8009))

    def test_name_falls_back_to_model_then_host(self):
        self.assertEqual(cast.receiver_from_service({"md": "Chromecast"}, ["10.0.0.3"], 8009).name, "Chromecast")
        self.assertEqual(cast.receiver_from_service({}, ["10.0.0.3"], 8009).name, "10.0.0.3")


if __name__ == "__main__":
    unittest.main()
