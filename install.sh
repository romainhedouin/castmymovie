#!/usr/bin/env bash
# Installs castmymovie into its own virtualenv and puts the command on your PATH.
# Re-run it any time to update after pulling changes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
home="${CASTMYMOVIE_HOME:-$HOME/.local/share/castmymovie}"
venv="$home/venv"

command -v python3 >/dev/null || { echo "python3 is required (https://www.python.org)"; exit 1; }

echo "Installing castmymovie into $venv ..."
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet --upgrade "$here"

linked=""
for dir in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  if [ -d "$dir" ] && [ -w "$dir" ]; then
    ln -sf "$venv/bin/castmymovie" "$dir/castmymovie"
    linked="$dir/castmymovie"
    break
  fi
done

if [ -n "$linked" ]; then
  echo "Installed: $linked"
else
  echo "Installed at $venv/bin/castmymovie (no writable PATH folder found; add a symlink or PATH entry yourself)."
fi

command -v ffmpeg  >/dev/null || echo "Missing ffmpeg: brew install ffmpeg"
[ -e /Applications/VLC.app ] || command -v vlc >/dev/null || echo "Missing VLC (needed for casting): brew install --cask vlc"
echo "Run:  castmymovie"
