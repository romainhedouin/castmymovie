"""The real menus, driven with real key presses.

test_ui.py scripts the prompts, so it never touches questionary. These tests do: questionary quietly replaces a
choice value of None by the choice's title, which is how "Quit" once came back as the string "Quit".
"""

import io
import os
import signal
import sys
import unittest
from pathlib import Path

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import ui  # noqa: E402

DOWN, ENTER = "\x1b[B", "\r"


def press(keys, run, seconds=15):
    """Type `keys` into the real prompt. A prompt that never ends (a broken one just hangs) fails after `seconds`."""
    def give_up(signum, frame):
        raise TimeoutError(f"the prompt never finished after typing {keys!r}")
    old = signal.signal(signal.SIGALRM, give_up)
    signal.alarm(seconds)
    try:
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return run()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class Menus(unittest.TestCase):
    def select(self, keys, choices, **kw):
        return press(keys, lambda: ui.Prompter().select("Which?", choices, **kw))

    def test_a_none_choice_comes_back_as_none(self):
        choices = [("a movie", Path("/m/a.mkv")), ("Type a path", "__path__"), ("Quit", None)]
        self.assertIsNone(self.select(DOWN * 2 + ENTER, choices))

    def test_values_come_back_untouched(self):
        movie = Path("/m/a.mkv")
        choices = [("a movie", movie), ("Type a path", "__path__"), ("Quit", None)]
        self.assertIs(self.select(ENTER, choices), movie)
        self.assertEqual(self.select(DOWN + ENTER, choices), "__path__")

    def test_falsy_values_are_not_confused_with_no_answer(self):
        choices = [("zero", 0), ("empty", ""), ("no", False), ("Skip", None)]
        self.assertEqual([self.select(DOWN * i + ENTER, choices) for i in range(4)], [0, "", False, None])

    def test_the_default_is_preselected_by_value(self):
        choices = [("one", 1.0), ("ten", 10.0), ("Skip", None)]
        self.assertEqual(self.select(ENTER, choices, default=10.0), 10.0)

    def test_typing_filters_a_menu_whose_titles_are_coloured(self):
        # the movie picker: its titles are (style, text) lists, and questionary's filter calls .lower() on a title
        choices = [([("class:text", "alpha.mkv"), ("class:hint", "   1.2 GB   ~/Movies")], Path("/m/alpha.mkv")),
                   ([("class:text", "bravo.mkv"), ("class:hint", "   0.8 GB   ~/Movies")], Path("/m/bravo.mkv")),
                   ("Quit", None)]
        self.assertEqual(self.select("br" + ENTER, choices, filterable=True), Path("/m/bravo.mkv"))
        self.assertEqual(self.select(ENTER, choices, filterable=True), Path("/m/alpha.mkv"))


class WizardWithRealMenus(unittest.TestCase):
    def test_choosing_quit_in_the_movie_picker_ends_cleanly(self):
        console = ui.Console(file=io.StringIO(), theme=ui.THEME, force_terminal=False)
        wizard = ui.Wizard(console=console, find_videos=lambda: [], keys=None)
        # with no movies found the menu is: "Type or drop a file path…", "Quit"
        self.assertEqual(press(DOWN + ENTER, wizard.run), 0)

    def test_ctrl_d_at_a_text_prompt_cancels_like_ctrl_c(self):
        out = io.StringIO()
        wizard = ui.Wizard(console=ui.Console(file=out, theme=ui.THEME, force_terminal=False),
                           find_videos=lambda: [], keys=None)
        # "Type or drop a file path…" is the first entry; Ctrl-D at the path prompt used to end in a traceback
        self.assertEqual(press(ENTER + "\x04", wizard.run), 130)
        self.assertIn("Cancelled", out.getvalue())

    def test_choosing_skip_when_picking_a_tv_returns_no_receiver(self):
        console = ui.Console(file=io.StringIO(), theme=ui.THEME, force_terminal=False)
        wizard = ui.Wizard(console=console, discover=lambda: [], keys=None)
        # no TV found: "Search again", "Enter an IP address…", "Skip"
        self.assertIsNone(press(DOWN * 2 + ENTER, wizard.pick_receiver))


class KeyPresses(unittest.TestCase):
    def read_all(self, data: bytes, count: int) -> list:
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        os.write(w, data)
        keys = ui.Keys()
        keys.fd = r
        return [keys.read(0.2) for _ in range(count)]

    def test_presses_that_arrive_together_are_read_one_by_one(self):
        # key repeat, or a loop busy with a slow TV reply, delivers several presses in a single read
        right = b"\x1b[C"
        self.assertEqual(self.read_all(right * 3 + b"q", 5), ["right", "right", "right", "q", None])
        self.assertEqual(self.read_all(right + b"q", 3), ["right", "q", None])

    def test_every_key_the_player_uses(self):
        data = b"\x1b[A\x1b[B\x1b[D\x1b[C Qq"
        self.assertEqual(self.read_all(data, 7), ["up", "down", "left", "right", "space", "q", "q"])

    def test_other_escape_sequences_are_not_mistaken_for_letters(self):
        # Delete, F5, and a terminal's reply must not turn into a stray "q" or arrow
        got = self.read_all(b"\x1b[3~\x1b[15~\x1bOP", 3)
        self.assertNotIn("q", got)
        self.assertFalse(set(got) & {"up", "down", "left", "right", "space"})


if __name__ == "__main__":
    unittest.main()
