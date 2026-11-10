"""The wizard: pick a movie, choose a quality, watch it get ready, cast it to a TV."""

from __future__ import annotations

import math
import os
import re
import select
import sys
import time
from pathlib import Path

import questionary
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, ProgressBar, ProgressColumn, SpinnerColumn, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import __version__, cast, engine, finder, nativecast
from .telemetry import SessionReader, Snapshot, Telemetry

THEME = Theme({"accent": "bold cyan", "ok": "green", "bad": "bold red", "warn": "yellow", "dim": "dim"})
QSTYLE = questionary.Style([
    ("qmark", "fg:#00afff bold"), ("question", "bold"), ("answer", "fg:#00d787 bold"),
    ("pointer", "fg:#00afff bold"), ("highlighted", "fg:#00afff bold"), ("selected", "fg:#00d787"),
    ("instruction", "fg:#808080"), ("text", ""), ("hint", "fg:#808080"),
])

# name, peak Mbit/s allowed, who it is for
PRESETS = [
    ("Weak Wi-Fi", engine.DEFAULT_MAX_MBPS, "2.4 GHz or far from the router (safest)"),
    ("Good Wi-Fi", 10.0, "5 GHz in the same room"),
    ("Great Wi-Fi", 20.0, "5 GHz with a strong signal"),
    ("Original", engine.NO_CAP_MBPS, "keep the quality, only fix what is incompatible"),
]


# ---------------------------------------------------------------- progress bars

class SpeedColumn(ProgressColumn):
    def render(self, task) -> Text:
        return Text(f"{task.speed:5.1f}×" if task.speed else "   – ", style="cyan")


class RichBar:
    """Adapter from the engine's four-method progress protocol to a Rich progress bar."""

    def __init__(self, console: Console, label: str, total: float):
        self.extra = ""
        self.total = total
        self._p = Progress(
            SpinnerColumn(style="cyan"), TextColumn("[bold]{task.description:<10}"), BarColumn(bar_width=None),
            TaskProgressColumn(), SpeedColumn(), TimeElapsedColumn(), TextColumn("[dim]ETA"), TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[extra]}"), console=console)
        self._task = self._p.add_task(label, total=total, extra="")
        self._p.start()

    def update(self, done: float) -> None:
        self._p.update(self._task, completed=done, extra=self.extra)

    def finish(self) -> None:
        self._p.update(self._task, completed=self.total, extra=self.extra)
        self._p.stop()

    def abort(self) -> None:
        self._p.stop()


# ---------------------------------------------------------------- prompts

class Cancelled(Exception):
    pass


class StyledTitle(list):
    """A coloured title, a list of (style, text) pairs, that questionary's type-to-filter can search.

    The filter calls `.lower()` on every title, which a plain list does not have: typing a letter in the menu
    would crash the prompt and leave it hung."""

    def lower(self) -> str:
        return "".join(text for _, text in self).lower()


class Prompter:
    """Arrow-key menus. Tests replace this with a scripted one."""

    def select(self, message: str, choices: list, default=None, filterable: bool = False):
        # questionary swaps a None value for the title ("Quit" would come back as the string "Quit"), so it is
        # given each choice's position instead and the real value is looked up afterwards
        items = [questionary.Choice(title=StyledTitle(t) if isinstance(t, list) else t, value=i)
                 for i, (t, _) in enumerate(choices)]
        default_item = next((item for item, (_, v) in zip(items, choices) if v == default), None) \
            if default is not None else None
        picked = questionary.select(
            message, choices=items, default=default_item, style=QSTYLE, qmark="❯", pointer="❯",
            use_search_filter=filterable, use_jk_keys=not filterable,
            instruction="(↑/↓ move, Enter select" + (", type to filter)" if filterable else ")"),
        ).unsafe_ask()
        return choices[picked][1]

    def confirm(self, message: str, default: bool = True) -> bool:
        return questionary.confirm(message, default=default, style=QSTYLE, qmark="❯").unsafe_ask()

    def text(self, message: str, default: str = "") -> str:
        return questionary.text(message, default=default, style=QSTYLE, qmark="❯").unsafe_ask()

    def path(self, message: str) -> str:
        return questionary.path(message, style=QSTYLE, qmark="❯").unsafe_ask()


class Keys:
    """Single-key reader for the live cast controls. Returns None on timeout."""

    NAMES = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left", b" ": "space"}
    # one key press: an escape sequence (arrows), a lone escape, an ASCII character or a UTF-8 character
    PRESS = re.compile(rb"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]|\x1b|[\x00-\x7f]|[\xc0-\xff][\x80-\xbf]*")

    def __init__(self):
        self.pending: list = []

    def __enter__(self):
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self, timeout: float = 0.5) -> str | None:
        """One key per call. Several presses that arrive together (key repeat, a busy loop) are queued, not merged."""
        if not self.pending:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return None
            data = os.read(self.fd, 64)
            self.pending = [self.NAMES.get(p, p.decode(errors="ignore").lower()) for p in self.PRESS.findall(data)]
        return self.pending.pop(0) if self.pending else None


def default_telemetry(session, receiver: cast.Receiver, curve):
    """Live TV/link/buffer readings. The session reports on itself, so no second Cast connection is opened. None for
    a session that cannot (a test double)."""
    if callable(getattr(session, "tv_status", None)) and callable(getattr(session, "link_sample", None)):
        return Telemetry(SessionReader(session), session.link_sample, curve)
    return None


# ---------------------------------------------------------------- the wizard

class Wizard:
    def __init__(self, console: Console | None = None, prompt: Prompter | None = None, keys=None,
                 find_videos=finder.find_videos, discover=cast.discover, telemetry=default_telemetry,
                 caster=nativecast.NativeCast, stream_prepare=None):
        self.console = console or Console(theme=THEME)
        self.prompt = prompt or Prompter()
        self.keys = keys if keys is not None else (Keys if sys.stdin.isatty() else None)
        self.find_videos, self.discover, self.caster = find_videos, discover, caster
        self.telemetry = telemetry
        self._stream_prepare = stream_prepare  # None: onthefly.prepare, looked up when first needed

    # --- small helpers
    def stream_prepare(self, sc: engine.Scan, an: engine.Analysis, progress):
        """Get the on-the-fly cast ready (audio pass, subtitles). Imported late: the mode is optional."""
        if self._stream_prepare is None:
            from . import onthefly
            self._stream_prepare = onthefly.prepare
        return self._stream_prepare(sc, an, progress)

    def bar(self, label: str, total: float) -> RichBar:
        return RichBar(self.console, label, total)

    def step(self, n: int, title: str) -> None:
        self.console.print()
        self.console.rule(f"[accent]{n} · {title}", style="dim", align="left")

    def error(self, message: str) -> None:
        self.console.print(Panel(message, title="[bad]Something went wrong", border_style="red", expand=False))

    def banner(self) -> None:
        self.console.print(Panel.fit(
            f"[accent]castmymovie[/]  [dim]v{__version__}[/]\n"
            "[dim]Pick a movie → make it cast-ready → play it on your Google TV[/]",
            border_style="cyan", padding=(1, 3)))

    # --- main loop
    def run(self, path: Path | None = None) -> int:
        self.banner()
        if not engine.have_ffmpeg():
            self.error("ffmpeg is not installed.\n\nInstall it with:  [bold]brew install ffmpeg[/]")
            return 1
        try:
            while True:
                src = path or self.pick_file()
                path = None
                if src is None:
                    return 0
                try:
                    outcome = self.prepare(src)
                except engine.CastError as e:
                    self.error(escape(str(e)))
                    outcome = None
                if outcome is not None:
                    self.offer_cast(outcome)
                if not self.prompt.confirm("Do another movie?", default=False):
                    self.console.print("\n[dim]Enjoy the movie![/]")
                    return 0
        except (KeyboardInterrupt, EOFError, Cancelled):  # Ctrl-D at a text prompt raises EOFError
            self.console.print("\n[dim]Cancelled. Bye![/]")
            return 130

    # --- 1: pick a file
    def pick_file(self) -> Path | None:
        self.step(1, "Pick a movie")
        with self.console.status("[dim]Looking for movies on this Mac…", spinner="dots"):
            found = self.find_videos()
        choices = [(self.file_title(f), f.path) for f in found]
        choices += [("Type or drop a file path…", "__path__"), ("Quit", None)]
        while True:
            pick = self.prompt.select("Which movie?" if found else "No movies found automatically.", choices,
                                      filterable=True)
            if pick != "__path__":
                return pick
            typed = self.prompt.path("Path to the movie (you can drag the file here):")
            candidate = finder.clean_dropped_path(typed)
            if candidate.is_file():
                return candidate
            self.console.print(f"[warn]That is not a file: {escape(str(candidate))}[/]")

    def file_title(self, f: finder.Found):
        name = f.path.name if len(f.path.name) <= 58 else f.path.name[:40] + "…" + f.path.name[-16:]
        home, parent = Path.home(), f.path.parent
        folder = "~" if parent == home else f"~/{parent.relative_to(home)}" if home in parent.parents else str(parent)
        mark = "  ✓ converted" if engine.output_path(f.path).exists() else ""
        return [("class:text", name), ("class:hint", f"   {engine.fmt_size(f.size)}   {folder}{mark}")]

    # --- 2 + 3: check, choose quality, convert
    def prepare(self, src: Path):
        self.step(2, "Checking your movie")
        sc = engine.scan(src, self.bar)
        audio_index = self.pick_audio(sc)
        self.show_facts(sc, audio_index)

        self.step(3, "Choose the quality")
        an = self.pick_quality(sc, audio_index)
        self.show_report(an)

        if an.plan.video == "encode" and self.pick_when(an) == "otf":
            outcome = self.stream_prepare(sc, an, self.bar)
            self.show_stream_ready(outcome)
            return outcome

        if not an.plan.changes:
            self.console.print("\n[ok]✓[/] Already compatible, nothing to convert.")
            outcome = engine.convert(sc, an, self.bar)
            self.show_outcome(outcome, an)
            return outcome
        if an.plan.video == "encode":
            self.console.print("\n[warn]This needs a re-encode, which takes a while.[/] You can cancel with Ctrl-C.")
        self.console.print()
        outcome = self.convert_fully(sc, an)
        if outcome is not None:
            self.show_outcome(outcome, an)
        return outcome

    def pick_when(self, an: engine.Analysis) -> str:
        """A re-encode is slow: play only once it is done ("full"), or start after a few seconds ("otf")?"""
        return self.prompt.select("When should the movie start playing?", [
            ("Convert first, then play (recommended)", "full"),
            ("Cast on the fly, while it converts (experimental)", "otf")], default="full")

    def convert_fully(self, sc: engine.Scan, an: engine.Analysis):
        """engine.convert, asking what to do when the converted file is already there. None if cancelled."""
        try:
            return engine.convert(sc, an, self.bar)
        except engine.OutputExists as e:
            action = self.prompt.select(f"{e.path.name} already exists.", [
                ("Use the existing file", "use"), ("Convert again (overwrite)", "redo"), ("Cancel", "cancel")])
            if action == "cancel":
                return None
            return (engine.reuse_existing(sc, an, self.bar) if action == "use"
                    else engine.convert(sc, an, self.bar, overwrite=True))

    def show_stream_ready(self, outcome) -> None:
        lines = [f"[bold]{escape(outcome.video.name)}[/]",
                 "Playback starts in a few seconds and the movie keeps converting while it plays."]
        for s in outcome.subtitles:
            lines.append(f"[ok]✓[/] Subtitles: {escape(s.name)}")
        self.console.print()
        self.console.print(Panel("\n".join(lines), title="[ok]Ready", border_style="green", expand=False))

    def pick_audio(self, sc: engine.Scan) -> int | None:
        audios = sc.audios
        if len(audios) < 2:
            return None
        default = engine.pick_audio(audios, None)
        choices = []
        for a in audios:
            tags = a.get("tags", {})
            title = f"#{a['index']}  {tags.get('language', 'und')}  {a.get('codec_name', 'unknown').upper()} {a.get('channel_layout', '')}"
            if tags.get("title"):
                title += f"  ({tags['title']})"
            choices.append((title, a["index"]))
        return self.prompt.select("This movie has several audio tracks. Which one?", choices, default=default["index"])

    def show_facts(self, sc: engine.Scan, audio_index: int | None) -> None:
        v = sc.video
        a = next((x for x in sc.audios if x["index"] == audio_index), None) if audio_index is not None \
            else engine.pick_audio(sc.audios, None)
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        t.add_row("File", f"[bold]{escape(sc.path.name)}[/]")
        t.add_row("Length", f"{engine.fmt_dur(sc.duration)}   {engine.fmt_size(sc.path.stat().st_size)}")
        t.add_row("Video", f"{v.get('codec_name', 'unknown').upper()} {v.get('width')}×{v.get('height')}  {engine.fps_of(v):.3g} fps")
        if a:
            t.add_row("Audio", escape(f"{a.get('codec_name', 'unknown').upper()} {a.get('channel_layout', '')}  [{a.get('tags', {}).get('language', 'und')}]"))
        t.add_row("Bitrate", f"average {sc.video_avg_bps() / 1e6:.1f} Mbit/s, [bold]peak {sc.video_peak_bps() / 1e6:.1f} Mbit/s[/]")
        self.console.print()
        self.console.print(Panel(t, border_style="dim", expand=False))

    def preview(self, an: engine.Analysis) -> str:
        if an.plan.mode == "re-encode" and an.plan.video == "copy":
            return "remux + audio"  # only the sound is converted: seconds, not a slow video encode
        return {"as-is": "ready as is", "remux": "instant remux", "re-encode": "re-encode (slow)"}[an.plan.mode]

    def pick_quality(self, sc: engine.Scan, audio_index: int | None) -> engine.Analysis:
        choices = []
        for name, mbps, who in PRESETS:
            an = engine.analyze(sc, mbps, audio_index=audio_index)
            limit = "no cap" if mbps >= engine.NO_CAP_MBPS else f"{mbps:g} Mbit/s"
            choices.append((f"{name:<12}{limit:<10} {self.preview(an):<17} {who}", mbps))
        choices.append(("Custom…", "custom"))
        pick = self.prompt.select("How good is the Wi-Fi to your TV?", choices, default=PRESETS[0][1])
        if pick == "custom":
            while True:
                raw = self.prompt.text("Maximum peak bitrate in Mbit/s:", default="8")
                try:
                    pick = float(raw)
                    if pick > 0:
                        break
                except ValueError:
                    pass
                self.console.print("[warn]Enter a number such as 8 or 12.5[/]")
        return engine.analyze(sc, pick, audio_index=audio_index)

    def show_report(self, an: engine.Analysis) -> None:
        t = Table.grid(padding=(0, 2))
        for c in an.checks:
            icon = "[ok]✓[/]" if c.ok else "[bad]✗[/]" if c.ok is False else "[warn]![/]"
            t.add_row(icon, f"[bold]{c.name}[/]", escape(c.detail))
        self.console.print()
        self.console.print(t)
        if an.plan.changes:
            self.console.print(f"\n[accent]Plan:[/] {an.plan.mode}  [dim]({escape('; '.join(an.plan.steps))})[/]")
            if an.plan.video == "copy" and an.plan.audio != "encode":
                self.console.print("[dim]Streams are copied untouched: lossless, takes seconds.[/]")

    def show_outcome(self, outcome: engine.Outcome, an: engine.Analysis) -> None:
        lines = [f"[bold]{escape(outcome.video.name)}[/]  [dim]{engine.fmt_size(outcome.video.stat().st_size)}"
                 f"  ·  done in {engine.fmt_dur(outcome.seconds)}[/]"]
        ver = outcome.verification
        if ver:
            if ver.ok:
                lines.append(f"[ok]✓[/] Verified: duration matches, index at the front, {escape(ver.verdict)}, "
                             f"test decode {ver.decode_speed:.0f}× real-time")
            else:
                lines += [f"[bad]✗[/] {escape(p)}" for p in ver.problems]
        for s in outcome.subtitles:
            lines.append(f"[ok]✓[/] Subtitles: {escape(s.name)}")
        self.console.print()
        self.console.print(Panel("\n".join(lines), title="[ok]Ready", border_style="green", expand=False))

    # --- 4: cast
    def offer_cast(self, outcome) -> None:
        """Cast `outcome`: a converted file (engine.Outcome) or a movie being converted on the fly (a StreamOutcome)."""
        self.step(4, "Play it on your TV")
        try:
            self.cast_loop(outcome)
        finally:
            if getattr(outcome, "on_the_fly", False):  # the encoded segments are not kept
                outcome.cleanup()

    def cast_loop(self, outcome) -> None:
        stream_outcome = outcome
        streaming = getattr(outcome, "on_the_fly", False)
        if not self.prompt.confirm("Play it on your TV now?", default=True):
            return
        receiver, subtitles = None, None
        while True:
            receiver = receiver or self.pick_receiver()
            if receiver is None:
                return
            if subtitles is None:
                subtitles = self.pick_subtitles(outcome.subtitles)
            start = self.ask_start(outcome.duration)
            extra = {"stream": outcome.make_stream()} if streaming else {}  # a new encoder for every attempt
            session = self.caster(receiver, outcome.video, start, subtitles=subtitles, duration=outcome.duration, **extra)
            ok = self.cast_to(session, receiver, outcome.video, outcome.curve)
            options = [("Cast it again", "again")]
            if streaming:
                options.append(("Convert it fully, then cast (slower, reliable)", "full"))
            options += [("Choose another TV", "tv"), ("Done with this movie", "done")]
            action = self.prompt.select("What next?", options, default="full" if streaming and not ok else "again")
            if action == "done":
                return
            if action == "tv":
                receiver = None
            if action == "full":
                converted = self.convert_stream_fully(stream_outcome)
                if converted is not None:  # from here on it is an ordinary converted file
                    stream_outcome.cleanup()
                    outcome, streaming = converted, False

    def convert_stream_fully(self, stream_outcome):
        """The on-the-fly cast did not work out: convert the movie the usual way. None if it fails or is cancelled."""
        self.console.print()
        try:
            outcome = self.convert_fully(stream_outcome.scan, stream_outcome.analysis)
        except engine.CastError as e:
            self.error(escape(str(e)))
            return None
        if outcome is not None:
            self.show_outcome(outcome, stream_outcome.analysis)
        return outcome

    def pick_subtitles(self, subtitles: list) -> list:
        """Which subtitles the TV should show, if the movie has any. Returns the chosen .vtt files (one or none)."""
        if not subtitles:
            return []
        choices = [("No subtitles", "off")] + [(f"{s.stem.rsplit('.', 1)[-1]}  ({s.name})", s) for s in subtitles]
        pick = self.prompt.select("Show subtitles on the TV?", choices, default="off")
        return [] if pick == "off" else [pick]

    def pick_receiver(self):
        while True:
            with self.console.status("[dim]Looking for Cast devices on your Wi-Fi…", spinner="dots"):
                receivers = self.discover()
            choices = [(r.label, r) for r in receivers]
            choices += [("Search again", "again"), ("Enter an IP address…", "ip"), ("Skip", None)]
            if not receivers:
                self.console.print("[warn]No Cast devices found.[/] [dim]Check that the TV is on and on the same Wi-Fi "
                                   "as this Mac.[/]")
            pick = self.prompt.select("Which TV?" if receivers else "What now?", choices,
                                      default=receivers[0] if receivers else "again")
            if pick == "again":
                continue
            if pick == "ip":
                receiver = self.ask_address()
                if receiver:
                    return receiver
                continue
            return pick

    def ask_address(self):
        """A Receiver for the address typed in, asking again until it is a home-network one. None if left empty."""
        while True:
            typed = self.prompt.text("IP address of the TV:").strip()
            if not typed:
                return None
            ip = cast.lan_address(typed)
            if ip:
                return cast.Receiver(name=ip, host=ip)
            self.console.print(f"[warn]{escape(typed)} is not an address on your home network.[/] Type the TV's address "
                               "as four numbers, like 192.168.1.20 (your router lists it), or press Enter to go back.")

    def ask_start(self, duration: float = 0.0) -> float:
        while True:
            raw = self.prompt.text("Start at (e.g. 45:10 or 1:12:30, Enter = from the beginning):")
            secs = cast.parse_time(raw)
            if secs is None:
                self.console.print("[warn]Use minutes:seconds or hours:minutes:seconds, for example 1:12:30[/]")
            elif duration and secs >= duration:
                self.console.print(f"[warn]That is past the end of the movie ({engine.fmt_dur(duration)}).[/]")
            else:
                return secs

    def cast_to(self, session, receiver: cast.Receiver, video: Path, curve=None) -> bool:
        """Start `session` (a cast.CastSession), run the player until it ends, always stop it. False if it never started
        or gave up on its own (`session.failed`). A movie that finished is not a failure."""
        self.console.print("[dim]If macOS asks whether Python may accept incoming network connections, choose "
                           "Allow: the TV downloads the movie from this Mac.[/]")
        try:
            with self.console.status(f"[dim]Starting on {escape(receiver.name)}…", spinner="dots"):
                session.start()
        except engine.CastError as e:
            session.stop()
            self.error(f"{escape(str(e))}\n\n[dim]Make sure the TV is awake and on the same Wi-Fi as this Mac.[/]")
            return False
        except BaseException:  # Ctrl-C or a kill (SystemExit): the half-started cast must not outlive us
            session.stop()
            raise
        tele = None
        try:
            tele = self.telemetry(session, receiver, curve) if self.telemetry else None
            if tele:
                tele.start()
            self.live_controls(session, receiver, video, tele)
        finally:
            if tele:
                tele.stop()
            session.stop()
        self.console.print(f"[dim]Stopped casting to {escape(receiver.name)}.[/]")
        return not getattr(session, "failed", False)

    def buffer_row(self, snap: Snapshot | None):
        if snap is None or snap.buffer_s is None or not math.isfinite(snap.buffer_s):
            return None
        secs = snap.buffer_s
        shown = round(secs) if secs < 10 else int(round(secs / 5) * 5)
        colour, word = ("green", "comfortable") if secs >= 15 else ("yellow", "low") if secs >= 5 else ("red", "critical")
        if snap.refilling:
            colour, word = "cyan", "refilling"
        bar = ProgressBar(total=60, completed=min(secs, 60), width=24, complete_style=colour, finished_style=colour)
        row = Table.grid(padding=(0, 1))
        row.add_row(Text("Buffer", style="dim"), bar,
                    Text.from_markup(f"≈ {shown} s [dim](estimate)[/] [{colour}]{word}[/]"))
        return row

    def link_row(self, snap: Snapshot | None):
        if snap is None:
            return None
        parts = []
        if snap.delivered_mbps is not None:
            text = f"{snap.delivered_mbps:.1f} Mbit/s"
            if snap.needed_mbps:
                text += f" (needs {snap.needed_mbps:.1f})"
            parts.append(text)
        if snap.rtt_ms is not None:
            parts.append(f"{snap.rtt_ms:.0f} ms")
        if snap.retx_pct is not None:
            parts.append(f"{snap.retx_pct:.1f}% retransmits")
        if not parts:
            return None
        dot = {"good": "[ok]● good[/]", "fair": "[warn]● fair[/]", "poor": "[bad]● poor[/]",
               "refilling": "[cyan]● refilling[/]"}.get(snap.verdict or "", "")
        row = Table.grid(padding=(0, 1))
        row.add_row(Text("Link  ", style="dim"), Text.from_markup(" · ".join(parts) + "   " + dot))
        return row

    def encoder_row(self, status, position: float):
        """How far the on-the-fly encoder has got and whether it keeps up. None when there is no encoder."""
        if status is None or not math.isfinite(status.frontier_s):
            return None
        text = f"encoded to {engine.fmt_dur(status.frontier_s)}"
        if status.speed and math.isfinite(status.speed):
            text += f" ({status.speed:.1f}x)"
        ahead = max(status.frontier_s - position, 0.0) if math.isfinite(position) else 0.0
        text += f"  {round(ahead)} s ahead" if ahead < 60 else f"  {round(ahead / 60)} min ahead"
        if status.slow:
            text += "  [warn]slow: the picture may stall[/]"
        row = Table.grid(padding=(0, 1))
        row.add_row(Text("Encoder", style="dim"), Text.from_markup(text))
        return row

    def render_cast(self, st: cast.PlayState, receiver: cast.Receiver, video: Path, interactive: bool,
                    snap: Snapshot | None = None, telemetry_on: bool = False, notice: str = "",
                    encoder=None) -> Panel:
        over = not st.alive  # the TV's player is idle then, which a session counts as buffering: say "ended" instead
        icon = "■ " if over else "⏸ " if st.paused else "▶ "
        tv_buffering = snap is not None and snap.tv_state == "BUFFERING" and not st.paused
        status = ("[dim]ended[/]" if over else "[warn]paused[/]" if st.paused
                  else "[warn]buffering…[/]" if (st.buffering or tv_buffering) else "[ok]playing[/]")
        bar = ProgressBar(total=max(st.length, 1), completed=min(st.position, max(st.length, 1)), width=44,
                          complete_style="cyan", finished_style="cyan")
        row = Table.grid(padding=(0, 1))
        row.add_row(Text(icon + engine.fmt_dur(st.position), style="bold"), bar,
                    Text(engine.fmt_dur(st.length), style="dim"))
        help_ = "[dim]space[/] pause   [dim]←/→[/] ±10 s   [dim]↑/↓[/] ±2 min   [dim]q[/] stop" if interactive \
            else "[dim]Press Ctrl-C to stop[/]"
        parts = [Text(video.name, style="bold"), "", row, ""]
        if notice:
            parts += [Text(notice, style="dim"), ""]
        health = [r for r in (self.buffer_row(snap), self.link_row(snap), self.encoder_row(encoder, st.position))
                  if r is not None]
        if health:
            parts += health + [""]
        elif telemetry_on:
            parts += [Text.from_markup("[dim]Measuring the link…[/]"), ""]
        parts.append(Text.from_markup(status if over else f"{status}   {help_}"))
        return Panel(Group(*parts), title=f"[accent]Casting to {escape(receiver.name)}", border_style="cyan",
                     expand=False)

    @staticmethod
    def encoder_status(session):
        """The on-the-fly encoder's progress, or None (a plain file cast, or a session without it)."""
        read = getattr(session, "encoder_status", None)
        return read() if callable(read) else None

    def live_controls(self, session, receiver: cast.Receiver, video: Path, tele=None) -> None:
        interactive = self.keys is not None
        keys_cm = self.keys() if interactive else None
        steps = {"left": -10, "right": 10, "down": -120, "up": 120}
        measurable = callable(getattr(session, "link_sample", None))
        with Live(console=self.console, refresh_per_second=4, transient=False) as live:
            if keys_cm:
                keys_cm.__enter__()
            try:
                while True:
                    st = session.state()
                    if tele:
                        tele.position = st.position
                    live.update(self.render_cast(st, receiver, video, interactive,
                                                 tele.latest if tele else None,
                                                 telemetry_on=tele is not None and measurable,
                                                 notice=getattr(session, "notice", "") or "",
                                                 encoder=self.encoder_status(session)))
                    if not st.alive:
                        return
                    key = keys_cm.read(0.5) if keys_cm else time.sleep(0.5)
                    if key in ("q", "esc"):
                        return
                    if key == "space":
                        session.toggle_pause()
                    elif key in steps:
                        session.seek_by(steps[key])
            finally:
                if keys_cm:
                    keys_cm.__exit__(None, None, None)
