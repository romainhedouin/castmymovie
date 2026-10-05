import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import finder  # noqa: E402


def make(path: Path, mb: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(mb * 1_000_000)  # sparse: instant


class Finder(unittest.TestCase):
    def test_finds_movies_and_skips_the_noise(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make(root / "a.mkv", 60)
            make(root / "sub" / "b.MP4", 60)
            make(root / "small.mkv", 1)                      # too small
            make(root / "a.cast.mp4", 60)                    # our own output
            make(root / "notes.txt", 60)                     # not a video
            make(root / "node_modules" / "x.mkv", 60)        # skipped folder
            make(root / ".hidden" / "y.mkv", 60)             # hidden folder
            make(root / "1" / "2" / "3" / "4" / "5" / "deep.mkv", 60)  # beyond depth 4
            names = sorted(f.path.name for f in finder.find_videos([root]))
            self.assertEqual(names, ["a.mkv", "b.MP4"])

    def test_newest_first_and_limit(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            import os
            for i in range(5):
                make(root / f"m{i}.mkv", 60)
                os.utime(root / f"m{i}.mkv", (1_000_000 + i, 1_000_000 + i))
            got = [f.path.name for f in finder.find_videos([root], limit=3)]
            self.assertEqual(got, ["m4.mkv", "m3.mkv", "m2.mkv"])

    def test_missing_root_is_ignored(self):
        self.assertEqual(finder.find_videos(["/definitely/not/here"]), [])

    def test_dropped_paths(self):
        self.assertEqual(finder.clean_dropped_path("'/a b/c.mkv'"), Path("/a b/c.mkv"))
        self.assertEqual(finder.clean_dropped_path('"/a b/c.mkv"  '), Path("/a b/c.mkv"))
        self.assertEqual(finder.clean_dropped_path(r"/a\ b/Jackie\ Brown\ \(1997\).mp4"),
                         Path("/a b/Jackie Brown (1997).mp4"))
        self.assertEqual(finder.clean_dropped_path("~/x.mkv"), Path.home() / "x.mkv")


if __name__ == "__main__":
    unittest.main()
