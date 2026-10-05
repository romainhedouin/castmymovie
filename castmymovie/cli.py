"""Entry point. There is nothing to learn: run `castmymovie` and follow the prompts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="castmymovie",
        description="Get a movie onto your Google TV: pick it, make it cast-ready, play it with VLC. "
                    "Run it with no arguments and follow the prompts.")
    ap.add_argument("file", nargs="?", type=Path,
                    help="optional: skip the picker and use this movie (you can also drag a file into the terminal)")
    ap.add_argument("--version", action="version", version=f"castmymovie {__version__}")
    args = ap.parse_args(argv)
    if not sys.stdin.isatty():
        print("castmymovie is interactive: run it in a terminal.", file=sys.stderr)
        return 1
    from .ui import Wizard  # imported late so --help and --version stay instant
    return Wizard().run(args.file.expanduser().resolve() if args.file else None)


if __name__ == "__main__":
    sys.exit(main())
