# castmymovie

Cast any movie file from your Mac to a Google TV, even over weak 2.4 GHz Wi-Fi. Pick a movie, say how good
your Wi-Fi is, and castmymovie makes it cast-ready and plays it on the TV, with pause and seek keys right in
the terminal. The TV downloads and buffers the file like any online video. No flags to learn: run it and
follow the prompts.

```
castmymovie
```

```
╭───────────────────────────────────────────────────────────────────╮
│   castmymovie  v0.3.0                                             │
│   Pick a movie → make it cast-ready → play it on your Google TV   │
╰───────────────────────────────────────────────────────────────────╯

1 · Pick a movie ───────────────────────────────────────────────────────
❯ Which movie? (↑/↓ move, Enter select, type to filter)
 ❯ My Movie (2020).mp4              2.11 GB   ~/Movies
   Another.Movie.2014.1080p…mp4     1.77 GB   ~/Downloads
   Type or drop a file path…

3 · Choose the quality ─────────────────────────────────────────────────
❯ How good is the Wi-Fi to your TV?
 ❯ Weak Wi-Fi  6 Mbit/s   instant remux   2.4 GHz or far from the router (safest)
   Good Wi-Fi  10 Mbit/s  instant remux   5 GHz in the same room
   Great Wi-Fi 20 Mbit/s  instant remux   5 GHz with a strong signal
   Original    no cap     instant remux   keep the quality, only fix what is incompatible
   Custom…

4 · Play it on your TV ─────────────────────────────────────────────────
╭────────────────────── Casting to Living Room TV ──────────────────────╮
│ My Movie 2020 1080p HEVC 5.1.cast.mp4                                 │
│ ▶ 50:41 ━━━━━━━━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 2:34:45          │
│ Buffer ━━━━━━━━━━━━━━━━━━━━━━╺━ ≈ 55 s  comfortable                   │
│ Link   1.0 Mbit/s (needs 0.7) · 16 ms · 0.1% retransmits   ● good     │
│ playing   space pause   ←/→ ±10 s   ↑/↓ ±2 min   q stop               │
╰───────────────────────────────────────────────────────────────────────╯
```

## Install

You need a Mac (Linux should work but is untested), Python 3.9+ and [ffmpeg](https://ffmpeg.org).
[VLC](https://www.videolan.org/vlc/) is optional.

```
brew install ffmpeg
brew install --cask vlc      # optional
git clone https://github.com/romainhedouin/castmymovie && cd castmymovie
./install.sh
```

`install.sh` creates a private virtualenv and puts `castmymovie` on your PATH. Re-run it to update.
Prefer pipx? `pipx install .` works too.

## How it works

1. **Pick a movie.** It lists the movies in your Downloads, Movies, Desktop and Documents (type to filter),
   or type or drag in a path. `castmymovie path/to/movie.mkv` skips the list.
2. **It checks the file** (about 4 s for a 2 GB movie), including the real bitrate profile.
3. **You choose the quality.** Each option previews what it will do to *your* movie: nothing, a lossless
   remux that takes seconds, or a slower re-encode.
4. **It converts and verifies.** Progress bar with speed and ETA. Ctrl-C stops cleanly and leaves no partial
   file. The result is saved next to the original as `<name>.cast.mp4` (the original is never touched), with
   text subtitles as `.vtt` files.
5. **It casts.** It finds your TVs, asks where to start (`1:12:30`, or Enter for the beginning), and plays
   with a live panel: position, buffer, link health, and keys.

Chrome's tab casting is a live mirror at about 5 Mbit/s with almost no buffer, so one Wi-Fi hiccup stutters.
castmymovie instead serves the file from your Mac and lets the TV's own player pull and buffer it.
[How it works](docs/how-it-works.md) has the details, including the VLC alternative.

### Cast on the fly (experimental)

A video re-encode takes a while (a 2 h movie is about 20 minutes on a recent Mac). When your quality choice
means the video has to be re-encoded, the wizard asks *When should the movie start playing?* The default is
to convert first. *Cast on the fly* starts playing a few seconds after you pick the TV and keeps converting
while you watch.

- Offered only when the video must be re-encoded. Everything else takes seconds, so it is not asked.
- The sound is converted first, then ffmpeg cuts the video into 6 s pieces that the Mac serves to the TV as
  an ordinary stream, with the full length and seek bar from the start.
- The TV's own player only (no VLC), and no `.cast.mp4` is written. The pieces live in a temporary folder
  and are deleted when the cast stops.
- Seeking back to a converted part is instant. A jump far ahead of the encoder restarts it there and can
  pause the picture for a few seconds.
- An *Encoder* row shows progress and speed, and turns yellow when it barely keeps up. A heavy movie (4K, an
  old Mac) may convert slower than real time: convert first instead.
- If it does not work out, *What next?* offers *Convert it fully, then cast*.

## Reading the player

| Row | What it tells you |
|---|---|
| `▶ 50:41 ━━ 2:34:45` | Position and length, from the TV's own clock. |
| `playing` / `paused` / `buffering…` | The TV's own state. |
| `Buffer ≈ 55 s` | Seconds the TV has downloaded ahead of what it is showing. |
| `Encoder encoded to 41:20 (8.4x)  17 min ahead` | Cast on the fly only: how far the conversion has got, how fast, and its lead over the TV. |
| `Link 1.0 Mbit/s (needs 0.7) · 16 ms · 0.1% retransmits` | Over the last 30 s: data delivered vs what the movie needs, round-trip time, share of data re-sent. The last two are VLC only. |
| `● good / fair / poor / refilling` | One-word verdict. `refilling` means a seek just emptied the buffer, which is normal. |
| a dim line such as `TV reconnected` | What castmymovie just did to keep the cast going. When the cast ends on its own, it says why. |

With VLC the buffer figure is an estimate (about ±10 s). See [how it works](docs/how-it-works.md).

## Choosing the quality

| Option | Peak limit | Pick it when |
|---|---|---|
| Weak Wi-Fi | 6 Mbit/s | the TV is on 2.4 GHz or far from the router (default, safest) |
| Good Wi-Fi | 10 Mbit/s | 5 GHz in the same room |
| Great Wi-Fi | 20 Mbit/s | 5 GHz with a strong signal |
| Original | none | you only want incompatible parts fixed |
| Custom | your number | you measured your link |

The limit applies to the peak over any 10 second window of video and audio, not the average. The reasoning
behind 6 Mbit/s is in [how it works](docs/how-it-works.md#why-6-mbits-for-24-ghz).

## What it checks

| Check | Passes when | Otherwise |
|---|---|---|
| Container | MP4 (or WebM) with the index at the front | remux to MP4 + `faststart` |
| Video codec | H.264 Baseline/Main/High, 8-bit 4:2:0, level ≤ 4.2; or HEVC ≤ 5.1 if this Mac's Chrome (≥ 107) can hardware-decode it | re-encode to H.264 |
| Resolution | ≤ 1920×1080 | downscale |
| Scan type | progressive | deinterlace |
| Audio | AAC / MP3 / Opus / FLAC, ≤ 5.1 | convert to AAC (stereo 192 kbit/s, 5.1 384 kbit/s) |
| Bitrate | worst 10 s window ≤ your limit | re-encode with a hard cap |
| Frame rate, HDR | heads-up only | warned, not changed |
| Subtitles | n/a | text tracks exported as `.vtt` |

## Troubleshooting

**No TV found.** The TV must be on and on the same Wi-Fi as the Mac. Choose *Search again*, or *Enter an IP
address…* (your router's device list shows it, e.g. 192.168.1.20). macOS may ask Terminal for local network
access: allow it.

**The TV shows the Cast screen but the movie never starts**, or castmymovie says the TV *never came to
download it*. The TV downloads the movie from the Mac, so it must be able to reach it:

- The first time, macOS may ask whether *Python* may accept incoming connections: choose **Allow**. If you
  missed it, open System Settings > Network > Firewall > Options and allow Python.
- The Mac and the TV must be on the same network: not a guest Wi-Fi, and no VPN on the Mac.
- The movie is only served to the address the TV was found at. If castmymovie says it *turned away* another
  address, that is probably the TV: choose *Enter an IP address…* and type that one.

If the TV downloaded part of the movie and still refused it, its player cannot decode the file: try *Cast it
with VLC instead*.

**Stutters or buffers a lot.** Your Wi-Fi to the TV is the weak link, not the file. In order of impact: move
the TV to 5 GHz; set a fixed 2.4 GHz channel (1, 6 or 11) instead of Auto; put the Mac on 5 GHz or Ethernet
so the video does not cross the same congested band twice; pick *Weak Wi-Fi*. `ping <tv-ip>` should be under
10 ms with no loss.

**Nothing plays, or the cast stops by itself.** The terminal and the dim line in the player say why. Each
cast also writes a log, overwritten by the next one, in `$TMPDIR` (`open $TMPDIR`; `/tmp` if unset):

- `castmymovie-native.log` for the TV's own player: every message to and from the TV, state changes, every
  reconnect and reload decision, and every download the TV made. It stops growing at 20 MB.
- `castmymovie-vlc.log` for VLC (verbose).

The logs are readable by you only. They contain the movie's path and the TV's address; the secret part of the
movie's web address is written as `***`.

## Limits

- Subtitles: the TV's player shows the `.vtt` file you pick at the cast step. With VLC they are not shown.
- HDR sources are flagged, not tone-mapped. Image-based subtitles (PGS/VobSub) can't be converted.
- One video stream and one audio track are kept (you are asked when there are several).
- HEVC is only kept on macOS; elsewhere it is re-encoded to H.264.
- The TV must be able to reach the Mac over the network.
- Link statistics with VLC are macOS only.
- Tested against a Xiaomi Google TV; other Cast receivers should work but are untested.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m unittest discover -s tests
```

About 450 tests, around 3 minutes, all on this machine (nothing goes beyond 127.0.0.1). They build tiny
synthetic clips with ffmpeg and exercise the converter, the movie server, the Cast sender against a fake TV
(`tests/fakereceiver.py`), the VLC controller against a fake VLC, and the whole wizard with scripted prompts.

## License

MIT
