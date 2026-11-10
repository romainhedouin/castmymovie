# How castmymovie works

The details behind the [README](../README.md): how the casting works, how the player's numbers are
measured, and why the quality presets are what they are.

## How it casts (and why not from Chrome)

Chrome's tab casting is a *live mirror*: it re-encodes the page in real time at about 5 Mbit/s, 1080p,
30 fps and stereo
([Chromium source](https://chromium.googlesource.com/chromium/src/+/main/components/mirroring/service/mirror_settings.cc)),
with almost no buffer. One Wi-Fi hiccup and it stutters or drops to a blocky 0.3 Mbit/s. castmymovie
instead lets the **TV pull the file and buffer it**, which rides out the spikes.

castmymovie runs a tiny web server on the Mac that serves only the prepared `.cast.mp4`, only to the TV,
under a random address. It then asks the TV to open Google's Default Media Receiver (the player built into
every Cast device) and play that address as an ordinary movie (`streamType: BUFFERED`). From there the TV
does what it does with any online video: it downloads ahead, pauses and rebuffers *picture and sound
together* when the Wi-Fi dips, and seeks by asking for a different part of the file (HTTP Range requests).
Nothing is re-encoded on the way: the file plays exactly as prepared, 5.1 sound included.

The Cast sender is written from scratch (`nativecast.py` on top of `castlink.py`, with `zeroconf` for
finding TVs): there is no Cast library. castmymovie keeps a control connection to the TV for the panel and
the keys, and looks after the cast:

- if that connection drops or goes silent for 15 s, it reconnects (after 1, 2, 4, 8... s) and, if the TV is
  still playing, picks the session up again without touching playback;
- if the TV reports a playback error, or lost the movie while the connection was down, it loads the movie
  again *where it was* (never from the start), at most 3 times in 10 minutes, then stops and tells you the
  time to start from;
- if someone else casts to the TV, or you close the player with the TV remote, it lets them and stops.

### Cast on the fly

See the README for what it does and when it is offered. Under the hood:

- The sound is converted first in a separate pass (`.mka`), so the on-the-fly encoder only does video.
- ffmpeg writes 6 s MPEG-TS segments (H.264 + AAC) with a fixed frame-count GOP, so every segment is exactly
  `round(6 × fps) / fps` long (6.006 s at 23.976 fps).
- The TV gets a complete VOD playlist generated up front from the movie's duration, which is why the full
  length and the seek bar are there from the first second.
- The server holds a segment request until the encoder has written it. A request far from the encoder (a
  seek) restarts ffmpeg at that segment (`-ss`, `-start_number`, `-output_ts_offset`). Segments already
  written stay on disk, and an encoder run stops when it reaches a segment an earlier run wrote.
- Segments live in `castmymovie-otf-<pid>-*` in the temp folder. They are deleted when the cast ends, and
  the next start removes folders whose process is gone.
- The TV's HLS player only keeps about 1 to 2 segments (6 to 12 s) ahead. The panel's "ahead" figure is how
  far the *encoder* is ahead of the TV, not how much the TV has buffered.

## How the player's numbers are measured

**The Link and Buffer rows come from the Mac's own web server.** It counts every byte it hands the TV, which
gives the delivery rate, and it knows exactly how far into the file the TV has downloaded, which the movie's
bitrate profile turns into seconds: the buffer figure is good to about a second (how the file interleaves
sound and picture). Cast on the fly, the buffer is exact: the seconds of segments the TV has fetched past its
position. Round-trip time and retransmits are not measured, since the server only sees what it sends.

The TV's state and position come from the cast's own control connection, so no second connection is opened.
Cast devices use self-signed certificates, so the connection does not verify the TV's certificate; it only
talks to your own LAN.

Two things that look odd but are right: the buffer can read 50+ s in a quiet, low-bitrate scene because the
TV prefetches a fixed amount of *data* (about 6 MB), not a fixed number of seconds; and "delivered" can sit
well below "needs" for a few seconds because the TV pulls in bursts, which is why it is averaged over 30 s.

## Why 6 Mbit/s for 2.4 GHz?

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

The limit applies to the **peak** over any 10 second window of video + audio, measured from real packet
sizes rather than the container's average, because a 1.8 Mbit/s average file can burst to 5+ Mbit/s. When
re-encoding, video is capped at 90% of the limit minus the audio bitrate with a 1-second buffer, so no 10 s
window can exceed it. To measure your own link, run a speed test app on the TV and use *Custom* with about
measured ÷ 2.5.

## Encoder choice

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
