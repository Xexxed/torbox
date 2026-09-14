# Torbox Downloader

A web-GUI front end for [TorBox](https://torbox.app): browse your torrents, download
them to disk with a folder structure that mirrors the torrent, mount the library as a
virtual drive, and stream media straight from the browser.

The browser is driven by Playwright against the TorBox download page, so everything the
tool does — listing torrents, expanding a file tree, generating a download link — happens
through one real signed-in session rather than an API.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- `playwright install chromium` (or `firefox` / `webkit`)

Optional, Windows only: `winfspy` plus the WinFsp driver, needed for the native drive
mount. Without it the app falls back to WebDAV over `net use`.

Optional: **FFmpeg** — both `ffmpeg` and `ffprobe` on `PATH` — enables on-the-fly remuxing
and transcoding in the Stream tab, so MKV and HEVC files can play in the browser. Without
it those files fall back to the external-player panel. A hardware H.264 encoder
(NVENC / Quick Sync / AMF) is used when one actually works, and `libx264` otherwise.
Nothing else in the app depends on FFmpeg.

Optional: **VLC**, for the Stream tab's **Open in VLC** button. Found automatically; set
`TORBOX_VLC_PATH` if it lives somewhere unusual. Without it the button is simply absent.

## Running

```bash
python main.py
```

This starts the dashboard on `http://localhost:<port>`, opens it in your default browser
with a one-time `api_key`, and runs the download pipeline in the background. Useful flags:

| Flag | Purpose |
|---|---|
| `--no-headless` | Show the browser window (needed for a first manual login) |
| `--token`, `-t` | Supply the `torbox_session_token` directly, skipping the login redirect |
| `--dir` | Destination for downloads (default `downloads`) |
| `--parallel-files` | How many files to download at once |
| `--disk-profile` | `auto` / `hdd` / `ssd` — disk I/O strategy |
| `--no-gui` | Run the CLI pipeline without the dashboard |

## Tabs

- **Dashboard** — session status, the download queue, and the live log console.
- **Stream** — the media browser (below).
- **Completed Downloads** — finished transfers and bulk link generation.
- **Raw System Logs** — the unfiltered log stream.

## The Stream tab

A Netflix-style media browser over your TorBox library, built on the same virtual
filesystem the drive mount uses.

**Browsing resolves nothing.** Listing a folder returns only already-scraped metadata —
name, size, and a coarse kind (`folder` / `video` / `image` / `other`). Cards render as
placeholder posters grouped into rails. A file's CDN link is generated **only** when you
click that specific file, and the result is cached for its ~30-minute lifetime.

**Playback is proxied.** The player points at the app's own
`/api/stream/content` endpoint, which streams the file through a zero-temp, random-access
reader with an in-RAM chunk cache. That gives the browser working seeking via HTTP
`Range` requests (206 / 416 handled) without ever exposing the CDN URL to the page.

**Three playback modes.** Clicking a file resolves its link *and* decides how to serve it:

| Mode | When it is chosen | What you get |
|---|---|---|
| `direct` | A container browsers decode — MP4, WebM, MOV, OGG | A normal `<video>`, proxied with byte ranges, so seeking works. |
| `remux` | A container browsers cannot read, holding codecs they can — H.264 video with AAC or MP3 audio | Fragmented MP4 produced live by FFmpeg, copied without re-encoding. Free. |
| `external` | Everything else — HEVC, DTS, TrueHD, … | A panel explaining the real cause, plus a **Transcode & play** button when a re-encode is possible. |

**The external panel names the cause.** "This container cannot play" is not always the
truth — a `.mkv` holding HEVC is refused because of its *codec*, and a `.mkv` on a machine
without FFmpeg is refused because the server cannot remux it. Those need different fixes,
so the panel distinguishes them and names the offending codec. The kind chip does the same,
reading `Video · HEVC` rather than a generic `Video · external`.

**Remuxing, not transcoding, is the default.** The browser's only problem with a typical
scene-release MKV is the *container*; the H.264 and AAC inside it are already playable. So
FFmpeg is asked to `-c copy` those streams into fragmented MP4 and write it to stdout, which
the server relays straight to the client. There is no encode, so the CPU cost is negligible
and playback starts in well under a second.

**Transcoding is the last resort, and it is opt-in.** A modern x265 release is HEVC, which
browsers do not decode reliably — and a remux cannot help, because it copies codecs
unchanged. For those files the panel offers **Transcode & play**, which re-encodes to H.264
and AAC live. This is the only operation in the app that costs real CPU or GPU time, so it
never starts on its own: opening a file is always free, and a click is what starts an
encode. The encoder is chosen by actually *testing* each candidate with a one-frame encode
rather than trusting `ffmpeg -encoders`, because a build lists `h264_nvenc` whether or not
the machine has an NVIDIA card. Output is pinned to 8-bit `yuv420p`, since 10-bit HEVC is
the common case and 10-bit H.264 would be a correct re-encode no browser would play.

**Neither conversion can be seeked.** Both are live conversions rather than files, so the
length is unknown: the server sends no `Content-Length` and `Accept-Ranges: none`. The
player shows a notice saying so, and reports the source duration separately. The content
endpoint still accepts a `start` offset, so a resume point can be passed in — the player
just has no scrubber to drive it.

**Nothing is written to disk.** No temp file, no cache directory, no partial copy. Media
bytes travel from the CDN through the reader to the socket — and the conversion paths are
no different: FFmpeg pipes fragmented MP4 to stdout instead of a file.

**Open in VLC.** Every video also offers **Open in VLC**, which hands the *original* file's
CDN link to the desktop player: no conversion, so subtitles, every audio track and full
seeking all survive, and no Python process sits in the middle of the stream. A web page
cannot launch a local program, so the server does it on the page's behalf — the page sends
a *path*, and the server resolves the link itself. It never accepts a URL to open, which
would otherwise make the endpoint a way to set your own media player on any address a
caller cared to name.

VLC is located through `TORBOX_VLC_PATH`, then `PATH`, then the usual install locations —
on Windows that means the registry entry the installer writes, which matters because the
installer does *not* add VLC to `PATH`, so a stock install would otherwise go undetected.
When no player is found the button is not rendered at all, rather than failing on click.

One caveat, since VLC gets the CDN link rather than the proxy: that link lives about 30
minutes, so a film much longer than that can outlive it. The proxy path exists for the
cases where that matters.

### Behaviour worth knowing

- Closing the player (Escape, the close button, or clicking the backdrop) tears the media
  element down immediately and the server stops reading. For a converted file that also
  reaps the FFmpeg process, so a closed player never leaves an encoder running.
- FFmpeg reads from the app's own content endpoint rather than the CDN URL, so a CDN link
  expiring mid-playback is handled by the reader's automatic re-resolve. A two-hour film
  keeps streaming past the link's ~30-minute life.
- If FFmpeg produces nothing for 45 seconds the server kills it, so a stalled CDN read
  cannot leave an orphaned process holding a socket.
- The encoder is probed once at startup in the background, so the first click on a
  **Transcode & play** button does not wait for the detection to finish.
- The image lightbox pages between the images in the current folder with the arrow keys.
  Each page resolves only the image it moves to.
- If a link has gone stale, one forced re-resolve and retry happens automatically.
- A second browser session is never opened: link generation is serialized behind a
  per-operation lock, so a stream resolution can slip between a download's per-file
  generations instead of waiting for the whole transfer.

### Development flag

`TORBOX_GUI_NOCACHE=1` disables the in-memory cache for `gui/*.js` and `gui/*.css`, so
frontend edits appear on reload without restarting the server. Unset it in normal use.

### Settings

Alongside the usual token and destination directory, the dashboard exposes
`browser_op_lock_timeout_s` — how long a browser operation waits for the shared lock
before failing with a clear "browser is busy" error. The default is 60 seconds.

## Tests

```bash
# Python: VFS, browser lock, endpoints, and the content proxy (real loopback server)
python -m unittest discover -s tests -p "test_*.py"

# Frontend: the Stream tab and the player, driven in jsdom
NODE_PATH=<node_modules> node tests/test_stream_ui.js
NODE_PATH=<node_modules> node tests/test_stream_player.js
```

`tests/test_integration.py` additionally requires Playwright to be importable; the rest of
the suite runs without it.

`tests/test_remux.py` covers both conversion paths — codec rules, probe parsing, command
construction, encoder preference and the playback plan, then two real end-to-end runs. Its
end-to-end classes need FFmpeg and skip without it: they build an actual H.264/AAC MKV and
an actual 10-bit HEVC/AC3 MKV with `lavfi`, serve them from a loopback range server, and
assert the outputs carry an `ftyp` box and probe back as H.264 + AAC at 8-bit. One test
exists purely to check that the endpoint does **not** transcode when it was not asked to,
since the entire cost model depends on that.

`tests/test_vlc.py` covers the desktop-player hand-off: detection order (override, `PATH`,
install locations), that a broken override is not silently ignored, that a non-HTTP URL or
a flag-shaped argument is refused before it reaches the player, that the auth token never
reaches the log, and that the endpoint resolves the link from the path rather than
accepting one from the request.

`tests/test_stream_layout.py` reads the stylesheet as text and pins the poster-rail grid
rules, so the rail cannot silently regress to a single horizontal band.

## More detail

- `DISK_IO.md` — why the HDD disk profile exists and how it reduces seek thrashing.
- `.kilo/plans/STREAMING_PAGE_IMPLEMENTATION_PLAN.md` — the design and staged rollout
  behind the Stream tab.
