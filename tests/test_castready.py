"""Tests for castready. Fixtures are tiny synthetic clips generated with ffmpeg."""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import castready  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
TMP: Path


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def video_src(size="640x360", dur=6):
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={dur}"]


def audio_src(freq=440, dur=6):
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}"]


H264 = ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-b:v", "1M"]
AAC = ["-c:a", "aac", "-b:a", "128k"]


def setUpModule():
    global TMP
    if not HAVE_FFMPEG:
        return
    TMP = Path(tempfile.mkdtemp(prefix="castready-test-"))
    src = video_src() + audio_src()
    ff(*src, *H264, *AAC, "-movflags", "+faststart", str(TMP / "good.mp4"))
    ff(*src, *H264, *AAC, str(TMP / "nofaststart.mp4"))
    ff(*src, *H264, *AAC, str(TMP / "plain.mkv"))
    ff(*src, "-c:v", "libx265", "-x265-params", "log-level=error", "-pix_fmt", "yuv420p", "-crf", "28",
       "-c:a", "ac3", "-ac", "6", "-b:a", "384k", str(TMP / "hevc_ac3.mkv"))
    ff(*src, "-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le", "-crf", "28", *AAC,
       str(TMP / "hi10.mkv"))
    ff(*video_src("3840x2160", 3), *audio_src(dur=3), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
       "-pix_fmt", "yuv420p", *AAC, str(TMP / "uhd.mkv"))
    ff(*video_src("1280x720", 8), *audio_src(dur=8), "-vf", "noise=alls=90:allf=t", "-c:v", "libx264", "-preset", "veryfast",
       "-crf", "8", "-pix_fmt", "yuv420p", *AAC, str(TMP / "burst.mkv"))
    ff(*src, "-vf", "interlace=scan=tff", "-c:v", "libx264", "-flags", "+ilme+ildct", "-pix_fmt", "yuv420p", "-b:v", "1M",
       *AAC, str(TMP / "interlaced.mkv"))
    ff(*src, "-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-crf", "30", "-x265-params",
       "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", *AAC, str(TMP / "hdr.mkv"))
    (TMP / "subs.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n")
    ff(*src, "-i", str(TMP / "subs.srt"), "-map", "0:v", "-map", "1:a", "-map", "2:s", *H264, *AAC, "-c:s", "srt",
       "-metadata:s:s:0", "language=eng", str(TMP / "subs.mkv"))
    ff(*video_src(), *audio_src(440), *audio_src(880), "-map", "0:v", "-map", "1:a", "-map", "2:a", *H264, *AAC,
       "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=fra", str(TMP / "dual.mkv"))


def tearDownModule():
    if HAVE_FFMPEG:
        shutil.rmtree(TMP, ignore_errors=True)


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = castready.main([str(a) for a in args])
    return code, out.getvalue() + err.getvalue()


def streams(path, kind):
    return [s for s in castready.probe(path)["streams"] if s["codec_type"] == kind]


class PureLogic(unittest.TestCase):
    def test_peak_uses_worst_window(self):
        s = castready.StreamStats()
        for sec in range(30):
            s.add(sec, 1_000_000 if 10 <= sec < 20 else 100_000)
        self.assertAlmostEqual(s.peak_bps(10), 8_000_000)
        self.assertAlmostEqual(s.avg_bps(30), (10 * 1_000_000 + 20 * 100_000) * 8 / 30)

    def test_peak_short_clip_uses_whole_clip(self):
        s = castready.StreamStats()
        for sec in range(4):
            s.add(sec, 500_000)
        self.assertAlmostEqual(s.peak_bps(10), 4_000_000)

    def test_language_matching(self):
        audios = [{"index": 1, "tags": {"language": "eng"}}, {"index": 2, "tags": {"language": "fra"}}]
        self.assertEqual(castready.pick_audio(audios, "fr")["index"], 2)
        self.assertEqual(castready.pick_audio(audios, "fre")["index"], 2)
        self.assertEqual(castready.pick_audio(audios, "de")["index"], 1)  # falls back to first
        self.assertIsNone(castready.pick_audio([], "en"))

    def test_progress_eta_from_steady_rate(self):
        p = castready.Progress("x", 100)
        p.start = castready.time.monotonic() - 10
        now = castready.time.monotonic()
        p.samples.extend([(now - 10, 0), (now, 25)])
        self.assertAlmostEqual(p._eta(now, 25), 30, delta=0.5)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class EndToEnd(unittest.TestCase):
    def tearDown(self):  # keep tests independent: drop everything castready generated
        for f in [*TMP.glob("*.cast.*"), *TMP.glob("*.v2.*"), TMP / "twice.mkv", TMP / "broken.mkv"]:
            f.unlink(missing_ok=True)

    def test_good_file_is_left_alone(self):
        code, out = run("--check", TMP / "good.mp4")
        self.assertEqual(code, 0, out)
        code, out = run(TMP / "good.mp4")
        self.assertEqual(code, 0, out)
        self.assertIn("Ready as-is", out)
        self.assertFalse((TMP / "good.cast.mp4").exists())

    def test_missing_faststart_triggers_remux(self):
        self.assertFalse(castready.mp4_is_faststart(TMP / "nofaststart.mp4"))
        code, out = run(TMP / "nofaststart.mp4")
        self.assertEqual(code, 0, out)
        self.assertIn("Plan: remux", out)
        self.assertTrue(castready.mp4_is_faststart(TMP / "nofaststart.cast.mp4"))

    def test_mkv_remux_copies_streams(self):
        code, out = run("--check", TMP / "plain.mkv")
        self.assertEqual(code, 2, out)
        code, out = run(TMP / "plain.mkv")
        self.assertEqual(code, 0, out)
        self.assertIn("Plan: remux", out)
        dst = TMP / "plain.cast.mp4"
        self.assertEqual(streams(dst, "video")[0]["codec_name"], "h264")
        self.assertEqual(streams(dst, "audio")[0]["codec_name"], "aac")
        self.assertTrue(castready.mp4_is_faststart(dst))
        self.assertIn("Verified", out)

    @unittest.skipUnless(sys.platform == "darwin", "HEVC is only trusted on macOS")
    def test_hevc_kept_audio_converted(self):
        code, out = run(TMP / "hevc_ac3.mkv")
        self.assertEqual(code, 0, out)
        dst = TMP / "hevc_ac3.cast.mp4"
        v, a = streams(dst, "video")[0], streams(dst, "audio")[0]
        self.assertEqual((v["codec_name"], v["codec_tag_string"]), ("hevc", "hvc1"))
        self.assertEqual((a["codec_name"], a["channels"]), ("aac", 6))

    def test_h264_flag_forces_video_encode(self):
        code, out = run("--h264", TMP / "hevc_ac3.mkv", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(streams(TMP / "hevc_ac3.cast.mp4", "video")[0]["codec_name"], "h264")

    def test_ten_bit_h264_becomes_8_bit(self):
        code, out = run(TMP / "hi10.mkv")
        self.assertEqual(code, 0, out)
        v = streams(TMP / "hi10.cast.mp4", "video")[0]
        self.assertEqual((v["codec_name"], v["pix_fmt"]), ("h264", "yuv420p"))

    def test_uhd_is_downscaled_to_1080p(self):
        code, out = run(TMP / "uhd.mkv")
        self.assertEqual(code, 0, out)
        v = streams(TMP / "uhd.cast.mp4", "video")[0]
        self.assertEqual((v["width"], v["height"]), (1920, 1080))

    def test_interlaced_is_deinterlaced(self):
        self.assertIn(streams(TMP / "interlaced.mkv", "video")[0].get("field_order"), ("tt", "bb", "tb", "bt"))
        code, out = run(TMP / "interlaced.mkv")
        self.assertEqual(code, 0, out)
        self.assertIn("deinterlace", out)
        self.assertEqual(streams(TMP / "interlaced.cast.mp4", "video")[0].get("field_order", "progressive"), "progressive")

    def test_hdr_is_flagged(self):
        code, out = run("--check", TMP / "hdr.mkv")
        self.assertIn("HDR", out)

    def test_bitrate_is_capped(self):
        stats = castready.scan_bitrates(TMP / "burst.mkv", 8)
        self.assertGreater(stats[0].peak_bps(), 6e6, "fixture should exceed the cap")
        code, out = run(TMP / "burst.mkv")
        self.assertEqual(code, 0, out)
        self.assertIn("cap bitrate", out)
        dst = TMP / "burst.cast.mp4"
        new = castready.scan_bitrates(dst, 8)
        self.assertLessEqual(new[0].peak_bps(), 6e6 * castready.PEAK_TOLERANCE)

    def test_max_mbps_override(self):
        code, out = run("--check", "--max-mbps", "400", TMP / "burst.mkv")
        self.assertEqual(code, 2)  # still MKV, but bitrate is no longer the problem
        self.assertNotIn("cap bitrate", out)

    def test_subtitles_exported_as_vtt(self):
        code, out = run(TMP / "subs.mkv")
        self.assertEqual(code, 0, out)
        vtt = TMP / "subs.cast.eng.vtt"
        self.assertTrue(vtt.exists(), out)
        self.assertIn("Hello there", vtt.read_text())
        self.assertFalse(streams(TMP / "subs.cast.mp4", "subtitle"))

    def test_language_picks_second_audio_track(self):
        code, out = run("--lang", "fr", TMP / "dual.mkv")
        self.assertEqual(code, 0, out)
        got = streams(TMP / "dual.cast.mp4", "audio")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tags"].get("language"), "fra")

    def test_refuses_to_overwrite_without_force(self):
        shutil.copy(TMP / "plain.mkv", TMP / "twice.mkv")
        self.assertEqual(run(TMP / "twice.mkv")[0], 0)
        code, _ = run(TMP / "twice.mkv")
        self.assertEqual(code, 1)
        self.assertEqual(run("--force", TMP / "twice.mkv")[0], 0)

    def test_failed_encode_leaves_no_partial_file(self):
        bad = TMP / "broken.mkv"
        bad.write_bytes((TMP / "plain.mkv").read_bytes()[:3000])
        code, _ = run(bad)
        self.assertEqual(code, 1)
        self.assertEqual(list(TMP.glob("broken.cast.mp4*")), [])

    def test_missing_file(self):
        self.assertEqual(run(TMP / "nope.mkv")[0], 1)


if __name__ == "__main__":
    unittest.main()
