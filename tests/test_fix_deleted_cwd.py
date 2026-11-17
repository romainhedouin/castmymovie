"""Started from a terminal whose folder was deleted: Path.cwd() raises, and the wizard used to die with a traceback
before showing the movie list. It must search the usual folders instead, and say plainly why a relative path fails."""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cli, finder, ui  # noqa: E402
from test_finder import make  # noqa: E402
from test_ui import Script  # noqa: E402


class DeletedFolder(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        t = Path(tempfile.mkdtemp(prefix="castmymovie-cwd-")).resolve()
        self.addCleanup(shutil.rmtree, t, True)
        self.home = t / "home"
        self.movie = self.home / "Downloads" / "a.mkv"
        make(self.movie, 60)
        home = mock.patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.here = t / "here"
        self.here.mkdir()
        os.chdir(self.here)

    def delete_cwd(self):
        os.rmdir(self.here)
        with self.assertRaises(OSError):
            Path.cwd()

    def test_the_current_folder_is_searched_first_while_it_exists(self):
        self.assertEqual(finder.default_roots()[0].resolve(), self.here)

    def test_a_deleted_current_folder_is_skipped(self):
        self.delete_cwd()
        self.assertEqual(finder.default_roots(), [self.home / "Downloads"])
        self.assertEqual([f.path for f in finder.find_videos()], [self.movie])

    def test_the_wizard_still_lists_the_movies(self):
        self.delete_cwd()
        console = ui.Console(file=io.StringIO(), theme=ui.THEME, width=130, force_terminal=False)
        script = Script(self.movie)
        w = ui.Wizard(console=console, prompt=script, keys=None)  # the real finder.find_videos
        self.assertEqual(w.pick_file(), self.movie)
        self.assertEqual(len(script.choices["Which movie?"]), 3)  # the movie, "Type or drop a path…", "Quit"

    def run_cli(self, *argv):
        """cli.main in a fake terminal, with a wizard that only records the movie it was given."""
        runs = []
        with mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch.object(cli, "install_signal_handlers"), \
                mock.patch.object(ui, "Wizard", return_value=mock.Mock(run=lambda src: runs.append(src) or 0)):
            return cli.main(list(argv)), runs

    def test_a_relative_movie_argument_is_a_clear_error(self):
        self.delete_cwd()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.run_cli("Movie.mkv")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("the current folder no longer exists", err.getvalue())

    def test_a_full_movie_path_still_reaches_the_wizard(self):
        self.delete_cwd()
        self.assertEqual(self.run_cli(str(self.movie)), (0, [self.movie]))


if __name__ == "__main__":
    unittest.main()
