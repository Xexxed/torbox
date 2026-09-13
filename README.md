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
in the Stream tab, so MKV and friends can play in the browser. Without it those files
fall back to the external-player panel. Nothing else in the app depends on it.

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
| `remux` | A container browsers cannot read, holding codecs they can — H.264 video with AAC or MP3 audio | Fragmented MP4 produced live by FFmpeg, copied without re-encoding. |
| `external` | Everything else — HEVC, DTS, TrueHD, … | A panel with **Copy link**, **Open in new tab** and **Download**, to hand to VLC or a download manager. |

**The external panel names the cause.** "This container cannot play" is not always the
truth — a `.mkv` holding HEVC is refused because of its *codec*, and a `.mkv` on a machine
without FFmpeg is refused because the server cannot remux it. Those need different fixes,
so the panel distinguishes them: it names the offending codec (HEVC, DTS, …) and explains
that remuxing copies codecs unchanged and therefore cannot rescue such a file. The kind
chip does the same, reading `Video · HEVC` rather than a generic `Video · external`.

**Remuxing, not transcoding.** The browser's only problem with a typical scene-release MKV
is the *container*; the H.264 and AAC inside it are already playable. So FFmpeg is asked to
`-c copy` those streams into fragmented MP4 and write it to stdout, which the server relays
straight to the client. There is no encode, so the CPU cost is negligible and playback
starts in well under a second. Files whose codecs are not browser-safe are **not**
re-encoded — that would burn CPU for a quality loss, and it is a deliberate non-goal.

**A remuxed stream cannot be seeked.** Because it is a live conversion rather than a file,
its length is unknown, so the server sends no `Content-Length` and `Accept-Ranges: none`.
The player shows a notice saying so and reports the source duration separately. The content
endpoint still accepts a `start` offset, so a resume point can be passed in — the player
just has no scrubber to drive it.

**Nothing is written to disk.** No temp file, no cache directory, no partial copy. Media
bytes travel from the CDN through the reader to the socket — and the remux path is no
different: FFmpeg pipes fragmented MP4 to stdout instead of a file.

### Behaviour worth knowing

- Closing the player (Escape, the close button, or clicking the backdrop) tears the media
  element down immediately and the server stops reading. For a remuxed file that also
  reaps the FFmpeg process.
- FFmpeg reads from the app's own content endpoint rather than the CDN URL, so a CDN link
  expiring mid-playback is handled by the reader's automatic re-resolve.
- If FFmpeg produces nothing for 45 seconds the server kills it, so a stalled CDN read
  cannot leave an orphaned process holding a socket.
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

`tests/test_remux.py` covers the remux path — codec rules, probe parsing, command
construction, the mode decision, and a real end-to-end remux. Its end-to-end class needs
FFmpeg and skips itself without it: it builds an actual H.264/AAC MKV with `lavfi`, serves
it from a loopback range server, and asserts the output carries an `ftyp` box and probes
back as MP4.

`tests/test_stream_layout.py` reads the stylesheet as text and pins the poster-rail grid
rules, so the rail cannot silently regress to a single horizontal band.

## More detail

- `DISK_IO.md` — why the HDD disk profile exists and how it reduces seek thrashing.
- `.kilo/plans/STREAMING_PAGE_IMPLEMENTATION_PLAN.md` — the design and staged rollout
  behind the Stream tab.
