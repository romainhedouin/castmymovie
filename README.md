# castmymovie

Get any movie onto your Google TV, even over weak 2.4 GHz Wi-Fi. Pick a movie, choose how good your
Wi-Fi is, and castmymovie makes it cast-ready, then plays it on your TV through VLC, with pause and seek
controls right in the terminal. No flags to learn: run it and follow the prompts.

```
castmymovie
```

```
╭───────────────────────────────────────────────────────────────────╮
│   castmymovie  v0.2.0                                             │
│   Pick a movie → make it cast-ready → play it on your Google TV   │
╰───────────────────────────────────────────────────────────────────╯

1 · Pick a movie ───────────────────────────────────────────────────────
❯ Which movie? (↑/↓ move, Enter select, type to filter)
 ❯ My Movie (2020).mp4              2.11 GB   ~/Movies   
   Another.Movie.2014.1080p…mp4     1.77 GB   ~/Downloads
   Type or drop a file path…
   Quit

3 · Choose the quality ─────────────────────────────────────────────────
❯ How good is the Wi-Fi to your TV?
 ❯ Weak Wi-Fi  6 Mbps   instant remux   2.4 GHz or far from the router (safest)
   Good Wi-Fi  10 Mbps  instant remux   5 GHz in the same room
   Great Wi-Fi 20 Mbps  instant remux   5 GHz with a strong signal
   Original    no cap   instant remux   keep the quality, only fix what is incompatible
   Custom…

  Remuxing   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 121.9× 0:00:00 ETA 0:00:00 38 MB
╭────────────────────────────────── Ready ──────────────────────────────────╮
│ clip.cast.mp4  38 MB  ·  done in 00:02                                    │
│ ✓ Verified: duration matches, index at the front, peak 4.5 Mbps (limit 6) │
╰───────────────────────────────────────────────────────────────────────────╯

4 · Play it on your TV ─────────────────────────────────────────────────
❯ Which TV?
 ❯ Living Room TV  (Chromecast, 192.168.1.20)

╭──────────────── Casting to Living Room TV ─────────────────╮
│ clip.cast.mp4                                              │
│ ▶ 01:07 ━━━━━━━━━━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━ 03:01 │
│ playing   space pause   ←/→ ±30 s   ↑/↓ ±5 min   q stop    │
╰────────────────────────────────────────────────────────────╯
```

## Install

Built and tested on macOS. Linux should work but is untested. You need Python 3.9+, [ffmpeg](https://ffmpeg.org)
and [VLC](https://www.videolan.org/vlc/):

```
brew install ffmpeg
brew install --cask vlc
git clone https://github.com/romainhedouin/castmymovie && cd castmymovie
./install.sh
```

`install.sh` creates a private virtualenv and puts `castmymovie` on your PATH. Re-run it to update.
Prefer pipx? `pipx install .` works too.

## How it works

1. **Pick a movie.** It lists the movies it finds in your Downloads, Movies, Desktop and Documents
   (type to filter), or you can type or drag in a path. `castmymovie path/to/movie.mkv` skips the list.
2. **Checks it.** It reads the file once, including the real bitrate profile (about 4 s for a 2 GB file).
3. **You choose the quality.** Each option previews what it will do to *your* movie: nothing, a lossless
   remux that takes seconds, or a slower re-encode.
4. **Converts and verifies.** Progress bar with speed and ETA. It re-probes the result: duration, streams,
   index position, peak bitrate, a test decode. Ctrl-C stops cleanly and leaves no partial file.
5. **Casts it.** It finds your TVs on the network, asks where to start (`1:12:30`, or Enter for the
   beginning), and plays through VLC with live controls.

The converted file is saved next to the original as `<name>.cast.mp4` (the original is never touched) and
also plays in Chrome. Text subtitles are saved next to it as `.vtt` files.

### Why VLC instead of casting from Chrome

Chrome's tab casting is a *live mirror*: it re-encodes the page in real time at about 5 Mbps, 1080p,
30 fps and stereo
([Chromium source](https://chromium.googlesource.com/chromium/src/+/main/components/mirroring/service/mirror_settings.cc)),
with almost no buffer. One Wi-Fi hiccup and it stutters or drops to a blocky 0.3 Mbps. VLC instead serves
the file and the **TV pulls it and buffers it**, which rides out the spikes. The video is passed through
untouched (HEVC stays HEVC); only the audio is converted on the fly.

VLC runs without a window and castmymovie drives it through VLC's remote-control interface, which is
where the pause, seek and position bar come from.

## Choosing the quality

| Option | Peak limit | Pick it when |
|---|---|---|
| Weak Wi-Fi | 6 Mbps | the TV is on 2.4 GHz or far from the router (default, safest) |
| Good Wi-Fi | 10 Mbps | 5 GHz in the same room |
| Great Wi-Fi | 20 Mbps | 5 GHz with a strong signal |
| Original | none | you only want incompatible parts fixed |
| Custom | your number | you measured your link |

The limit applies to the **peak** over any 10 second window of video + audio, measured from real packet
sizes rather than the container's average, because a 1.8 Mbps average file can burst to 5+ Mbps.

### Why 6 Mbps for 2.4 GHz?

> These are estimates from published figures and standard rules of thumb, not a measurement of your
> network. Pick *Custom* to tune.

- A Chromecast/Google TV-class device has **one antenna**, and 2.4 GHz allows **20 MHz** channels in
  practice. 802.11n peaks at a 72 Mbps link rate there.
- Real TCP throughput is a fraction of that. Published tests land around 20-30 Mbps in decent conditions,
  with ~60 Mbps as a ceiling on better hardware
  ([source](https://blog.wirelessmoves.com/2023/09/wi-fi-bandwidth-vs-range-vs-throughput.html)).
- 2.4 GHz is shared with neighbours' networks, microwaves and Bluetooth, and walls push the device to
  slower modulations and retries. I assume a **worst realistic sustained rate of about 12 Mbps** (an
  engineering assumption, not a measurement).
- Streaming needs headroom for retries and bursts: **2-3× the bitrate** is the usual rule.
  12 Mbps ÷ 2 = **6 Mbps**, and that is a peak, not an average.

When re-encoding, video is capped at 90% of the limit minus the audio bitrate with a 1-second buffer, so no
10 s window can exceed it. To measure your own link, run a speed test app on the TV and use *Custom* with
about measured ÷ 2.5.

## What it checks

| Check | Passes when | Otherwise |
|---|---|---|
| Container | MP4 (or WebM) **with the index at the front** | remux to MP4 + `faststart` |
| Video codec | H.264 Baseline/Main/High, 8-bit 4:2:0, level ≤ 4.2; or HEVC ≤ 5.1 if this Mac's Chrome (≥ 107) can hardware-decode it | re-encode to H.264 |
| Resolution | ≤ 1920×1080 | downscale |
| Scan type | progressive | deinterlace |
| Audio | AAC / MP3 / Opus / FLAC, ≤ 5.1 | convert to AAC (stereo 192k, 5.1 384k) |
| Bitrate | worst 10 s window ≤ your limit | re-encode with a hard cap |
| Frame rate, HDR | heads-up only | warned, not changed |
| Subtitles | n/a | text tracks exported as `.vtt` |

Re-encodes use x264 `veryfast`. On a 60 s slice of a 1080p film (CRF 20, capped, Apple M4):

| Encoder | Speed | Size | SSIM vs source |
|---|---|---|---|
| x264 `veryfast` (used) | **5.5×** | 21.3 MB | 0.99239 |
| x264 `faster` | 3.4× | 23.3 MB | 0.99291 |
| x264 `medium` | 2.3× | 23.7 MB | 0.99279 |
| VideoToolbox (hardware) | 8.3× | 23.8 MB | 0.99233 |

`veryfast` is 2.4× faster than `medium` with practically the same quality. VideoToolbox is faster still but
has looser rate control, which matters when the peak must stay under a cap. A 2.5 hour film re-encodes in
about half an hour; a remux takes seconds.

## Troubleshooting

**No TV found.** The TV must be on and on the same Wi-Fi as the Mac. Choose *Search again*, or *Enter an IP
address…* (your router's device list shows it). macOS may ask Terminal for local network access: allow it.

**Stutters or buffers a lot.** Your Wi-Fi to the TV is the weak link, not the file. In order of impact:
move the TV to 5 GHz if the router allows it; set a fixed 2.4 GHz channel (1, 6 or 11) instead of Auto; put
the Mac on 5 GHz or Ethernet so the video does not cross the same congested band twice; pick *Weak Wi-Fi*.
The panel shows *buffering…* when the TV stalls. A quick check from the terminal is
`ping <tv-ip>`: healthy is under 10 ms with no loss.

**Nothing plays.** The terminal shows a message and VLC's log is kept at `$TMPDIR/castmymovie-vlc.log`.

## Limits

- Subtitles are not shown while casting: VLC passes the video through untouched, so there is nothing to
  draw them on, as far as its logs show. The `.vtt` files are for other players.
- HDR sources are flagged, not tone-mapped. Image-based subtitles (PGS/VobSub) can't be converted.
- One video stream and one audio track are kept (you are asked when there are several).
- HEVC is only kept on macOS; elsewhere it is re-encoded to H.264 to be safe.
- Tested against a Xiaomi Google TV; other Cast receivers should work but are untested.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m unittest discover -s tests
```

54 tests, around 45 s. They build tiny synthetic clips with ffmpeg and cover every decision path,
the VLC controller (against a fake VLC speaking the real rc protocol) and the whole wizard (with scripted
prompts and a fake caster).

## License

MIT
