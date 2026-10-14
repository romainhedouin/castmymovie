"""Find movie files on this computer for the file picker."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".m2ts", ".flv", ".mpg", ".mpeg"}
SKIP_DIRS = {"node_modules", "Library", "Applications", ".git", ".Trash", "venv", ".venv", "__pycache__", "site-packages"}
GENERATED = (".cast.mp4",)  # our own outputs: pick the original instead


@dataclass(frozen=True)
class Found:
    path: Path
    size: int
    mtime: float


def default_roots() -> list:
    home = Path.home()
    roots = [Path.cwd(), home / "Downloads", home / "Movies", home / "Desktop", home / "Documents"]
    seen, out = set(), []
    for r in roots:
        try:
            key = r.resolve()
        except OSError:
            continue
        if r.is_dir() and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def is_candidate(name: str) -> bool:
    low = name.lower()
    return Path(low).suffix in VIDEO_EXTS and not low.endswith(GENERATED) and not low.startswith(".")


def find_videos(roots: list | None = None, max_depth: int = 4, min_bytes: int = 50_000_000,
                limit: int = 300, budget_s: float = 4.0) -> list:
    """Newest first. Bounded in depth, count and time so it stays snappy on a big home folder."""
    roots = default_roots() if roots is None else [Path(r) for r in roots]
    deadline = time.monotonic() + budget_s
    found: dict = {}

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth or time.monotonic() > deadline or len(found) >= limit:
            return
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS and not e.name.startswith("."):
                        walk(Path(e.path), depth + 1)
                elif e.is_file() and is_candidate(e.name):
                    st = e.stat()
                    if st.st_size >= min_bytes:
                        found[Path(e.path).resolve()] = Found(Path(e.path), st.st_size, st.st_mtime)
            except OSError:
                continue

    for r in roots:
        walk(r, 0)
    return sorted(found.values(), key=lambda f: f.mtime, reverse=True)[:limit]


def clean_dropped_path(text: str) -> Path:
    """Terminal drag-and-drop pastes paths quoted or with backslash-escaped spaces."""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        t = t[1:-1]
    t = re.sub(r"\\(.)", r"\1", t)  # the terminal escapes spaces, quotes, brackets, &, ... one backslash each
    return Path(os.path.expanduser(t))
