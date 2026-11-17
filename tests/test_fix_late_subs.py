"""Two subtitle bugs found while fixing others: a cue that runs on past the movie made a good conversion fail
verification (the expected length counted the subtitle stream, which the .cast.mp4 doesn't carry), and a language tag
holding a "/" (an MKV can say "en/us") crashed the export with a ValueError from Path.with_name."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import engine  # noqa: E402
from common import AAC, H264, HAVE_FFMPEG, audio_src, ff, video_src  # noqa: E402


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class LateSubtitleCue(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="castmymovie-latesub-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        srt = self.d / "late.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nearly\n\n2\n00:00:14,000 --> 00:00:15,000\nlate\n\n")
        # a 4 s movie in MKV (so it needs a remux) whose subtitle track ends at 15 s
        self.movie = self.d / "Movie.mkv"
        ff(*video_src("320x180", 4), *audio_src(dur=4), "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s",
           *H264, *AAC, "-c:s", "srt", "-metadata:s:s:0", "language=eng", str(self.movie))

    def test_the_conversion_verifies(self):
        sc = engine.scan(self.movie)
        self.assertGreater(sc.duration, 10, "the container counts the subtitle")
        self.assertAlmostEqual(sc.video_duration, 4.0, delta=0.1)
        out = engine.convert(sc, engine.analyze(sc))
        self.assertTrue(out.converted)
        self.assertEqual(out.verification.problems, [])


class OddLanguageTag(unittest.TestCase):
    def test_a_slash_in_the_tag_names_a_plain_file(self):
        d = Path(tempfile.mkdtemp(prefix="castmymovie-langtag-"))
        self.addCleanup(shutil.rmtree, d, True)
        subs = [{"index": 2, "codec_name": "subrip", "tags": {"language": "en/us"}},
                {"index": 3, "codec_name": "subrip", "tags": {"language": "../.."}}]

        def write(cmd, **kw):
            Path(cmd[-1]).write_text("WEBVTT\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(engine.subprocess, "run", side_effect=write):
            made = engine.export_subtitles(d / "Movie.mkv", engine.output_path(d / "Movie.mkv"), subs)
        self.assertEqual([p.name for p in made], ["Movie.cast.en-us.vtt", "Movie.cast.und.vtt"])
        self.assertTrue(all(p.parent == d for p in made))


if __name__ == "__main__":
    unittest.main()
