"""The engine: probe a movie, decide what Chrome and Google Cast need, convert it, verify it.

No user interface in here. The wizard in `ui.py` drives it in three steps:

    scan(path)                       read the file once (streams + real bitrate profile)
    analyze(scan, max_mbps, ...)     cheap, can be re-run for every bitrate preset
    convert(scan, analysis)          remux / re-encode, verify, export subtitles
"""

from __future__ import annotations

import collections
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

# Peak (video + audio, worst 10 s window) a 2.4 GHz link can carry reliably. See README.
DEFAULT_MAX_MBPS = 6.0
NO_CAP_MBPS = 1000.0
WINDOW_S = 10
PEAK_TOLERANCE = 1.05
MAX_W, MAX_H = 1920, 1080  # Chrome tab mirroring tops out at 1080p
MAX_FPS = 30.5  # ... and at 30 fps by default
CHROME_MIN_HEVC = 107  # first Chrome with hardware HEVC decode
SEG_SECONDS = 6  # length of a cast-on-the-fly HLS segment (see onthefly.py)

H264_PROFILES = {"Baseline", "Constrained Baseline", "Main", "High"}
H264_MAX_LEVEL = 42  # Chromecast 3rd gen limit, the lowest common denominator
HEVC_MAX_LEVEL = 153
HEVC_PIX_FMTS = {"yuv420p", "yuv420p10le"}
AAC_PROFILES = {"LC", "HE-AAC", "HE-AACv2"}
MP4_AUDIO = {"aac", "mp3", "opus", "flac"}  # what a Cast TV plays itself from an MP4, up to 5.1
# VLC hands only AAC (up to stereo) and Opus to the TV untouched. Everything else (5.1 AAC, MP3, FLAC, AC-3, ...) it
# decodes and re-encodes live inside the cast, and that live transcode is the fragile part of a stream, so when the
# movie is meant for VLC (`analyze(..., for_vlc=True)`) such audio is converted up front, where there is no deadline.
VLC_AUDIO = {"aac", "opus"}
VLC_MAX_CH = 2
WEBM_AUDIO = {"opus", "vorbis"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
LANG_ALIASES = {
    "en": "eng", "fr": "fra", "fre": "fra", "de": "deu", "ger": "deu", "es": "spa",
    "it": "ita", "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho", "chi": "zho",
    "nl": "nld", "dut": "nld", "ru": "rus",
}


class CastError(Exception):
    pass


class OutputExists(CastError):
    def __init__(self, path: Path):
        super().__init__(f"{path.name} already exists")
        self.path = path


# ---------------------------------------------------------------- progress protocol

class NullProgress:
    """Does nothing. The UI passes a factory that returns something with the same four members."""

    extra = ""

    def __init__(self, label: str, total: float):
        pass

    def update(self, done: float) -> None:
        pass

    def finish(self) -> None:
        pass

    def abort(self) -> None:
        pass


ProgressFactory = Callable[[str, float], NullProgress]


# ---------------------------------------------------------------- probing

def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path: Path) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if p.returncode:
        raise CastError(f"ffprobe could not read {path.name}: {p.stderr.strip() or 'unknown error'}")
    return json.loads(p.stdout)


def fps_of(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = stream.get(key, "0/0").partition("/")
        try:
            n, d = float(num), float(den or 1)
        except ValueError:
            continue
        if n and d:
            return n / d
    return 0.0


@dataclass
class StreamStats:
    bins: list = field(default_factory=list)  # bytes per second of media time
    total: int = 0

    def add(self, t: float, size: int) -> None:
        i = int(max(t, 0))
        if i >= len(self.bins):
            self.bins.extend([0] * (i + 1 - len(self.bins)))
        self.bins[i] += size
        self.total += size

    def avg_bps(self, duration: float) -> float:
        return self.total * 8 / duration if duration else 0.0

    def peak_bps(self, window: int = WINDOW_S) -> float:
        if not self.bins:
            return 0.0
        window = min(window, len(self.bins))
        cur = sum(self.bins[:window])
        best = cur
        for i in range(window, len(self.bins)):
            cur += self.bins[i] - self.bins[i - window]
            best = max(best, cur)
        return best * 8 / window


def scan_bitrates(path: Path, duration: float, label: str = "Analyzing",
                  progress: ProgressFactory = NullProgress) -> dict:
    """Read every packet header (no decoding) to get true average and peak bitrates per stream."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time,dts_time,size",
           "-of", "compact=p=0", str(path)]
    stats: dict = collections.defaultdict(StreamStats)
    bar = progress(label, duration)
    latest = 0.0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20) as proc:
        try:
            for n, line in enumerate(proc.stdout):
                f = dict(kv.split("=", 1) for kv in line.strip().split("|") if "=" in kv)
                t = f.get("dts_time", "N/A")
                if t == "N/A":
                    t = f.get("pts_time", "N/A")
                if t == "N/A" or "size" not in f:
                    continue
                t = float(t)
                stats[int(f["stream_index"])].add(t, int(f["size"]))
                latest = max(latest, t)
                if n % 2000 == 0:
                    bar.update(latest)
        except BaseException:
            proc.kill()
            bar.abort()
            raise
    if proc.returncode:
        bar.abort()
        raise CastError("ffprobe failed while scanning packets (is the file truncated?)")
    bar.finish()
    return dict(stats)


def mp4_boxes(path: Path):
    """Yield (kind, payload offset) for each top-level MP4 box, in file order. Stops quietly at anything odd."""
    try:
        with open(path, "rb") as f:
            while True:
                at = f.tell()
                head = f.read(8)
                if len(head) < 8:
                    return
                size, kind = int.from_bytes(head[:4], "big"), head[4:].decode("latin1")
                header = 8
                if size == 1:
                    size, header = int.from_bytes(f.read(8), "big"), 16
                yield kind, at + header
                if size == 0 or size < header:  # 0 = "runs to the end of the file"; smaller is corrupt
                    return
                f.seek(at + size)
    except OSError:
        return


def mp4_is_faststart(path: Path) -> bool:
    """True when the moov atom precedes mdat, so playback can start before the file is fully read."""
    for kind, _ in mp4_boxes(path):
        if kind in ("moov", "mdat"):
            return kind == "moov"
    return False


def mdat_offset(path: Path) -> int | None:
    """Where the media data starts in an MP4: file offsets past it map to media time through the bitrate curve."""
    return next((at for kind, at in mp4_boxes(path) if kind == "mdat"), None)


def vlc_audio_issue(path: Path) -> str | None:
    """Why VLC would have to convert this file's audio live while casting, or None if it passes it through."""
    try:
        audio = next((s for s in probe(path)["streams"] if s["codec_type"] == "audio"), None)
    except (CastError, ValueError, KeyError):
        return None
    if audio is None:
        return None
    codec, ch = audio.get("codec_name", "unknown"), int(audio.get("channels", 2) or 2)
    if codec not in VLC_AUDIO:
        return f"{codec.upper()} audio"
    if ch > VLC_MAX_CH:
        return f"{audio.get('channel_layout') or f'{ch}-channel'} {codec.upper()} audio"
    return None


def chrome_major() -> int | None:
    candidates = [
        ("plist", "/Applications/Google Chrome.app/Contents/Info.plist"),
        ("cmd", "google-chrome"), ("cmd", "google-chrome-stable"), ("cmd", "chromium"),
    ]
    for kind, target in candidates:
        try:
            if kind == "plist" and os.path.exists(target):
                with open(target, "rb") as f:
                    return int(plistlib.load(f)["CFBundleShortVersionString"].split(".")[0])
            if kind == "cmd" and shutil.which(target):
                out = subprocess.run([target, "--version"], capture_output=True, text=True, timeout=10).stdout
                m = re.search(r"(\d+)\.", out)
                if m:
                    return int(m.group(1))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            continue
    return None


@dataclass
class Scan:
    """Everything we learn from reading the file once. Independent of the bitrate the user picks."""
    path: Path
    info: dict
    stats: dict
    duration: float
    chrome: int | None

    @property
    def video(self) -> dict:
        for s in self.info["streams"]:
            if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic"):
                return s
        raise CastError("no video stream found")

    @property
    def audios(self) -> list:
        return [s for s in self.info["streams"] if s["codec_type"] == "audio"]

    @property
    def subtitles(self) -> list:
        return [s for s in self.info["streams"] if s["codec_type"] == "subtitle"]

    def video_peak_bps(self) -> float:
        st = self.stats.get(self.video["index"])
        return st.peak_bps() if st else 0.0

    def video_avg_bps(self) -> float:
        st = self.stats.get(self.video["index"])
        return st.avg_bps(self.duration) if st else 0.0


def scan(path: Path, progress: ProgressFactory = NullProgress) -> Scan:
    if not have_ffmpeg():
        raise CastError("ffmpeg not found. Install it first (macOS: brew install ffmpeg).")
    path = Path(os.path.abspath(path))  # ffmpeg reads a relative "Movie: 1.mp4" as a URL and "-x.mp4" as an option
    info = probe(path)
    duration = float(info["format"].get("duration") or 0)
    if duration <= 0:
        raise CastError("could not determine the duration")
    s = Scan(path, info, {}, duration, chrome_major())
    s.video  # raises early when there is no video
    s.stats = scan_bitrates(path, duration, "Analyzing", progress)
    return s


# ---------------------------------------------------------------- analysis

@dataclass
class Check:
    name: str
    ok: bool | None  # True = fine, False = must be fixed, None = heads-up only
    detail: str


@dataclass
class Plan:
    video: str = "copy"   # copy | encode
    audio: str = "copy"   # copy | encode | none
    remux: bool = False   # container must change / be rewritten
    video_why: list = field(default_factory=list)
    audio_why: list = field(default_factory=list)
    container_why: list = field(default_factory=list)
    video_idx: int = 0
    audio_idx: int | None = None
    audio_ch: int = 2
    audio_out_kbps: int = 0
    deinterlace: bool = False
    scale: bool = False
    fps: float = 24.0

    @property
    def changes(self) -> bool:
        return self.video == "encode" or self.audio == "encode" or self.remux

    @property
    def mode(self) -> str:
        if not self.changes:
            return "as-is"
        return "re-encode" if "encode" in (self.video, self.audio) else "remux"

    @property
    def steps(self) -> list:
        out = list(self.container_why)
        if self.video == "encode":
            out.append("re-encode video: " + ", ".join(self.video_why))
        if self.audio == "encode":
            out.append("convert audio to " + {1: "mono AAC", 2: "stereo AAC"}.get(self.audio_ch, "AAC"))
        return out


@dataclass
class Analysis:
    checks: list
    plan: Plan
    max_mbps: float
    peak: float
    avg: float
    audio: dict | None


def norm_lang(code: str | None) -> str:
    code = (code or "").lower()
    return LANG_ALIASES.get(code, code)


def pick_audio(audios: list, lang: str | None) -> dict | None:
    if not audios:
        return None
    if lang:
        wanted = norm_lang(lang)
        for a in audios:
            if norm_lang(a.get("tags", {}).get("language")) == wanted:
                return a
    for a in audios:
        if a.get("disposition", {}).get("default"):
            return a
    return audios[0]


def analyze(sc: Scan, max_mbps: float = DEFAULT_MAX_MBPS, force_h264: bool = False,
            lang: str | None = None, audio_index: int | None = None, for_vlc: bool = False) -> Analysis:
    """What the file needs. `for_vlc` adds VLC's own audio limits (see VLC_AUDIO); the default is for a TV that
    plays the file itself, which the native cast does."""
    path, info, stats, duration = sc.path, sc.info, sc.stats, sc.duration
    v, audios, subs = sc.video, sc.audios, sc.subtitles
    a = next((x for x in audios if x["index"] == audio_index), None) if audio_index is not None \
        else pick_audio(audios, lang)

    plan = Plan(video_idx=v["index"], audio_idx=a["index"] if a else None, fps=fps_of(v) or 24.0)
    checks: list[Check] = []
    ext = path.suffix.lower()
    fmts = info["format"].get("format_name", "").split(",")
    is_mp4 = "mp4" in fmts and ext in (".mp4", ".m4v")
    is_webm = ext == ".webm" and "webm" in fmts

    # --- container
    if is_mp4:
        if mp4_is_faststart(path):
            checks.append(Check("Container", True, "MP4, index at the start (starts playing instantly)"))
        else:
            checks.append(Check("Container", False, "MP4 but index is at the end: must download everything before playing"))
            plan.remux = True
            plan.container_why.append("move index to the front (faststart)")
    elif is_webm:
        checks.append(Check("Container", True, "WebM"))
    else:
        label = "MKV" if ext == ".mkv" else ext.lstrip(".").upper() or "unknown"
        checks.append(Check("Container", False, f"{label} is not supported by Chrome/Cast (needs MP4)"))
        plan.remux = True
        plan.container_why.append(f"{label} → MP4")

    # --- video codec
    codec, pix = v.get("codec_name", "unknown"), v.get("pix_fmt", "")  # ffprobe omits the name of a codec it does not know
    level = int(v.get("level", 0) or 0)
    level = 0 if level < 0 else level
    hevc_ok = sys.platform == "darwin" and (sc.chrome is None or sc.chrome >= CHROME_MIN_HEVC)
    why: list[str] = []
    if codec == "h264":
        if v.get("profile") not in H264_PROFILES:
            why.append(f"H.264 {v.get('profile')} profile has no hardware decoding in Chrome/Cast")
        if pix != "yuv420p":
            why.append(f"{pix} (needs 8-bit 4:2:0)")
        if level > H264_MAX_LEVEL:
            why.append(f"level {level / 10:.1f} is above 4.2")
        detail = f"H.264 {v.get('profile')} L{level / 10:.1f}, {pix}"
    elif codec == "hevc":
        detail = f"HEVC {v.get('profile')}, {pix}"
        if force_h264:
            why.append("H.264 requested (older Chromecast)")
        elif not hevc_ok:
            why.append("cannot confirm Chrome hardware HEVC decoding on this machine")
        elif pix not in HEVC_PIX_FMTS:
            why.append(f"{pix} is not decodable in Chrome")
        elif level > HEVC_MAX_LEVEL:
            why.append("HEVC level above 5.1")
    elif codec in ("vp8", "vp9") and is_webm:
        detail = codec.upper()
    else:
        detail = codec
        why.append(f"{codec} is not supported by Chrome/Cast")
    checks.append(Check("Video codec", not why, detail if not why else f"{detail}: {'; '.join(why)}"))
    plan.video_why += why

    # --- resolution / frame rate / scan / HDR
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if w > MAX_W or h > MAX_H:
        checks.append(Check("Resolution", False, f"{w}×{h} exceeds 1920×1080 (Chrome casts at 1080p max)"))
        plan.video_why.append("downscale to 1080p")
        plan.scale = True
    else:
        checks.append(Check("Resolution", True, f"{w}×{h}"))
    if plan.fps > MAX_FPS:
        checks.append(Check("Frame rate", None, f"{plan.fps:.2f} fps: Chrome casts at 30 fps by default, expect dropped frames"))
    else:
        checks.append(Check("Frame rate", True, f"{plan.fps:.3f} fps"))
    if v.get("field_order", "progressive") not in ("progressive", "unknown"):
        checks.append(Check("Scan type", False, f"interlaced ({v['field_order']}): needs deinterlacing"))
        plan.video_why.append("deinterlace")
        plan.deinterlace = True
    if v.get("color_transfer") in HDR_TRANSFERS:
        checks.append(Check("HDR", None, "HDR source: colors will look washed out when cast (tone-mapping is not implemented)"))

    # --- audio
    if a is None:
        plan.audio = "none"
        checks.append(Check("Audio", None, "no audio track"))
    else:
        acodec, ach = a.get("codec_name", "unknown"), int(a.get("channels", 2) or 2)
        lang_tag = a.get("tags", {}).get("language", "und")
        allowed = WEBM_AUDIO if is_webm else VLC_AUDIO if for_vlc else MP4_AUDIO
        awhy: list[str] = []
        if acodec not in allowed:
            awhy.append(f"{acodec.upper()} would be converted live while casting" if for_vlc
                        else f"{acodec.upper()} is not playable in Chrome here" if acodec in ("ac3", "eac3", "dts", "truehd")
                        else f"{acodec} is not supported")
        elif acodec == "aac" and a.get("profile") not in AAC_PROFILES:
            awhy.append(f"AAC {a.get('profile')} is not supported")
        elif for_vlc and acodec == "aac" and ach > VLC_MAX_CH:
            awhy.append(f"{ach} channels would be downmixed live while casting")
        if ach > 6:
            awhy.append(f"{ach} channels (max 5.1)")
        layout = a.get("channel_layout", f"{ach}ch")
        checks.append(Check("Audio", not awhy,
                            f"{acodec.upper()} {layout} [{lang_tag}]" + ("" if not awhy else f": {'; '.join(awhy)}")))
        plan.audio_ch = min(ach, VLC_MAX_CH if for_vlc else 6)  # what a re-encode produces
        if awhy:
            plan.audio, plan.audio_why = "encode", awhy
            plan.audio_out_kbps = 384 if plan.audio_ch > 2 else 192 if plan.audio_ch == 2 else 96
        else:
            astats = stats.get(a["index"])
            plan.audio_out_kbps = round(astats.avg_bps(duration) / 1000) if astats else 192
        if len(audios) > 1:
            others = ", ".join(f"#{x['index']} {x.get('tags', {}).get('language', 'und')}" for x in audios if x is not a)
            checks.append(Check("Audio tracks", None, f"{len(audios)} found, using #{a['index']} [{lang_tag}] (ignored: {others})"))
            # the TV (and VLC) play the file's default track, so the pick only holds if the other tracks leave the file
            plan.remux = True
            plan.container_why.append(f"keep only audio track #{a['index']}")

    # --- bitrate
    audio_bps = plan.audio_out_kbps * 1000 if a else 0  # measured when copied, target when re-encoded
    peak = sc.video_peak_bps() + audio_bps
    avg = sc.video_avg_bps() + audio_bps
    cap = max_mbps * 1e6
    if peak <= cap * PEAK_TOLERANCE:
        limit = "no cap" if max_mbps >= NO_CAP_MBPS else f"≤ {max_mbps:g} Mbit/s ({WINDOW_S} s window)"
        checks.append(Check("Bitrate", True, f"avg {avg / 1e6:.1f} Mbit/s, peak {peak / 1e6:.1f} Mbit/s {limit}"))
    else:
        checks.append(Check("Bitrate", False, f"peak {peak / 1e6:.1f} Mbit/s > {max_mbps:g} Mbit/s: too much for this Wi-Fi"))
        plan.video_why.append(f"cap bitrate to {max_mbps:g} Mbit/s")

    # --- subtitles
    if subs:
        text = [s for s in subs if s.get("codec_name") in TEXT_SUBS]
        bitmap = [s for s in subs if s.get("codec_name") in BITMAP_SUBS]
        msg = f"{len(text)} text track(s) → .vtt sidecar files"
        if bitmap:
            msg += f"; {len(bitmap)} image-based track(s) cannot be converted"
        checks.append(Check("Subtitles", None, msg))

    # --- finalize plan
    plan.video = "encode" if plan.video_why else "copy"
    if codec in ("vp8", "vp9") and plan.changes and plan.video == "copy":
        plan.video, plan.video_why = "encode", ["repackaging VP9 as MP4: encode to H.264"]
    if plan.video == "encode":
        plan.remux = True
    if a is not None and plan.audio == "copy" and plan.changes and acodec not in (VLC_AUDIO if for_vlc else MP4_AUDIO):
        # a rewritten file is always MP4, so audio that only WebM allows (Vorbis) has to be converted
        plan.audio, plan.audio_why = "encode", [f"{acodec} is not supported in MP4"]
        plan.audio_out_kbps = 384 if plan.audio_ch > 2 else 192 if plan.audio_ch == 2 else 96
    if plan.audio == "encode":
        plan.remux = True
    if codec == "hevc" and plan.video == "copy" and v.get("codec_tag_string") == "hev1":
        plan.remux = True
        plan.container_why.append("retag HEVC as hvc1 so Chrome/Apple decoders accept it")
    return Analysis(checks, plan, max_mbps, peak, avg, a)


# ---------------------------------------------------------------- doing the work

def output_path(src: Path) -> Path:
    """<name>.cast.mp4 next to the input. Always differs from the input, which has a single extension."""
    return src.with_name(f"{src.stem}.cast.mp4")


def _video_args(plan: Plan, v: dict, max_mbps: float) -> list:
    if plan.video == "copy":
        return ["-c:v", "copy"] + (["-tag:v", "hvc1"] if v["codec_name"] == "hevc" else [])
    # VBV window = 1 s, so any 10 s window carries at most 1.1x maxrate; 0.9 keeps that under the cap
    vmax = max(500, int(min(max_mbps, 100) * 1000 * 0.9 - plan.audio_out_kbps))
    vf = []
    if plan.deinterlace:
        vf.append("yadif")
    if plan.scale:
        vf.append(f"scale=w={MAX_W}:h={MAX_H}:force_original_aspect_ratio=decrease:force_divisible_by=2")
    elif int(v.get("width", 0)) % 2 or int(v.get("height", 0)) % 2:
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # libx264 with yuv420p refuses odd dimensions
    args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", f"{vmax}k", "-bufsize", f"{vmax}k",
            "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p"]
    return args + (["-vf", ",".join(vf)] if vf else [])


def _audio_args(plan: Plan) -> list:
    if plan.audio == "copy":
        return ["-c:a", "copy"]
    if plan.audio == "encode":
        return ["-c:a", "aac", "-b:a", f"{plan.audio_out_kbps}k", "-ac", str(plan.audio_ch), "-ar", "48000"]
    return []


def build_cmd(src: Path, dst: Path, plan: Plan, info: dict, max_mbps: float) -> list:
    v = next(s for s in info["streams"] if s["index"] == plan.video_idx)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats",
           "-y", "-i", str(src), "-map", f"0:{plan.video_idx}"]
    if plan.audio_idx is not None:
        cmd += ["-map", f"0:{plan.audio_idx}"]
    cmd += ["-sn", "-dn"] + _video_args(plan, v, max_mbps) + _audio_args(plan)
    cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", "-f", "mp4", str(dst)]
    return cmd


# ---------------------------------------------------------------- cast on the fly (HLS segments)

TS_AUDIO = {"aac", "mp3"}  # what an MPEG-TS segment carries and the TV plays (opus and flac only fit MP4)
STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(50), Fraction(60000, 1001), Fraction(60)]
TS_REORDER_FRAMES = 2  # x264 veryfast delays the first video timestamp by this many frames (see build_hls_cmd)


def snap_rate(fps: float) -> Fraction:
    """The standard frame rate nearest to `fps` when it is within 0.1 % of one (variable-rate sources report averages
    such as 23.9761), otherwise `fps` as a plain fraction."""
    best = min(STANDARD_RATES, key=lambda r: abs(float(r) - fps))
    if abs(float(best) - fps) <= float(best) * 0.001:
        return best
    return Fraction(fps).limit_denominator(1001)


def cfr_rate(stream: dict) -> Fraction:
    """The constant frame rate the segments are cut at: the source's."""
    return snap_rate(fps_of(stream) or 24.0)


def needs_audio_pass(plan: Plan, info: dict) -> bool:
    """On the fly only the video is encoded live: audio that must be converted, or that TS cannot carry, is done first."""
    if plan.audio in ("none", None) or plan.audio_idx is None:
        return False
    if plan.audio == "encode":
        return True
    a = next((s for s in info["streams"] if s["index"] == plan.audio_idx), {})
    return a.get("codec_name") not in TS_AUDIO


def audio_out_kbps(channels: int) -> int:
    return 384 if channels > 2 else 192 if channels == 2 else 96


def build_audio_cmd(src: Path, dst: Path, plan: Plan, info: dict) -> list:
    """The audio track alone, as AAC, in a Matroska file. `-copyts` keeps its timestamps where the movie has them, so
    the video pass can read it as a second input without the two drifting apart."""
    a = next(s for s in info["streams"] if s["index"] == plan.audio_idx)
    channels = min(int(a.get("channels", 2) or 2), 6)
    return ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats", "-y",
            "-i", str(src), "-map", f"0:{plan.audio_idx}", "-vn", "-sn", "-dn", "-copyts",
            "-c:a", "aac", "-b:a", f"{audio_out_kbps(channels)}k", "-ac", str(channels), "-ar", "48000",
            "-f", "matroska", str(dst)]


def build_hls_cmd(src: Path, audio_src: Path | None, outdir: Path, plan: Plan, info: dict, max_mbps: float,
                  fps: Fraction, seg_frames: int, start_index: int = 0) -> list:
    """Encode the video into `seg%05d.ts` segments of exactly `seg_frames` frames, numbered from `start_index`.

    The GOP is a fixed frame count, not a duration: that makes every segment last exactly seg_frames/fps, so the
    playlist the TV gets can state the lengths before they exist. Segment i then starts at i * seg_frames / fps on the
    movie's clock. A run that starts mid-movie seeks both inputs there and shifts its timestamps by the same amount
    (plus the reorder delay the first run has naturally), so segments from different runs join without a gap.
    `audio_src` is the audio pass's output; without it the chosen track is copied from `src` (TS-compatible codecs only)."""
    v = next(s for s in info["streams"] if s["index"] == plan.video_idx)
    at = start_index * seg_frames / fps
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats", "-y"]
    if start_index:
        cmd += ["-ss", f"{float(at):.6f}"]
    cmd += ["-i", str(src)]
    if audio_src is not None:
        if start_index:
            cmd += ["-ss", f"{float(at):.6f}"]
        cmd += ["-i", str(audio_src)]
    cmd += ["-map", f"0:{plan.video_idx}"]
    if audio_src is not None:
        cmd += ["-map", "1:0"]
    elif plan.audio_idx is not None:
        cmd += ["-map", f"0:{plan.audio_idx}"]
    cmd += ["-sn", "-dn"] + _video_args(plan, v, max_mbps)
    cmd += ["-r", f"{fps.numerator}/{fps.denominator}", "-fps_mode", "cfr",
            "-g", str(seg_frames), "-keyint_min", str(seg_frames), "-sc_threshold", "0"]
    if plan.audio_idx is not None:
        cmd += ["-c:a", "copy", "-copypriorss", "0"]  # without it a seeked copy starts at the previous packet cluster
    cmd += ["-muxdelay", "0", "-muxpreload", "0"]
    if start_index:
        cmd += ["-output_ts_offset", f"{float(at + TS_REORDER_FRAMES / fps):.6f}"]
    cmd += ["-f", "hls", "-hls_time", str(SEG_SECONDS), "-hls_list_size", "0", "-hls_segment_type", "mpegts",
            "-hls_flags", "temp_file+independent_segments", "-start_number", str(start_index),
            "-hls_segment_filename", str(outdir / "seg%05d.ts"), str(outdir / "ffmpeg.m3u8")]
    return cmd


def run_ffmpeg(cmd: list, duration: float, label: str, progress: ProgressFactory = NullProgress) -> None:
    bar = progress(label, duration)
    with tempfile.TemporaryFile(mode="w+") as errlog, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog, text=True, bufsize=1) as proc:
        try:
            for line in proc.stdout:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit():
                    bar.update(int(val) / 1e6)
                elif key == "total_size" and val.isdigit():
                    bar.extra = fmt_size(int(val))
        except BaseException:
            proc.terminate()
            bar.abort()
            raise
        proc.wait()
        if proc.returncode:
            errlog.seek(0)
            tail = "\n".join(errlog.read().strip().splitlines()[-12:])
            bar.abort()
            raise CastError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    bar.finish()


def export_subtitles(src: Path, base: Path, subs: list, lang: str | None = None) -> list:
    """Write text subtitle tracks as <base>.<lang>.vtt next to the video."""
    made, used = [], collections.Counter()
    for s in subs:
        if s.get("codec_name") not in TEXT_SUBS:
            continue
        code = s.get("tags", {}).get("language", "und")
        if lang and norm_lang(code) != norm_lang(lang):
            continue
        used[code] += 1
        suffix = code if used[code] == 1 else f"{code}{used[code]}"
        out = base.with_name(f"{base.stem}.{suffix}.vtt")
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(src),
                            "-map", f"0:{s['index']}", "-c:s", "webvtt", str(out)], capture_output=True, text=True)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
            made.append(out)
        else:
            out.unlink(missing_ok=True)
    return made


def decode_test(path: Path, duration: float) -> tuple[bool, float, str]:
    """Software-decode a few slices; catches corruption and shows decode headroom."""
    spans = [(0, duration)] if duration < 45 else [(duration * f, 10) for f in (0.1, 0.5, 0.9)]
    worst = float("inf")
    for start, length in spans:
        t0 = time.monotonic()
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}",
                            "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
        elapsed = max(time.monotonic() - t0, 1e-3)
        if p.returncode or p.stderr.strip():
            return False, 0.0, p.stderr.strip().splitlines()[0] if p.stderr.strip() else "decode failed"
        worst = min(worst, min(length, duration) / elapsed)
    return True, worst, ""


def cumulative_curve(stats: dict, duration: float) -> list:
    """Cumulative bytes of all streams per second of media: curve[t] = bytes needed to play up to second t."""
    n = int(duration) + 2
    curve, total = [0], 0
    for i in range(n):
        total += sum(s.bins[i] if i < len(s.bins) else 0 for s in stats.values())
        curve.append(total)
    return curve


@dataclass
class Verification:
    problems: list
    verdict: str   # e.g. "peak 4.5 Mbit/s (limit 6)"
    decode_speed: float  # x real-time
    curve: list | None = None  # cumulative bitrate curve of the verified file

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(out: Path, src_duration: float, wants_audio: bool, max_mbps: float,
           progress: ProgressFactory = NullProgress) -> Verification:
    """Re-probe the finished file: duration, streams, index position, peak bitrate, test decode."""
    info = probe(out)
    problems = []
    got = float(info["format"].get("duration") or 0)
    if abs(got - src_duration) > max(1.0, src_duration * 0.003):
        problems.append(f"duration {fmt_dur(got)} differs from source {fmt_dur(src_duration)}")
    kinds = [s["codec_type"] for s in info["streams"]]
    if "video" not in kinds:
        problems.append("output has no video")
    if wants_audio and "audio" not in kinds:
        problems.append("output has no audio")
    if not mp4_is_faststart(out):
        problems.append("index is not at the start of the file")
    stats = scan_bitrates(out, got, "Verifying", progress)
    kind = {s["index"]: s["codec_type"] for s in info["streams"]}
    peak = max((s.peak_bps() for i, s in stats.items() if kind.get(i) == "video"), default=0) \
        + sum(s.avg_bps(got) for i, s in stats.items() if kind.get(i) == "audio")
    limit = "no cap" if max_mbps >= NO_CAP_MBPS else f"limit {max_mbps:g}"
    verdict = f"peak {peak / 1e6:.1f} Mbit/s ({limit})"
    if peak > max_mbps * 1e6 * PEAK_TOLERANCE:
        problems.append(verdict + ": above the limit")
    ok, speed, err = decode_test(out, got)
    if not ok:
        problems.append(f"decode test failed: {err}")
    return Verification(problems, verdict, speed, cumulative_curve(stats, got))


@dataclass
class Outcome:
    video: Path                       # the file to play
    converted: bool                   # False when the input was already fine or an existing output was reused
    verification: Verification | None
    subtitles: list
    seconds: float
    curve: list | None = None         # cumulative bitrate curve of `video`, for the live buffer estimate
    duration: float = 0.0             # length of the movie in seconds


def _finish(sc: Scan, an: Analysis, video: Path, converted: bool, started: float,
            progress: ProgressFactory) -> Outcome:
    ver = None
    if video != sc.path:
        ver = verify(video, sc.duration, an.plan.audio != "none", an.max_mbps, progress)
    curve = ver.curve if ver else cumulative_curve(sc.stats, sc.duration)
    subs = export_subtitles(sc.path, video, sc.subtitles)
    return Outcome(video, converted, ver, subs, time.monotonic() - started, curve, sc.duration)


def convert(sc: Scan, an: Analysis, progress: ProgressFactory = NullProgress, overwrite: bool = False) -> Outcome:
    """Do the minimum the plan asks for, then verify. Raises OutputExists unless overwrite is set."""
    started = time.monotonic()
    plan = an.plan
    if not plan.changes:
        return _finish(sc, an, sc.path, False, started, progress)
    dst = output_path(sc.path)
    if dst.exists() and not overwrite:
        raise OutputExists(dst)
    need = sc.path.stat().st_size if plan.video == "copy" else sc.duration * min(an.max_mbps, 40) * 1e6 / 8 * 1.1
    free = shutil.disk_usage(dst.parent).free
    if free < need * 1.05:
        raise CastError(f"not enough disk space: need ~{fmt_size(need)}, have {fmt_size(free)}")
    part = dst.with_name(dst.name + ".part")
    try:
        run_ffmpeg(build_cmd(sc.path, part, plan, sc.info, an.max_mbps), sc.duration,
                   "Encoding" if plan.video == "encode" else "Remuxing", progress)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    return _finish(sc, an, dst, True, started, progress)


def reuse_existing(sc: Scan, an: Analysis, progress: ProgressFactory = NullProgress) -> Outcome:
    """Use a previously converted output after checking it is still sound."""
    return _finish(sc, an, output_path(sc.path), False, time.monotonic(), progress)


# ---------------------------------------------------------------- formatting

def fmt_dur(seconds: float) -> str:
    h, rem = divmod(int(max(0, seconds)) if math.isfinite(seconds) else 0, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(nbytes: float) -> str:
    return f"{nbytes / 1e9:.2f} GB" if nbytes >= 1e9 else f"{nbytes / 1e6:.0f} MB"
