"""Cast on the fly stops where the picture does: a movie whose sound (or a subtitle) runs on after the last frame got a
playlist with segments the encoder never writes, since a segment can only begin at a frame."""

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import HAVE_FFMPEG, ff  # noqa: E402
from test_onthefly import info, plan  # noqa: E402

from castmymovie import engine, onthefly  # noqa: E402

NTSC = Fraction(24000, 1001)


def scan_of(duration, video_end=None, start_time="0.000000"):
    """A Scan of a 1-video 1-audio movie of container length `duration` whose video packets end at `video_end`."""
    stats = {}
    if video_end is not None:
        stats[0] = engine.StreamStats()
        stats[0].add(1.0, 1000, video_end)
    fmt = {"duration": str(duration)} | ({"start_time": start_time} if start_time is not None else {})
    return engine.Scan(Path("/m/a.mkv"), {"format": fmt, **info()}, stats, duration)


class PictureLength(unittest.TestCase):
    def test_the_stats_remember_where_the_last_packet_ends(self):
        s = engine.StreamStats()
        s.add(29.96, 100, 30.0)
        s.add(29.88, 100, 29.92)  # B-frames arrive out of order: the end is the latest, not the last seen
        self.assertEqual(s.end, 30.0)

    def test_the_picture_ends_before_sound_that_runs_on(self):
        self.assertEqual(scan_of(40.023, 30.0).video_duration, 30.0)

    def test_it_is_on_the_containers_clock(self):
        self.assertAlmostEqual(scan_of(40.0, 31.5, start_time="1.500000").video_duration, 30.0)

    def test_the_container_length_when_the_packets_tell_nothing(self):
        self.assertEqual(scan_of(40.0).video_duration, 40.0)                    # no packet stats
        self.assertEqual(scan_of(40.0, 0.0).video_duration, 40.0)               # no timestamps
        self.assertEqual(scan_of(40.0, 40.004).video_duration, 40.0)            # rounding: never past the container
        self.assertEqual(scan_of(40.0, 30.0, start_time="N/A").video_duration, 30.0)
        self.assertEqual(scan_of(40.0, 30.0, start_time=None).video_duration, 30.0)


class Commands(unittest.TestCase):
    def test_the_output_stops_where_the_picture_ends(self):
        cmd = engine.build_hls_cmd(Path("/m/a.mkv"), Path("/t/audio.mka"), Path("/t"), plan(), info(), 6.0, NTSC, 144, 0, end=30.0)
        self.assertEqual(cmd[cmd.index("-t") + 1], "30.000000")
        self.assertGreater(cmd.index("-t"), max(i for i, x in enumerate(cmd) if x == "-i"), "an output option, for both inputs")
        cmd = engine.build_hls_cmd(Path("/m/a.mkv"), None, Path("/t"), plan(), info(), 6.0, NTSC, 144, 3, end=30.0)
        self.assertAlmostEqual(float(cmd[cmd.index("-t") + 1]), 30.0 - 18.018, places=5)  # counted from the seek

    def test_without_an_end_nothing_is_cut(self):
        self.assertNotIn("-t", engine.build_hls_cmd(Path("/m/a.mkv"), None, Path("/t"), plan(), info(), 6.0, NTSC, 144))


def packet_span(path: Path, kind: str) -> tuple:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "packet=pts_time,duration_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.split()
    packets = [[float(x) for x in line.strip(",").split(",")] for line in out]
    return min(p for p, _ in packets), max(p + d for p, d in packets)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RealClips(unittest.TestCase):
    """The first encoder run alone (no request, so no restart) writes every segment of the playlist."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="castmymovie-test-tail-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def clip(self, name, video_s, audio_s, codec):
        src = self.dir / name
        ff("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={video_s}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_s}",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "800k", "-ac", "2", "-c:a", codec, "-b:a", "96k", str(src))
        return src

    def test_every_segment_of_the_playlist_is_written(self):
        # 13 s of picture, 20 s of sound: the playlist used to declare 4 segments, the encoder wrote 3.
        # The last two are the common case, an overhang far shorter than a segment: AAC makes the sound 23 ms longer
        # than a picture that ends on a boundary, which was enough for one more segment (16 declared, 15 written at 90 s).
        for name, video_s, audio_s, codec in (("aac.mkv", 13, 20, "aac"), ("opus.mkv", 13, 20, "libopus"),
                                              ("aac.mp4", 13, 20, "aac"), ("even.mkv", 12, 12, "aac"),
                                              ("even90.mkv", 90, 90, "aac")):
            with self.subTest(name):
                sc = engine.scan(self.clip(name, video_s, audio_s, codec))
                self.assertGreater(sc.duration, video_s)
                out = onthefly.prepare(sc, engine.analyze(sc, 0.3))
                s = out.make_stream()
                try:
                    s.begin(0)
                    deadline = time.monotonic() + 60
                    while s._proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertEqual(s._proc.returncode, 0)
                    self.assertEqual(sorted(p.name for p in s.workdir.glob("seg*.ts")),
                                     [s.timeline.segment_name(i) for i in range(s.timeline.count)])
                    self.assertEqual(s.timeline.count, -(-video_s // 6))
                    self.assertAlmostEqual(out.duration, video_s, delta=0.01)
                    self.assertAlmostEqual(sc.video_duration, video_s, delta=0.01)
                    last = s._path(s.timeline.count - 1)
                    _, video_end = packet_span(last, "v")
                    audio_start, audio_end = packet_span(last, "a")
                    self.assertAlmostEqual(audio_end, video_end, delta=0.1, msg="the sound stops with the picture")
                    self.assertLess(audio_start, video_end)
                finally:
                    s.close()
                    out.cleanup()


if __name__ == "__main__":
    unittest.main()
