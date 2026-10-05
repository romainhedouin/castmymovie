# castready

One command to make a movie file play smoothly in Chrome and cast to a Google TV, even over 2.4 GHz Wi-Fi.

```
castready movie.mkv
```

It probes the file, runs every check Chrome and Google Cast care about, and does the
**minimum** needed: nothing, a lossless remux (seconds), or a bitrate-capped re-encode.
It shows a progress bar with speed and ETA, then verifies the result.

```
clip.mkv  (38 MB, 03:01)
Analyzing ████████████████████████████████ 100.0%  1836.7x  00:00  ETA 00:00

  ✗ Container     MKV is not supported by Chrome/Cast (needs MP4)
  ✓ Video codec   HEVC Main, yuv420p
  ✓ Resolution    1920×1036
  ✓ Frame rate    23.976 fps
  ✓ Audio         AAC 5.1 [eng]
  ✓ Bitrate       avg 1.7 Mbps, peak 4.5 Mbps ≤ 6 Mbps (10 s window)
  ! Subtitles     1 text track(s) → .vtt sidecar files (Chrome's file player does not show embedded subtitles)

Plan: remux  (MKV → MP4)
      streams are copied untouched: lossless and takes seconds

Remuxing  ████████████████████████████████ 100.0%  1328.3x  00:00  ETA 00:00  38 MB

Done in 00:00: clip.cast.mp4 (38 MB)
✓ Verified: duration matches, index at front, peak 4.5 Mbps (limit 6), test decode 9× real-time
✓ Subtitles: clip.cast.eng.vtt
```

While re-encoding, the same bar shows live speed, elapsed time and ETA:

```
Encoding  ███████████████████░░░░░░░░░░░░░  59.8%    2.4x  00:45  ETA 00:34  45 MB
```

## Install

Needs Python 3.9+ and `ffmpeg` (with `ffprobe`, `libx264`): `brew install ffmpeg`.

```
git clone https://github.com/romainhedouin/castready && cd castready
ln -s "$PWD/castready.py" /usr/local/bin/castready      # or: pipx install .
```

No Python dependencies.

## Usage

Only the file is required. The output goes next to it as `<name>.cast.mp4`; the original is never touched.

| Option | Meaning |
|---|---|
| `--check` | Report what would be done, change nothing. Exit code 2 if work is needed. |
| `--max-mbps N` | Peak video+audio bitrate allowed. Default **6**, safe for 2.4 GHz. Try 15+ on 5 GHz or Ethernet. |
| `--lang CODE` | Preferred audio language (`en`, `fr`, …); also filters subtitle export. |
| `--h264` | Never keep HEVC (for older Chromecasts that lack HEVC decoding). |
| `--force` | Overwrite an existing `.cast.mp4`. |

Exit codes: `0` ready, `1` error, `2` work needed (`--check`), `130` cancelled. Ctrl-C stops
cleanly and leaves no partial file.

## What it checks

| Check | Passes when | Otherwise |
|---|---|---|
| Container | MP4 (or WebM) **with the index at the front** | remux to MP4 + `faststart` |
| Video codec | H.264 Baseline/Main/High, 8-bit 4:2:0, level ≤ 4.2; or HEVC ≤ 5.1 if this Mac's Chrome (≥ 107) can hardware-decode it | re-encode to H.264 |
| Resolution | ≤ 1920×1080 | downscale |
| Scan type | progressive | deinterlace (`yadif`) |
| Audio | AAC / MP3 / Opus / FLAC, ≤ 5.1 | convert to AAC (stereo 192k, 5.1 384k) |
| Bitrate | worst 10 s window of video + audio ≤ cap | re-encode with a hard cap |
| Frame rate, HDR | heads-up only | warned, not changed |
| Subtitles | n/a | text tracks exported as `.vtt` sidecars |

Peak bitrate is measured from the real packet sizes (no decoding, about 4 s for a 2 GB file),
not from the container's average, because **a 1.8 Mbps average file can burst to 5+ Mbps**.

After converting it re-probes the output: duration, streams, index position, peak bitrate,
and a test decode of a few slices.

## Why 6 Mbps for 2.4 GHz?

> These are estimates from published figures and standard rules of thumb, not a measurement of
> your network. Tune with `--max-mbps`.

- A Chromecast/Google TV-class device has **one antenna**, and 2.4 GHz allows **20 MHz** channels
  in practice. 802.11n peaks at a 72 Mbps link rate there.
- Real TCP throughput is a fraction of that. Published tests land around 20-30 Mbps in decent
  conditions, with ~60 Mbps as a ceiling on better hardware
  ([source](https://blog.wirelessmoves.com/2023/09/wi-fi-bandwidth-vs-range-vs-throughput.html)).
- The 2.4 GHz band is shared with neighbours' networks, microwaves and Bluetooth. Walls and
  distance push the device to slower modulations and retries. I assume a **worst realistic sustained rate of
  about 12 Mbps** (an engineering assumption, not a measurement).
- Streaming needs headroom for retries and bursts: **2-3× the bitrate** is the usual rule.
  12 Mbps ÷ 2 = **6 Mbps**, and that is a *peak*, not an average.
- Chrome's own tab-cast encoder tops out at about **5 Mbps, 1080p, 30 fps, stereo**
  ([Chromium source](https://chromium.googlesource.com/chromium/src/+/main/components/mirroring/service/mirror_settings.cc)),
  so going far beyond 6 Mbps buys nothing on that path.

When re-encoding, video is capped at 90% of the limit minus the audio bitrate with a 1-second
VBV buffer, so no 10 s window can exceed the cap.

**Measure your own link.** Run a speed test app on the TV, then use `--max-mbps` ≈ measured ÷ 2.5.
A TV at 30 Mbps on 2.4 GHz suggests `--max-mbps 12`.

## Why x264 `veryfast`?

Benchmark on a 60 s slice of a 1080p film (HEVC source, CRF 20, capped; Apple M4):

| Encoder | Speed | Size | SSIM vs source |
|---|---|---|---|
| x264 `veryfast` (default) | **5.5×** | 21.3 MB | 0.99239 |
| x264 `faster` | 3.4× | 23.3 MB | 0.99291 |
| x264 `medium` | 2.3× | 23.7 MB | 0.99279 |
| VideoToolbox (hardware) | 8.3× | 23.8 MB | 0.99233 |

`veryfast` is 2.4× faster than `medium` with practically the same quality. VideoToolbox is
faster still but has looser rate control, which matters when the peak must stay under a cap.
A full 2.5-hour film re-encodes in roughly half an hour; a remux takes seconds.

## Tips for smooth casting

- **Mac on Ethernet or 5 GHz.** If the Mac is also on 2.4 GHz, the video crosses the same
  congested band twice.
- Chrome's **Cast tab** re-encodes live, so close heavy tabs. If your Chrome offers the
  **Cast file** source, prefer it.
- Put the TV on 5 GHz if the router is in range. The cap exists for when it cannot.
- Older Chromecasts: use `--h264`.

## Limits

- HDR sources are flagged, not tone-mapped (colours look washed out when cast).
- Image-based subtitles (PGS/VobSub) can't be converted; no burn-in.
- Chrome's built-in file player does not show embedded or sidecar subtitles.
- One video stream and one audio track are kept (choose with `--lang`).
- HEVC is only kept on macOS; elsewhere it is re-encoded to H.264 to be safe.

## Development

```
python3 -m unittest discover -s tests -v
```

Tests build tiny synthetic clips with ffmpeg (about 20 s total) and cover every decision path.

## License

MIT
