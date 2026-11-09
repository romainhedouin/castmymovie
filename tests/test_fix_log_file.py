"""The session logs live in a shared temp dir: they are private, never follow a planted link, never block a cast,
are capped, and keep the secret URL token out."""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, engine, nativecast  # noqa: E402
from test_nativecast import Base  # noqa: E402


class NativeLogIsSafe(Base):
    def test_a_planted_symlink_is_not_followed(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        (self.tmp / nativecast.LOG_NAME).symlink_to(victim)
        fake, s = self.started()
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual(len(fake.loads), 1)                       # the cast itself did not care

    def test_the_log_is_readable_by_its_owner_only(self):
        fake, s = self.started()
        self.assertEqual(stat.S_IMODE(s.log_path.stat().st_mode), 0o600)

    def test_an_older_world_readable_log_is_tightened(self):
        log = self.tmp / nativecast.LOG_NAME
        log.write_text("from an older version")
        log.chmod(0o644)
        fake, s = self.started()
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertNotIn("older version", self.log(s))

    def test_a_log_with_another_hard_link_is_left_alone(self):
        other = self.tmp / "other.txt"
        other.write_text("precious")
        os.link(other, self.tmp / nativecast.LOG_NAME)
        fake, s = self.started()
        self.assertEqual(other.read_text(), "precious")
        self.assertEqual(len(fake.loads), 1)

    def test_a_log_in_a_missing_folder_does_not_stop_the_cast(self):
        with mock.patch.object(nativecast.tempfile, "gettempdir", return_value=str(self.tmp / "no" / "such" / "dir")):
            fake, s = self.started()
        self.assertEqual(len(fake.loads), 1)
        self.assertTrue(s.state().alive)

    def test_a_log_path_that_is_a_folder_does_not_stop_the_cast(self):
        (self.tmp / nativecast.LOG_NAME).mkdir()
        fake, s = self.started()
        self.assertEqual(len(fake.loads), 1)
        self.assertTrue(s.state().alive)

    def test_a_failed_start_does_not_point_at_a_log_that_could_not_be_written(self):
        with mock.patch.object(nativecast.tempfile, "gettempdir", return_value=str(self.tmp / "missing")):
            s = self.session(None, port=1)
            with self.assertRaises(engine.CastError) as cm:
                s.start(timeout=5)
        self.assertNotIn("The details are in", str(cm.exception))
        self.assertIn("Could not reach Fake TV", str(cm.exception))

    def test_the_url_token_is_not_in_the_log(self):
        vtt = self.tmp / "Night.cast.eng.vtt"
        vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
        fake = self.fake()
        s = self.session(fake, subtitles=[vtt])
        s.start(timeout=10)
        token = s.server.token
        self.assertIn(token, fake.loads[0]["media"]["contentId"])      # the TV still got the real URL
        text = self.log(s)
        self.assertNotIn(token, text)
        load = next(line for line in text.splitlines() if " TX " in line and "LOAD" in line)
        self.assertIn(f"http://127.0.0.1:{s.server.port}/***.mp4", load)   # enough left to debug with
        self.assertIn("/***-1.vtt", load)
        self.assertIn("TX", text)
        self.assertIn("RX", text)

    def test_the_movie_path_and_tv_address_stay_in_the_log(self):
        fake, s = self.started()
        text = self.log(s)
        self.assertIn(str(self.media), text)
        self.assertIn("127.0.0.1", text)

    def test_keep_awake_failing_does_not_stop_the_cast(self):
        with mock.patch.object(cast.sys, "platform", "darwin"), \
                mock.patch.object(cast.shutil, "which", return_value="/usr/bin/caffeinate"), \
                mock.patch.object(cast.subprocess, "Popen", side_effect=OSError("Too many open files")):
            self.assertIsNone(cast.keep_awake(os.getpid()))
            fake, s = self.started()
        self.assertEqual(len(fake.loads), 1)
        self.assertIsNone(s.awake)


class SessionLogCap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_it_stops_growing_past_the_cap(self):
        log = nativecast.SessionLog(self.tmp / "x.log")
        self.addCleanup(log.close)
        with mock.patch.object(nativecast.SessionLog, "MAX_BYTES", 2000):
            log.open()
            for i in range(500):
                log.write(f"line {i} " + "x" * 50)
            size = log.path.stat().st_size
            for i in range(500):
                log.write("later")
        self.assertEqual(log.path.stat().st_size, size)
        self.assertLess(size, 2000 + 200)
        lines = log.path.read_text().splitlines()
        self.assertIn("line 0 ", lines[0])                              # the start, where the context is, is kept
        self.assertIn("log is full", lines[-1])
        self.assertEqual(sum("log is full" in line for line in lines), 1)

    def test_writing_before_open_or_after_close_is_harmless(self):
        log = nativecast.SessionLog(self.tmp / "x.log")
        log.write("nobody listens")
        log.open()
        log.close()
        log.write("nobody listens")
        self.assertEqual((self.tmp / "x.log").read_text(), "")

    def test_a_failing_disk_write_does_not_raise(self):
        log = nativecast.SessionLog(self.tmp / "x.log")
        self.addCleanup(log.close)
        log.open()
        with mock.patch.object(log._f, "write", side_effect=OSError("No space left on device")):
            log.write("one")
        log.write("two")                                                # the log gave up, quietly


class VlcLogIsSafe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, value in (("tempfile.gettempdir", str(self.tmp)), ("free_port", 1234), ("keep_awake", None)):
            patcher = mock.patch(f"castmymovie.cast.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_start(self, vlc_cast):
        """start() against a VLC that dies at once; returns the Popen call and the error."""
        proc = mock.Mock()
        proc.poll.return_value = 1
        with mock.patch.object(cast.subprocess, "Popen", return_value=proc) as popen:
            with self.assertRaises(engine.CastError) as cm:
                vlc_cast.start(timeout=2)
        return popen, str(cm.exception)

    def vlc_cast(self):
        return cast.VlcCast("/nonexistent/vlc", cast.Receiver("Fake TV", "127.0.0.1"), self.tmp / "m.mp4")

    def test_a_planted_symlink_is_not_followed(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("precious")
        (self.tmp / "castmymovie-vlc.log").symlink_to(victim)
        popen, message = self.run_start(self.vlc_cast())
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual(popen.call_args.kwargs["stdout"], cast.subprocess.DEVNULL)   # VLC still starts, without a log
        self.assertNotIn("(see", message)

    def test_the_log_is_readable_by_its_owner_only(self):
        c = self.vlc_cast()
        popen, message = self.run_start(c)
        self.assertEqual(stat.S_IMODE(c.log_path.stat().st_mode), 0o600)
        self.assertIn(f"(see {c.log_path})", message)


class OpenPrivateLog(unittest.TestCase):
    def test_a_file_owned_by_someone_else_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "theirs.log"
        path.write_text("theirs")
        with mock.patch.object(cast.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertIsNone(cast.open_private_log(path))
        self.assertEqual(path.read_text(), "theirs")


if __name__ == "__main__":
    unittest.main()
