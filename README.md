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

**Container support.** Browsers only decode MP4, WebM, MOV and OGG natively. Anything
else — MKV, AVI, TS, and friends — cannot be transcoded by this tool, so clicking one
opens a panel offering **Copy link**, **Open in new tab** and **Download** instead of a
broken player. Use those to hand the file to VLC or a download manager.

**Nothing is written to disk.** No temp file, no cache directory, no partial copy. Media
bytes travel from the CDN through the reader to the socket.

### Behaviour worth knowing

- Closing the player (Escape, the close button, or clicking the backdrop) tears the media
  element down immediately and the server stops reading.
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

## More detail

- `DISK_IO.md` — why the HDD disk profile exists and how it reduces seek thrashing.
- `.kilo/plans/STREAMING_PAGE_IMPLEMENTATION_PLAN.md` — the design and staged rollout
  behind the Stream tab.
