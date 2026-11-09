import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine  # noqa: E402

# A stand-in for VLC that speaks the same rc protocol we captured from the real one:
# banner + "> " prompts, get_time / get_length / is_playing / pause / seek / quit,
# and it keeps running after `quit` (real VLC's chromecast teardown does not exit either).
FAKE_VLC = r'''#!%(py)s
import os, socket, sys, threading, time
args = sys.argv[1:]
if os.environ.get("FAKE_EXIT"):
    sys.exit(3)
rc = int(next(a for a in args if a.startswith("--rc-host=")).rsplit(":", 1)[1])
start = int(next((a.split("=")[1] for a in args if a.startswith("--start-time=")), 0))
open(os.environ["FAKE_ARGS_FILE"], "w").write(" ".join(args))
st = {"t": float(start), "paused": False, "len": 600, "playing": False}
end = float(os.environ.get("FAKE_END_AT", 0))   # the movie ends here: VLC stays up but has no input left
def ended(): return end and st["t"] >= end
def tick():
    time.sleep(0.6)
    st["playing"] = not os.environ.get("FAKE_NEVER_PLAY")
    while True:
        time.sleep(0.1)
        if st["playing"] and not st["paused"]:
            st["t"] += 1.0   # 10x speed so tests are quick
threading.Thread(target=tick, daemon=True).start()
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", rc)); srv.listen(1)
c, _ = srv.accept()
c.sendall(b"VLC media player 3.0.24 Vetinari\nCommand Line Interface initialized. Type `help' for help.\r\n> ")
buf = b""
while True:
    d = c.recv(1024)
    if not d: break
    buf += d
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        cmd = line.decode().strip()
        slow = os.environ["FAKE_ARGS_FILE"] + ".slow"
        if cmd == "get_time" and os.path.exists(slow):
            os.remove(slow); time.sleep(2.2)   # a busy VLC answers late, once
        if cmd == "get_time": c.sendall(b"\r\n> " if ended() else ("%%d\r\n> " %% st["t"]).encode())
        elif cmd == "get_length": c.sendall(("%%d\r\n> " %% st["len"]).encode())
        elif cmd == "is_playing": c.sendall(b"1\r\n> " if st["playing"] and not ended() else b"0\r\n> ")
        elif cmd == "pause": st["paused"] = not st["paused"]; c.sendall(b"> ")
        elif cmd.startswith("seek "): st["t"] = float(cmd.split()[1]); c.sendall(b"> ")
        elif cmd == "quit": c.sendall(b"Bye-bye!\r\n"); open(os.environ["FAKE_ARGS_FILE"] + ".quit", "w").close()
        else: c.sendall(b"Unknown command `%%s'.\r\n> " %% cmd.encode())
time.sleep(60)
'''


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

    def test_vlc_command_line(self):
        r = cast.Receiver("TV", "192.168.8.63")
        cmd = cast.build_vlc_cmd("/v/VLC", r, Path("/m/a b.mp4"), 4212, 8011, start=90)
        for flag in ("--sout", "#chromecast", "--sout-chromecast-ip=192.168.8.63", "--sout-chromecast-http-port=8011",
                     "--rc-host=127.0.0.1:4212", "--demux-filter=demux_chromecast", "--start-time=90",
                     "--no-sub-autodetect-file"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[-1], "/m/a b.mp4")
        self.assertNotIn("--start-time=0", cast.build_vlc_cmd("/v/VLC", r, Path("/m/a.mp4"), 1, 2, start=0))

    def test_find_vlc_returns_something_or_none(self):
        self.assertIn(type(cast.find_vlc()), (str, type(None)))


class Session(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = self.tmp / "fakevlc"
        self.fake.write_text(FAKE_VLC % {"py": sys.executable})
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.args_file = self.tmp / "args"
        patcher = mock.patch.dict(os.environ, {"FAKE_ARGS_FILE": str(self.args_file)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = cast.Receiver("TV", "192.168.8.63")
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.stop()

    def make(self, start=0.0):
        s = cast.VlcCast(str(self.fake), self.rec, Path("/tmp/movie.mp4"), start)
        self.sessions.append(s)
        return s

    def test_start_waits_for_playback_and_reports_state(self):
        s = self.make(start=100)
        s.start(timeout=15)
        st = s.state()
        self.assertTrue(st.alive and not st.paused)
        self.assertEqual(st.length, 600)
        self.assertGreaterEqual(st.position, 100)
        self.assertIn("--start-time=100", self.args_file.read_text())
        self.assertIn("--sout-chromecast-ip=192.168.8.63", self.args_file.read_text())

    def test_pause_resume_seek(self):
        s = self.make()
        s.start(timeout=15)
        s.toggle_pause()
        a = s.state()
        time.sleep(0.5)
        b = s.state()
        self.assertTrue(a.paused)
        self.assertEqual(a.position, b.position, "time must freeze while paused")
        s.toggle_pause()
        time.sleep(0.5)
        self.assertGreater(s.state().position, b.position)
        s.seek_to(300)
        self.assertGreaterEqual(s.state().position, 300)
        s.seek_to(10_000)  # clamped to just before the end
        self.assertLessEqual(s.state().position, 600)
        s.seek_to(-50)
        self.assertLess(s.state().position, 50)
        s.seek_to(200)
        s.state()
        s.seek_by(30)
        self.assertGreaterEqual(s.state().position, 230)

    def test_a_late_reply_is_not_taken_for_the_next_answer(self):
        s = self.make()
        s.start(timeout=15)
        Path(str(self.args_file) + ".slow").touch()
        self.assertIsNone(s._number("get_time"))           # VLC is too slow: no answer inside the read timeout
        self.assertEqual(s._number("get_length"), 600)     # and the late time must not pass for the length
        self.assertGreaterEqual(s._number("get_time"), 0)

    def test_stalled_playback_is_reported_as_buffering(self):
        s = self.make()
        s.start(timeout=15)
        s.STALL_SECONDS = 0.4
        s._cmd("pause")  # freeze the fake behind our back, like a stalled TV
        s.state()
        time.sleep(0.8)
        self.assertTrue(s.state().buffering)

    def test_a_finished_movie_is_reported_as_ended_not_buffering_forever(self):
        with mock.patch.dict(os.environ, {"FAKE_END_AT": "60"}):
            s = self.make(start=50)
            s.start(timeout=15)
        s.STALL_SECONDS = 0.4
        deadline, st = time.monotonic() + 10, s.state()
        while st.alive and time.monotonic() < deadline:
            time.sleep(0.3)
            st = s.state()
        self.assertTrue(s.proc.poll() is None, "VLC itself is still running")
        self.assertFalse(st.alive, "nothing is left to play, so the live view has to end")
        self.assertFalse(st.buffering)

    def test_stop_ends_the_process_even_though_vlc_ignores_quit(self):
        s = self.make()
        s.start(timeout=15)
        t0 = time.monotonic()
        s.stop()
        self.assertLess(time.monotonic() - t0, 6)
        self.assertIsNotNone(s.proc.poll())
        self.assertTrue(Path(str(self.args_file) + ".quit").exists(), "quit should be sent first")
        self.assertFalse(s.state().alive)

    def test_vlc_dying_at_startup_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"FAKE_EXIT": "1"}):
            with self.assertRaises(engine.CastError) as cm:
                self.make().start(timeout=10)
        self.assertIn("VLC", str(cm.exception))

    def test_tv_that_never_plays_times_out(self):
        with mock.patch.dict(os.environ, {"FAKE_NEVER_PLAY": "1"}):
            with self.assertRaises(engine.CastError) as cm:
                self.make().start(timeout=3)
        self.assertIn("did not start playing", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
