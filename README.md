# castmymovie

Get any movie onto your Google TV, even over weak 2.4 GHz Wi-Fi. Pick a movie, choose how good your
Wi-Fi is, and castmymovie makes it cast-ready, then plays it on your TV, with pause and seek controls right
in the terminal. Your Mac serves the file and the TV's own player streams and buffers it like any online
video; VLC is there as an alternative. No flags to learn: run it and follow the prompts.

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
 ❯ Weak Wi-Fi  6 Mbit/s   instant remux   2.4 GHz or far from the router (safest)
   Good Wi-Fi  10 Mbit/s  instant remux   5 GHz in the same room
   Great Wi-Fi 20 Mbit/s  instant remux   5 GHz with a strong signal
   Original    no cap     instant remux   keep the quality, only fix what is incompatible
   Custom…

  Remuxing   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 121.9× 0:00:00 ETA 0:00:00 38 MB
╭─────────────────────────────────── Ready ───────────────────────────────────╮
│ clip.cast.mp4  38 MB  ·  done in 00:02                                      │
│ ✓ Verified: duration matches, index at the front, peak 4.5 Mbit/s (limit 6) │
╰─────────────────────────────────────────────────────────────────────────────╯

4 · Play it on your TV ─────────────────────────────────────────────────
❯ Play it on your TV now?
 ❯ Cast it to your TV (recommended)
   Cast it with VLC instead
   Not now
❯ Which TV?
 ❯ Living Room TV  (Chromecast, 192.168.1.20)

╭────────────────────── Casting to Living Room TV ──────────────────────╮
│ My Movie 2020 1080p HEVC 5.1.cast.mp4                                 │
│                                                                       │
│ ▶ 50:41 ━━━━━━━━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 2:34:45          │
│                                                                       │
│ Buffer ━━━━━━━━━━━━━━━━━━━━━━╺━ ≈ 55 s (estimate) comfortable         │
│ Link   1.0 Mbit/s (needs 0.7) · 16 ms · 0.1% retransmits   ● good     │
│                                                                       │
│ playing   space pause   ←/→ ±10 s   ↑/↓ ±2 min   q stop               │
╰───────────────────────────────────────────────────────────────────────╯
```

## Install

Built and tested on macOS. Linux should work but is untested. You need Python 3.9+ and
[ffmpeg](https://ffmpeg.org). [VLC](https://www.videolan.org/vlc/) is optional: install it if you want the
VLC way of casting as well.

```
brew install ffmpeg
brew install --cask vlc      # optional
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
5. **Casts it.** You choose how (the TV's own player, recommended, or VLC), it finds your TVs on the
   network, asks where to start (`1:12:30`, or Enter for the beginning), and plays with a live panel:
   position, buffer, link health, and keys (space pause, ←/→ ±10 s, ↑/↓ ±2 min, q stop). If the movie
   has text subtitles you are asked whether the TV should show them.

The converted file is saved next to the original as `<name>.cast.mp4` (the original is never touched) and
also plays in Chrome. Text subtitles are saved next to it as `.vtt` files.

### How it casts (and why not from Chrome)

Chrome's tab casting is a *live mirror*: it re-encodes the page in real time at about 5 Mbit/s, 1080p,
30 fps and stereo
([Chromium source](https://chromium.googlesource.com/chromium/src/+/main/components/mirroring/service/mirror_settings.cc)),
with almost no buffer. One Wi-Fi hiccup and it stutters or drops to a blocky 0.3 Mbit/s. castmymovie
instead lets the **TV pull the file and buffer it**, which rides out the spikes. There are two ways to do
that; you pick one at the cast step.

**Cast it to your TV (the default).** castmymovie runs a tiny web server on the Mac that serves only the
prepared `.cast.mp4`, only to the TV, under a random address. It then asks the TV to open Google's Default
Media Receiver (the player built into every Cast device) and play that address as an ordinary movie
(`streamType: BUFFERED`). From there the TV does what it does with any online video: it downloads ahead,
pauses and rebuffers *picture and sound together* when the Wi-Fi dips, and seeks by asking for a different
part of the file (HTTP Range requests). Nothing is re-encoded on the way: the file plays exactly as
prepared, 5.1 sound included. castmymovie keeps a control connection to the TV for the panel and the keys,
and looks after the cast:

- if that connection drops or goes silent for 15 s, it reconnects (after 1, 2, 4, 8... s) and, if the TV is
  still playing, picks the session up again without touching playback;
- if the TV reports a playback error, or lost the movie while the connection was down, it loads the movie
  again *where it was* (never from the start), at most 3 times in 10 minutes, then stops and tells you the
  time to start from;
- if someone else casts to the TV, or you close the player with the TV remote, it lets them and stops.

**Cast it with VLC instead.** A headless VLC re-muxes the file into a *live* Matroska stream for the TV
(VLC 3's Chromecast output always streams live). The video is passed through untouched, and so are stereo
AAC and Opus, but VLC decodes and re-encodes any other audio (5.1 AAC, MP3, FLAC...) live while casting:
castmymovie warns you when that will happen. The weak spot is what happens on a bad link: a TV that falls
behind on a live stream drops video and keeps the sound, and when the TV reports a stall VLC restarts the
stream from where it began and switches to live video transcoding. VLC stays available because it is a
different code path: if the TV's own player refuses a file, VLC may still get it on screen. castmymovie
drives VLC through its remote-control interface, which is where the pause, seek and
position bar come from in that mode. The prepared file is the same for both (it is made for the TV's own
player), which is why VLC may have audio to convert.

### Cast on the fly (experimental)

A video re-encode takes a while (a 2 h movie is about 20 minutes on a recent Mac). When your choice of
quality means the *video* has to be re-encoded, the wizard also asks *When should the movie start playing?*:
the default is to convert first, as above. The other answer, *Cast on the fly*, starts playing a few
seconds after you pick the TV and keeps converting while you watch.

- **When it is offered.** Only when the video must be re-encoded. As-is, remux and audio-only fixes are
  already seconds, so there is nothing to wait for and the question is not asked.
- **How it works.** The sound is converted first, in a short separate pass. Then ffmpeg cuts the video into
  6 s pieces (H.264 and AAC in MPEG-TS, the HLS format every Cast device plays) and the Mac serves them to
  the TV as an ordinary stream, with the full length and the seek bar from the first second. If the TV
  asks for a piece that is not ready yet, the request waits for it.
- **Native cast only.** VLC is not offered and not switched to in this mode, and no `.cast.mp4` is written
  (so no verification pass either: the bitrate cap is held by the encoder's settings, not re-measured).
- **Nothing is left behind.** The pieces live in a temporary folder and are deleted when the cast stops
  (Ctrl-C and `q` included). After a crash the next start sweeps the leftovers.
- **Seeking.** Going back to a part already converted is instant. A jump far ahead of the encoder makes it
  restart there, which can pause the picture for a few seconds.
- **The Encoder row** of the panel shows how far the conversion has got, its speed and how far ahead of the
  TV it is. It turns yellow with a *slow* hint when the encoder barely keeps up: a heavy movie (4K, an old
  Mac) may convert slower than real time, and then converting first is the better choice.
- **If it does not work out**, *What next?* offers *Convert it fully, then cast (slower, reliable)*, which
  makes the usual file and carries on as if you had chosen that at the start.

## Reading the player

| Row | What it tells you |
|---|---|
| `▶ 50:41 ━━ 2:34:45` | Position and length. Natively it is the TV's own clock (asked every 1.5 s, smoothed in between); with VLC, VLC's clock, which tracks the TV to within about a second. |
| `playing` / `paused` / `buffering…` | The TV's own state. `buffering…` comes straight from the TV, so it is exact. |
| `Buffer ≈ 55 s (estimate)` | How many seconds the TV has downloaded ahead of what it is showing. |
| `Encoder encoded to 41:20 (8.4x)  17 min ahead` | Only when casting on the fly: how far the conversion has got, how fast, and its lead over the TV (yellow *slow* when it barely keeps up). |
| `Link 1.0 Mbit/s (needs 0.7) · 16 ms · 0.1% retransmits` | Over the last 30 s: data delivered vs what the movie needs, round-trip time, and the share of data that had to be re-sent. Natively only the first part is shown (see below). |
| `● good / fair / poor / refilling` | One-word verdict. `refilling` means a seek just emptied the buffer, which is normal. |
| a dim line such as `TV reconnected` | Native cast only: what castmymovie just did to keep the cast going (a reconnect, a reload after a TV error and where). When the cast ends on its own, it says why. |

**Natively, the Link and Buffer rows come from the Mac's own web server.** It counts every byte it hands
the TV, which gives the delivery rate, and it knows exactly how far into the file the TV has downloaded,
which the movie's bitrate profile turns into seconds: the buffer figure is then good to about a second (how
the file interleaves sound and picture). The operating system's TCP figures (round-trip time,
retransmits) are not read in this mode, so those parts of the Link row are left out.

**With VLC, the buffer figure is an estimate, good to roughly ±10 s and sometimes worse.** A Cast TV never says how much it has buffered
(its status messages carry only position and state), so castmymovie infers it: the bytes sent to the TV
are converted to media time using the movie's real bitrate profile, minus where the TV is now. That
conversion is not exact (VLC re-muxes the stream and converts the audio), so the number is smoothed,
rounded, and re-anchored whenever the truth is known: at a stall the buffer is empty, and after a seek the
TV starts filling from scratch. Against a real Xiaomi Google TV it showed a believable sawtooth (draining
about 1 s per second, jumping each time the TV pulled a chunk) and stayed flat while paused. Over a 5 minute
run it stayed in a plausible 20-50 s band without runaway drift, but its low points moved by 20 s or more
between scenes, partly the estimate and partly the TV's own behaviour, which I could not separate.

Two things that look odd but are right: the buffer can read 50+ s in a quiet, low-bitrate scene because the
TV prefetches a fixed amount of *data* (about 6 MB), not a fixed number of seconds; and "delivered" can
sit well below "needs" for a few seconds because the TV pulls in bursts, which is why it is averaged over 30 s.

With VLC, the TV's state and position come from a small read-only Cast client built in (`castlink.py`): it
connects as a second sender, the way the Google Home app does, and only ever sends status requests and
heartbeats. It never launches anything or sends playback commands (a test enforces this). Link statistics
come from the operating system's TCP counters (`nettop`) and are **macOS only**: elsewhere the Link row is
simply left out. The native cast needs neither: it reads its own control connection and its own server.
Cast devices use self-signed certificates, so neither connection verifies the TV's certificate; they only
talk to your own LAN.

## Choosing the quality

| Option | Peak limit | Pick it when |
|---|---|---|
| Weak Wi-Fi | 6 Mbit/s | the TV is on 2.4 GHz or far from the router (default, safest) |
| Good Wi-Fi | 10 Mbit/s | 5 GHz in the same room |
| Great Wi-Fi | 20 Mbit/s | 5 GHz with a strong signal |
| Original | none | you only want incompatible parts fixed |
| Custom | your number | you measured your link |

The limit applies to the **peak** over any 10 second window of video + audio, measured from real packet
sizes rather than the container's average, because a 1.8 Mbit/s average file can burst to 5+ Mbit/s.

### Why 6 Mbit/s for 2.4 GHz?

> These are estimates from published figures and standard rules of thumb, not a measurement of your
> network. Pick *Custom* to tune.

- A Chromecast/Google TV-class device has **one antenna**, and 2.4 GHz allows **20 MHz** channels in
  practice. 802.11n peaks at a 72 Mbit/s link rate there.
- Real TCP throughput is a fraction of that. Published tests land around 20-30 Mbit/s in decent conditions,
  with ~60 Mbit/s as a ceiling on better hardware
  ([source](https://blog.wirelessmoves.com/2023/09/wi-fi-bandwidth-vs-range-vs-throughput.html)).
- 2.4 GHz is shared with neighbours' networks, microwaves and Bluetooth, and walls push the device to
  slower modulations and retries. I assume a **worst realistic sustained rate of about 12 Mbit/s** (an
  engineering assumption, not a measurement).
- Streaming needs headroom for retries and bursts: **2-3× the bitrate** is the usual rule.
  12 Mbit/s ÷ 2 = **6 Mbit/s**, and that is a peak, not an average.

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
| Audio | AAC / MP3 / Opus / FLAC, ≤ 5.1 | convert to AAC (stereo 192 kbit/s, 5.1 384 kbit/s) |
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
address…* (your router's device list shows it; it must be a home-network address like 192.168.1.20, not a
name). macOS may ask Terminal for local network access: allow it.

**The TV shows the Cast screen but the movie never starts** (or castmymovie says the TV *never came to
download it*). With the default cast the TV downloads the movie from the Mac, so the TV must be able to
reach the Mac:

- The first time, macOS may ask whether *Python* may accept incoming network connections: choose
  **Allow**. If you missed it, open System Settings > Network > Firewall > Options and allow Python (or turn
  the firewall's *Block all incoming connections* off).
- The Mac and the TV must be on the same network: not a guest Wi-Fi (those often keep devices from seeing
  each other), and no VPN on the Mac.
- The movie is only served to the address the TV was found at. If castmymovie says it *turned away* another
  address, that is probably the TV: choose *Enter an IP address…* and type that one.

If the TV downloaded part of the movie and still refused it, its player cannot decode the file: try
*Cast it with VLC instead*.

**Stutters or buffers a lot.** Your Wi-Fi to the TV is the weak link, not the file. In order of impact:
move the TV to 5 GHz if the router allows it; set a fixed 2.4 GHz channel (1, 6 or 11) instead of Auto; put
the Mac on 5 GHz or Ethernet so the video does not cross the same congested band twice; pick *Weak Wi-Fi*.
The panel shows *buffering…* when the TV stalls, and the Link row shows the retransmit rate. A quick check from the terminal is
`ping <tv-ip>`: healthy is under 10 ms with no loss.

**The picture freezes but the sound goes on (VLC).** VLC sends the movie to the TV as a *live* stream, and a TV
that falls behind on a live stream drops video and keeps the audio running; the picture only comes back at the
next keyframe (4-10 s later in a typical film). It means the data stopped arriving in time, so look at the Wi-Fi
first (see above): a link with 100+ ms ping and a few percent loss cannot sustain even 2 Mbit/s over TCP. The
default cast avoids this failure mode: the TV pauses both picture and sound until it has enough again.

**Nothing plays, or the cast stops by itself.** The terminal (and the dim line in the player panel) says why.
Each cast keeps a log, overwritten by the next one:

- default cast: `$TMPDIR/castmymovie-native.log`, one timestamped line for every message to and from the TV
  (except heartbeats), every state change (`STATE PLAYING -> BUFFERING at 41:10`), every reconnect and
  reload decision, and every download the TV made from the Mac (`HTTP ... GET movie Range=bytes=... -> 206`).
  It stops growing at 20 MB (about 12 hours of casting);
- VLC: `$TMPDIR/castmymovie-vlc.log` (verbose: it shows how VLC set up the stream).

`open $TMPDIR` in the terminal shows the folder (`/tmp` if `$TMPDIR` is not set). The logs contain the full
path of the movie and the TV's address, so they are created readable by you only; in the default cast's log the secret part of the
movie's web address is written as `***`. If a log cannot be created (say, another user has a file by that name in `/tmp`),
the cast goes ahead without it.

## Limits

- Subtitles: with the default cast the TV's player shows the `.vtt` file you pick at the cast step. With
  VLC they are not shown: VLC passes the video through untouched, so there is nothing to draw them on, as
  far as its logs show.
- HDR sources are flagged, not tone-mapped. Image-based subtitles (PGS/VobSub) can't be converted.
- One video stream and one audio track are kept (you are asked when there are several).
- HEVC is only kept on macOS; elsewhere it is re-encoded to H.264 to be safe.
- With VLC the buffer figure is an estimate (about ±10 s, sometimes more) and the Link row is macOS only.
- The default cast needs the TV to reach the Mac over the network (see Troubleshooting).
- Tested against a Xiaomi Google TV; other Cast receivers should work but are untested.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m unittest discover -s tests
```

About 450 tests, around 3 minutes, all on this machine (nothing goes beyond 127.0.0.1). They build tiny
synthetic clips with ffmpeg and cover every decision path, the movie server (ranges, CORS, refusals,
hang-ups, parallel downloads), the native sender against a fake Cast TV (`tests/fakereceiver.py`: real
Cast framing, really downloads the movie with Range requests like a TV, and can drop the connection, go
silent, lose the movie or fail on purpose), the VLC controller (against a fake VLC speaking the real rc
protocol), the Cast status client (including frames captured byte-for-byte from a real TV), the buffer and
link maths, and the whole wizard (with scripted prompts and fake casters).

## License

MIT
