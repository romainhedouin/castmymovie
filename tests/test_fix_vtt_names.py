"""Subtitle sidecars are always <name>.cast.<lang>.vtt: a movie played as-is used to get <name>.<lang>.vtt, written with
-y over the user's own subtitles of that name. A Ctrl-C midway must not leave a half-written .vtt either."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine  # noqa: E402
from common import H264, HAVE_FFMPEG, ff, video_src  # noqa: E402

MINE = "WEBVTT\n\n00:00.000 --> 00:02.000\nMY OWN HAND-MADE SUBTITLES\n"


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AsIsMovie(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Path(tempfile.mkdtemp(prefix="castmymovie-vtt-"))
        cls.addClassCleanup(shutil.rmtree, cls.src, True)
        srts = []
        for i, line in enumerate(("First English", "Second English", "Bonjour")):
            srts.append(cls.src / f"{i}.srt")
            srts[-1].write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{line}\n\n")
        # H.264/AAC MP4 with the index first: nothing to convert. Two English text tracks and a French one.
        ff(*video_src("320x180", 3), "-f", "lavfi", "-i", "sine=duration=3", *sum((["-i", str(s)] for s in srts), []),
           "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s", "-map", "4:s", *H264, "-c:a", "aac",
           "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=eng",
           "-metadata:s:s:2", "language=fra", "-movflags", "+faststart", str(cls.src / "src.mp4"))

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="castmymovie-vtt-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.movie = self.d / "Movie.mp4"
        shutil.copy(self.src / "src.mp4", self.movie)

    def run_engine(self):
        sc = engine.scan(self.movie)
        an = engine.analyze(sc)
        self.assertFalse(an.plan.changes)  # played as-is
        return engine.convert(sc, an)

    def test_the_users_own_vtt_next_to_the_movie_is_left_alone(self):
        mine = self.d / "Movie.eng.vtt"
        mine.write_text(MINE)
        out = self.run_engine()
        self.assertEqual(out.video, self.movie)
        self.assertEqual(out.subtitles, [self.d / n for n in ("Movie.cast.eng.vtt", "Movie.cast.eng2.vtt",
                                                              "Movie.cast.fra.vtt")])
        self.assertEqual(mine.read_text(), MINE)
        self.assertEqual([p.read_text().count("English") for p in out.subtitles], [1, 1, 0])
        self.assertIn("First English", out.subtitles[0].read_text())
        self.assertIn("Bonjour", out.subtitles[2].read_text())
        self.assertEqual(sorted(p.name for p in self.d.iterdir()),
                         ["Movie.cast.eng.vtt", "Movie.cast.eng2.vtt", "Movie.cast.fra.vtt", "Movie.eng.vtt",
                          "Movie.mp4"])  # and no .part file left behind

    def test_a_second_run_refreshes_the_files_castmymovie_wrote(self):
        ours = self.d / "Movie.cast.eng.vtt"
        ours.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nstale, from an earlier run\n")
        out = self.run_engine()
        self.assertEqual(out.subtitles[0], ours)
        self.assertIn("First English", ours.read_text())
        self.assertNotIn("stale", ours.read_text())


class Interrupted(unittest.TestCase):
    """No ffmpeg needed: subprocess.run is replaced by one that writes half a file to the output, then stops."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="castmymovie-vtt-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.subs = [{"index": 2, "codec_name": "mov_text", "tags": {"language": "eng"}}]
        self.ours = self.d / "Movie.cast.eng.vtt"
        self.ours.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nfrom an earlier run\n")

    def export(self, then):
        def half_written(cmd, **kw):
            Path(cmd[-1]).write_text("WEBVTT\n\n00:00.000 --> 00:0")
            return then(cmd)
        with mock.patch.object(engine.subprocess, "run", side_effect=half_written):
            return engine.export_subtitles(self.d / "Movie.mp4", engine.output_path(self.d / "Movie.mp4"), self.subs)

    def test_ctrl_c_leaves_no_half_written_vtt(self):
        def ctrl_c(cmd):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.export(ctrl_c)
        self.assertEqual(self.ours.read_text(), "WEBVTT\n\n00:00.000 --> 00:02.000\nfrom an earlier run\n")
        self.assertEqual([p.name for p in self.d.iterdir()], ["Movie.cast.eng.vtt"])

    def test_a_failed_export_offers_nothing_and_leaves_no_part_file(self):
        made = self.export(lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "boom"))
        self.assertEqual(made, [])
        self.assertEqual([p.name for p in self.d.iterdir()], ["Movie.cast.eng.vtt"])


if __name__ == "__main__":
    unittest.main()
