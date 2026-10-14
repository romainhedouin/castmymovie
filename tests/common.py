"""Shared fixtures: tiny synthetic clips generated once per test run with ffmpeg."""

import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_dir = None


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def video_src(size="640x360", dur=6):
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={dur}"]


def audio_src(freq=440, dur=6):
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}"]


H264 = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-b:v", "1M"]
AAC = ["-c:a", "aac", "-b:a", "128k"]


def fixtures() -> Path:
    global _dir
    if _dir:
        return _dir
    _dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-"))
    atexit.register(shutil.rmtree, _dir, True)
    d, src = _dir, video_src() + audio_src()
    ff(*src, *H264, *AAC, "-movflags", "+faststart", str(d / "good.mp4"))
    ff(*src, *H264, *AAC, str(d / "nofaststart.mp4"))
    ff(*src, *H264, *AAC, str(d / "plain.mkv"))
    ff(*src, "-c:v", "libx265", "-x265-params", "log-level=error", "-pix_fmt", "yuv420p", "-crf", "28",
       "-c:a", "ac3", "-ac", "6", "-b:a", "384k", str(d / "hevc_ac3.mkv"))
    ff(*src, "-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le", "-crf", "28", *AAC, str(d / "hi10.mkv"))
    ff(*video_src("3840x2160", 3), *audio_src(dur=3), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
       "-pix_fmt", "yuv420p", *AAC, str(d / "uhd.mkv"))
    ff(*video_src("1280x720", 8), *audio_src(dur=8), "-vf", "noise=alls=90:allf=t", "-c:v", "libx264", "-preset",
       "veryfast", "-crf", "8", "-pix_fmt", "yuv420p", *AAC, str(d / "burst.mkv"))
    ff(*src, "-vf", "interlace=scan=tff", "-c:v", "libx264", "-flags", "+ilme+ildct", "-pix_fmt", "yuv420p",
       "-b:v", "1M", *AAC, str(d / "interlaced.mkv"))
    ff(*src, "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-crf", "30", "-x265-params",
       "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", *AAC, str(d / "hdr.mkv"))
    (d / "subs.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n")
    ff(*src, "-i", str(d / "subs.srt"), "-map", "0:v", "-map", "1:a", "-map", "2:s", *H264, *AAC, "-c:s", "srt",
       "-metadata:s:s:0", "language=eng", str(d / "subs.mkv"))
    ff(*video_src(), *audio_src(440), *audio_src(880), "-map", "0:v", "-map", "1:a", "-map", "2:a", *H264, *AAC,
       "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fra", str(d / "dual.mkv"))
    # already compatible (H.264/AAC, index first), but with two audio tracks
    ff(*video_src(), *audio_src(440), *audio_src(880), "-map", "0:v", "-map", "1:a", "-map", "2:a", *H264, *AAC,
       "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fra", "-movflags", "+faststart",
       str(d / "dual_aac.mp4"))
    # odd dimensions: libx264 cannot encode them as they are
    ff(*video_src("640x480", 3), *audio_src(dur=3), "-vf", "crop=321:241:exact=1", "-c:v", "mpeg4", "-q:v", "4",
       "-pix_fmt", "yuv420p", *AAC, str(d / "odd.avi"))
    # WebM audio (Vorbis) is not legal in the MP4 that a rewrite produces
    ff(*video_src("320x180", 3), *audio_src(dur=3), "-c:v", "libvpx-vp9", "-b:v", "300k", "-c:a", "vorbis",
       "-strict", "-2", "-ac", "2", str(d / "vorbis.webm"))
    return d
