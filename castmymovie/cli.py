"""Entry point. There is nothing to learn: run `castmymovie` and follow the prompts."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import __version__


def install_signal_handlers() -> None:
    """Turn `kill` and a hangup into a normal exit, so the finally blocks stop the cast and ffmpeg instead of orphaning them."""
    def leave(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, leave)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="castmymovie",
        description="Get a movie onto your Google TV: pick it, make it cast-ready, cast it. "
                    "Run it with no arguments and follow the prompts.")
    ap.add_argument("file", nargs="?", type=Path,
                    help="optional: skip the picker and use this movie (you can also drag a file into the terminal)")
    ap.add_argument("--version", action="version", version=f"castmymovie {__version__}")
    args = ap.parse_args(argv)
    if not sys.stdin.isatty():
        print("castmymovie is interactive: run it in a terminal.", file=sys.stderr)
        return 1
    from .ui import Wizard  # imported late so --help and --version stay instant
    install_signal_handlers()
    return Wizard().run(args.file.expanduser().resolve() if args.file else None)


if __name__ == "__main__":
    sys.exit(main())
