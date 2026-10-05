"""The wizard: pick a movie, choose a quality, watch it get ready, cast it to a TV with VLC."""

from __future__ import annotations

import os
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

from . import __version__, cast, engine, finder

THEME = Theme({"accent": "bold cyan", "ok": "green", "bad": "bold red", "warn": "yellow", "dim": "dim"})
QSTYLE = questionary.Style([
    ("qmark", "fg:#00afff bold"), ("question", "bold"), ("answer", "fg:#00d787 bold"),
    ("pointer", "fg:#00afff bold"), ("highlighted", "fg:#00afff bold"), ("selected", "fg:#00d787"),
    ("instruction", "fg:#808080"), ("text", ""), ("hint", "fg:#808080"),
])

# name, peak Mbps allowed, who it is for
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


class Prompter:
    """Arrow-key menus. Tests replace this with a scripted one."""

    def select(self, message: str, choices: list, default=None, filterable: bool = False):
        items = [questionary.Choice(title=t, value=v) for t, v in choices]
        default_item = next((c for c in items if c.value == default), None) if default is not None else None
        return questionary.select(
            message, choices=items, default=default_item, style=QSTYLE, qmark="❯", pointer="❯",
            use_search_filter=filterable, use_jk_keys=not filterable,
            instruction="(↑/↓ move, Enter select" + (", type to filter)" if filterable else ")"),
        ).unsafe_ask()

    def confirm(self, message: str, default: bool = True) -> bool:
        return questionary.confirm(message, default=default, style=QSTYLE, qmark="❯").unsafe_ask()

    def text(self, message: str, default: str = "") -> str:
        return questionary.text(message, default=default, style=QSTYLE, qmark="❯").unsafe_ask()

    def path(self, message: str) -> str:
        return questionary.path(message, style=QSTYLE, qmark="❯").unsafe_ask()


class Keys:
    """Single-key reader for the live cast controls. Returns None on timeout."""

    NAMES = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left", b" ": "space"}

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
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        data = os.read(self.fd, 8)
        return self.NAMES.get(data, data.decode(errors="ignore").lower())


# ---------------------------------------------------------------- the wizard

class Wizard:
    def __init__(self, console: Console | None = None, prompt: Prompter | None = None, keys=None,
                 find_videos=finder.find_videos, discover=cast.discover, find_vlc=cast.find_vlc,
                 caster=cast.VlcCast):
        self.console = console or Console(theme=THEME)
        self.prompt = prompt or Prompter()
        self.keys = keys if keys is not None else (Keys if sys.stdin.isatty() else None)
        self.find_videos, self.discover, self.find_vlc, self.caster = find_videos, discover, find_vlc, caster

    # --- small helpers
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
        except (KeyboardInterrupt, Cancelled):
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
        folder = str(f.path.parent).replace(str(Path.home()), "~")
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

        if not an.plan.changes:
            self.console.print("\n[ok]✓[/] Already compatible, nothing to convert.")
            outcome = engine.convert(sc, an, self.bar)
            self.show_outcome(outcome, an)
            return outcome
        if an.plan.mode == "re-encode":
            self.console.print("\n[warn]This needs a re-encode, which takes a while.[/] You can cancel with Ctrl-C.")
        self.console.print()
        try:
            outcome = engine.convert(sc, an, self.bar)
        except engine.OutputExists as e:
            action = self.prompt.select(f"{e.path.name} already exists.", [
                ("Use the existing file", "use"), ("Convert again (overwrite)", "redo"), ("Cancel", "cancel")])
            if action == "cancel":
                return None
            outcome = (engine.reuse_existing(sc, an, self.bar) if action == "use"
                       else engine.convert(sc, an, self.bar, overwrite=True))
        self.show_outcome(outcome, an)
        return outcome

    def pick_audio(self, sc: engine.Scan) -> int | None:
        audios = sc.audios
        if len(audios) < 2:
            return None
        default = engine.pick_audio(audios, None)
        choices = []
        for a in audios:
            tags = a.get("tags", {})
            title = f"#{a['index']}  {tags.get('language', 'und')}  {a['codec_name'].upper()} {a.get('channel_layout', '')}"
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
        t.add_row("Video", f"{v['codec_name'].upper()} {v.get('width')}×{v.get('height')}  {engine.fps_of(v):.3g} fps")
        if a:
            t.add_row("Audio", escape(f"{a['codec_name'].upper()} {a.get('channel_layout', '')}  [{a.get('tags', {}).get('language', 'und')}]"))
        t.add_row("Bitrate", f"average {sc.video_avg_bps() / 1e6:.1f} Mbps, [bold]peak {sc.video_peak_bps() / 1e6:.1f} Mbps[/]")
        self.console.print()
        self.console.print(Panel(t, border_style="dim", expand=False))

    def preview(self, an: engine.Analysis) -> str:
        return {"as-is": "ready as is", "remux": "instant remux", "re-encode": "re-encode (slow)"}[an.plan.mode]

    def pick_quality(self, sc: engine.Scan, audio_index: int | None) -> engine.Analysis:
        choices = []
        for name, mbps, who in PRESETS:
            an = engine.analyze(sc, mbps, audio_index=audio_index)
            limit = "no cap" if mbps >= engine.NO_CAP_MBPS else f"{mbps:g} Mbps"
            choices.append((f"{name:<12}{limit:<8} {self.preview(an):<17} {who}", mbps))
        choices.append(("Custom…", "custom"))
        pick = self.prompt.select("How good is the Wi-Fi to your TV?", choices, default=PRESETS[0][1])
        if pick == "custom":
            while True:
                raw = self.prompt.text("Maximum peak bitrate in Mbps:", default="8")
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
    def offer_cast(self, outcome: engine.Outcome) -> None:
        self.step(4, "Play it on your TV")
        vlc = self.find_vlc()
        if not vlc:
            self.console.print("[warn]VLC is not installed, so I can't cast.[/]  Install it with:  "
                               "[bold]brew install --cask vlc[/]")
            return
        if not self.prompt.confirm("Cast it to your TV with VLC now?", default=True):
            return
        receiver = None
        while True:
            receiver = receiver or self.pick_receiver()
            if receiver is None:
                return
            start = self.ask_start()
            self.cast_to(vlc, receiver, outcome.video, start)
            action = self.prompt.select("What next?", [
                ("Cast it again", "again"), ("Choose another TV", "tv"), ("Done with this movie", "done")])
            if action == "done":
                return
            if action == "tv":
                receiver = None

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
                ip = self.prompt.text("IP address of the TV:").strip()
                if ip:
                    return cast.Receiver(name=ip, host=ip)
                continue
            return pick

    def ask_start(self) -> float:
        while True:
            raw = self.prompt.text("Start at (e.g. 45:10 or 1:12:30, Enter = from the beginning):")
            secs = cast.parse_time(raw)
            if secs is not None:
                return secs
            self.console.print("[warn]Use minutes:seconds or hours:minutes:seconds, for example 1:12:30[/]")

    def cast_to(self, vlc: str, receiver: cast.Receiver, video: Path, start: float) -> bool:
        session = self.caster(vlc, receiver, video, start)
        try:
            with self.console.status(f"[dim]Starting on {escape(receiver.name)}…", spinner="dots"):
                session.start()
        except engine.CastError as e:
            session.stop()
            self.error(f"{escape(str(e))}\n\n[dim]Make sure the TV is awake and on the same Wi-Fi as this Mac.[/]")
            return False
        try:
            self.live_controls(session, receiver, video)
        finally:
            session.stop()
        self.console.print(f"[dim]Stopped casting to {escape(receiver.name)}.[/]")
        return True

    def render_cast(self, st: cast.PlayState, receiver: cast.Receiver, video: Path, interactive: bool) -> Panel:
        icon = "⏸ " if st.paused else "▶ "
        status = "[warn]paused[/]" if st.paused else "[warn]buffering…[/]" if st.buffering else "[ok]playing[/]"
        bar = ProgressBar(total=max(st.length, 1), completed=min(st.position, max(st.length, 1)), width=44,
                          complete_style="cyan", finished_style="cyan")
        row = Table.grid(padding=(0, 1))
        row.add_row(Text(icon + engine.fmt_dur(st.position), style="bold"), bar,
                    Text(engine.fmt_dur(st.length), style="dim"))
        help_ = "[dim]space[/] pause   [dim]←/→[/] ±30 s   [dim]↑/↓[/] ±5 min   [dim]q[/] stop" if interactive \
            else "[dim]Press Ctrl-C to stop[/]"
        return Panel(Group(Text(video.name, style="bold"), "", row, "", f"{status}   {help_}"),
                     title=f"[accent]Casting to {escape(receiver.name)}", border_style="cyan", expand=False)

    def live_controls(self, session, receiver: cast.Receiver, video: Path) -> None:
        interactive = self.keys is not None
        keys_cm = self.keys() if interactive else None
        with Live(console=self.console, refresh_per_second=4, transient=False) as live:
            if keys_cm:
                keys_cm.__enter__()
            try:
                while True:
                    st = session.state()
                    live.update(self.render_cast(st, receiver, video, interactive))
                    if not st.alive:
                        return
                    key = keys_cm.read(0.5) if keys_cm else time.sleep(0.5)
                    if key in ("q", "esc"):
                        return
                    if key == "space":
                        session.toggle_pause()
                    elif key == "left":
                        session.seek_by(-30)
                    elif key == "right":
                        session.seek_by(30)
                    elif key == "down":
                        session.seek_by(-300)
                    elif key == "up":
                        session.seek_by(300)
            finally:
                if keys_cm:
                    keys_cm.__exit__(None, None, None)
