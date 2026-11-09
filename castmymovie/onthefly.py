"""Cast on the fly: the TV starts playing while ffmpeg is still encoding.

Converting a whole movie before the first frame can take most of an hour. Here ffmpeg writes the movie as HLS
segments of 6 seconds (MPEG-TS, H.264 + AAC) into a temp folder and the TV plays them as they appear:

- The TV is given the complete VOD playlist from the start, generated from the movie's length (`Timeline`), so it
  shows the full duration and a normal seek bar. The segments only have to exist when the TV asks for them.
- `OnTheFly.segment(n)` hands out segment n, waiting for the encoder when it is about to get there. When the TV asks
  for one far from where the encoder is (a seek), ffmpeg is restarted at that segment. Segments already written stay
  on disk, so seeking back is instant, and an encoder that reaches a segment written earlier stops there.
- Audio that has to be converted is done first, in a separate quick pass (`prepare`), so the live encoder only
  handles video and copies the audio, which also makes restarts clean.

Everything lives in a temp folder named after this process and is deleted on stop; a crash leaves one behind, and
the next start sweeps folders whose process is gone.
"""

from __future__ import annotations

import dataclasses
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, ClassVar

from . import engine
from .engine import SEG_SECONDS, CastError

LOOKAHEAD = 10          # segments: a request this far ahead of the encoder is worth waiting for, further means a seek
SETTLE = 0.3            # seconds a request waits before restarting the encoder: n+1 often arrives just before n
SLOW_BELOW = 1.2        # encoding slower than this (x real time) cannot stay ahead of the TV for long
FOLDER = "castmymovie-otf-"


class Timeline:
    """Where the segments are, for a movie of `duration` seconds at `fps`. Pure arithmetic, no ffmpeg.

    Every segment is a whole number of frames (`seg_frames`), so all but the last last exactly `seg_len` seconds."""

    def __init__(self, duration: float, fps):
        self.fps = fps if isinstance(fps, Fraction) else engine.snap_rate(float(fps))
        self.duration = max(float(duration), 0.0)
        self.seg_frames = max(1, round(SEG_SECONDS * self.fps))
        self.seg_len = float(self.seg_frames / self.fps)
        self.count = max(1, math.ceil(self.duration / self.seg_len - 1e-9))

    def start(self, i: int) -> float:
        return i * self.seg_len

    def end(self, i: int) -> float:
        return min(self.start(i + 1), self.duration) if self.duration else self.start(i + 1)

    def index_at(self, t: float) -> int:
        return min(max(int((t + 1e-6) / self.seg_len), 0), self.count - 1)

    @staticmethod
    def segment_name(i: int) -> str:
        return f"seg{i:05d}.ts"

    def playlist(self) -> bytes:
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{round(self.seg_len)}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for i in range(self.count):
            length = (self.duration - self.start(i)) if i == self.count - 1 and self.duration else self.seg_len
            lines += [f"#EXTINF:{min(length, self.seg_len):.6f},", self.segment_name(i)]
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()


@dataclass(frozen=True)
class EncoderStatus:
    frontier_s: float        # media seconds encoded without a gap from where the encoder started
    speed: float | None      # x real time, as ffmpeg reports it
    slow: bool               # too slow to stay ahead of the TV


class OnTheFly:
    """One encoder for one cast attempt. `make_cmd(start_index)` returns the ffmpeg command that writes the segments
    of `timeline` into `workdir` from that segment on (see `engine.build_hls_cmd`)."""

    def __init__(self, timeline: Timeline, make_cmd: Callable[[int], list], workdir: Path, clock=time.monotonic):
        self.timeline, self.make_cmd, self.workdir, self.clock = timeline, make_cmd, Path(workdir), clock
        self.error: str | None = None
        self._cv = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._errlog = None
        self._run = 0                    # which encoder run is current; a reader of an older run ignores what it sees
        self._run_start = 0              # first segment the current run writes
        self._cursor = 0                 # first segment of the current run that is not written yet
        self._before: set = set()        # segments that existed when the run started (it stops on reaching one)
        self._speed: float | None = None
        self._pending: set = set()       # segments asked for that the encoder is not going to reach
        self._closed = False

    # --- the surface the media server and the cast use
    def begin(self, start_seconds: float) -> None:
        with self._cv:
            self.workdir.mkdir(parents=True, exist_ok=True)
            self._restart(self.timeline.index_at(start_seconds))

    def wait_ready(self, segments: int, timeout: float) -> None:
        """Block until the first `segments` segments of this run exist. CastError when ffmpeg dies or takes too long."""
        deadline = self.clock() + timeout
        with self._cv:
            want = range(self._run_start, min(self._run_start + segments, self.timeline.count))
            while not all(self._path(i).exists() for i in want):
                if self.error or self._closed:
                    raise CastError(self.error or "The encoder was stopped.")
                if self.clock() >= deadline:
                    raise CastError(f"The encoder did not produce the first seconds within {int(timeout)} s.")
                self._cv.wait(0.05)

    def segment(self, n: int, timeout: float) -> Path | None:
        """Segment n once it exists. None when the encoder died or `timeout` ran out. Restarts the encoder when the
        request is not something it will reach soon (a seek)."""
        deadline, asked = self.clock() + timeout, None
        path = self._path(n)
        with self._cv:
            while True:
                if path.exists():
                    return path
                if self.error or self._closed:
                    return None
                now = self.clock()
                self._advance()
                if self._covers(n):
                    self._pending.discard(n)
                    asked = None
                else:
                    self._pending.add(n)
                    asked = now if asked is None else asked
                    if now - asked >= SETTLE:
                        target, self._pending = min(self._pending), set()
                        asked = None
                        self._restart(target)
                if now >= deadline:
                    self._pending.discard(n)
                    return None
                self._cv.wait(0.05)

    def status(self) -> EncoderStatus:
        with self._cv:
            self._advance()
            frontier = self.timeline.end(self._cursor - 1) if self._cursor > self._run_start else \
                self.timeline.start(self._run_start)
            alive = self._proc is not None and self._proc.poll() is None
            return EncoderStatus(frontier, self._speed, alive and self._speed is not None and self._speed < SLOW_BELOW)

    def close(self) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._run += 1
            proc, self._proc = self._proc, None
            self._cv.notify_all()
        self._kill(proc)
        if self._errlog:
            self._errlog.close()
        shutil.rmtree(self.workdir, ignore_errors=True)

    # --- internals (call with the lock held, except _kill)
    def _path(self, i: int) -> Path:
        return self.workdir / self.timeline.segment_name(i)

    def _advance(self) -> None:
        """Move the cursor past every segment the current run has written, and stop the run when it reaches one an
        earlier run wrote."""
        while self._cursor < self.timeline.count and self._path(self._cursor).exists() \
                and self._cursor not in self._before:
            self._cursor += 1
        if self._proc is not None and self._proc.poll() is None and self._cursor in self._before:
            self._proc.terminate()  # it is about to redo finished work

    def _covers(self, n: int) -> bool:
        running = self._proc is not None and self._proc.poll() is None
        return running and self._cursor in range(self._run_start, self.timeline.count) \
            and self._run_start <= n <= self._cursor + LOOKAHEAD

    def _restart(self, index: int) -> None:
        old, self._proc = self._proc, None
        self._run += 1
        run = self._run
        self._run_start = self._cursor = index
        self._before = {i for i in range(self.timeline.count) if self._path(i).exists()}
        self._speed = None
        self._kill(old)  # ffmpeg answers terminate within a moment; holding the lock keeps restarts one at a time
        if self._closed:
            return
        if self._errlog:
            self._errlog.close()
        self._errlog = tempfile.TemporaryFile(mode="w+")
        try:
            proc = subprocess.Popen(self.make_cmd(index), stdout=subprocess.PIPE, stderr=self._errlog, text=True, bufsize=1)
        except OSError as e:
            self.error = f"could not start ffmpeg: {e}"
            self._cv.notify_all()
            return
        self._proc = proc
        threading.Thread(target=self._read, args=(proc, run, self._errlog), name="castmymovie-encoder", daemon=True).start()

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _read(self, proc: subprocess.Popen, run: int, errlog) -> None:
        for line in proc.stdout:  # ffmpeg -progress: a block of key=value lines every half second
            key, _, val = line.strip().partition("=")
            if key == "speed":
                try:
                    speed = float(val.rstrip("x"))
                except ValueError:
                    continue
                with self._cv:
                    if run == self._run:
                        self._speed = speed
                        self._advance()
                        self._cv.notify_all()
        code = proc.wait()
        proc.stdout.close()
        with self._cv:
            if run != self._run or self._closed:
                return  # we stopped it ourselves
            self._advance()
            if code and not (self._cursor in self._before and code < 0):  # our own terminate() at a finished segment
                try:
                    errlog.seek(0)
                    tail = "\n".join(errlog.read().strip().splitlines()[-12:])
                except (OSError, ValueError):
                    tail = ""
                self.error = f"ffmpeg stopped (exit {code}):\n{tail}".strip()
            self._cv.notify_all()


# ---------------------------------------------------------------- preparing a stream

@dataclass
class StreamOutcome:
    """What the wizard gets instead of an `engine.Outcome` when the movie is cast on the fly."""
    video: Path                  # the source movie (the TV shows its name; nothing is played from it directly)
    subtitles: list
    duration: float
    seconds: float
    scan: engine.Scan
    analysis: engine.Analysis
    folder: Path                 # holds the audio pass's output and one subfolder of segments per cast attempt
    audio_path: Path | None
    plan: engine.Plan            # the plan the live encoder follows (audio already handled)
    curve: None = None
    on_the_fly: ClassVar[bool] = True

    def make_stream(self) -> OnTheFly:
        sc, v = self.scan, self.scan.video
        timeline = Timeline(self.duration, engine.cfr_rate(v))
        workdir = Path(tempfile.mkdtemp(prefix="seg-", dir=self.folder))

        def make_cmd(start_index: int) -> list:
            return engine.build_hls_cmd(sc.path, self.audio_path, workdir, self.plan, sc.info, self.analysis.max_mbps,
                                        timeline.fps, timeline.seg_frames, start_index)
        return OnTheFly(timeline, make_cmd, workdir)

    def cleanup(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def prepare(sc: engine.Scan, an: engine.Analysis, progress: engine.ProgressFactory = engine.NullProgress) -> StreamOutcome:
    """Everything that is done before the TV starts: sweep leftovers, check the disk, convert the audio when it has to
    be, export the subtitles. The video itself is encoded live, by the streams `StreamOutcome.make_stream` makes."""
    started = time.monotonic()
    sweep_stale()
    plan = an.plan
    need = sc.duration * min(an.max_mbps, 40) * 1e6 / 8 * 1.1 + sc.duration * 48_000
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < need * 1.05:
        raise CastError(f"not enough disk space for the video segments: need ~{engine.fmt_size(need)}, "
                        f"have {engine.fmt_size(free)}")
    folder = Path(tempfile.mkdtemp(prefix=f"{FOLDER}{os.getpid()}-"))
    audio_path = None
    try:
        if engine.needs_audio_pass(plan, sc.info):
            audio_path = folder / "audio.mka"
            engine.run_ffmpeg(engine.build_audio_cmd(sc.path, audio_path, plan, sc.info), sc.duration,
                              "Converting audio", progress)
            a = next(s for s in sc.info["streams"] if s["index"] == plan.audio_idx)
            plan = dataclasses.replace(plan, audio="copy",
                                       audio_out_kbps=engine.audio_out_kbps(min(int(a.get("channels", 2) or 2), 6)))
        subs = engine.export_subtitles(sc.path, engine.output_path(sc.path), sc.subtitles)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return StreamOutcome(sc.path, subs, sc.duration, time.monotonic() - started, sc, an, folder, audio_path, plan)


def sweep_stale() -> None:
    """Remove folders a crashed run left in the temp directory (their process is gone)."""
    root = Path(tempfile.gettempdir())
    try:
        entries = list(root.glob(f"{FOLDER}*"))
    except OSError:
        return
    for d in entries:
        pid = d.name[len(FOLDER):].split("-", 1)[0]
        if not pid.isdigit() or _alive(int(pid)):
            continue
        shutil.rmtree(d, ignore_errors=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
