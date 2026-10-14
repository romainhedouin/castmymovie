import os
import signal
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cli  # noqa: E402


class Signals(unittest.TestCase):
    def test_kill_and_hangup_become_a_normal_exit_so_cleanup_runs(self):
        # By default SIGTERM ends Python on the spot: the `finally` blocks that stop VLC never run and the TV
        # keeps casting from an orphaned VLC.
        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        cli.install_signal_handlers()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.assertRaises(SystemExit) as cm:
                os.kill(os.getpid(), sig)
                time.sleep(1)  # the handler runs between two bytecodes of this thread
            self.assertEqual(cm.exception.code, 128 + sig)


if __name__ == "__main__":
    unittest.main()
