# Torbox Downloader

A web-GUI front end for [TorBox](https://torbox.app): browse your torrents, download
them to disk with a folder structure that mirrors the torrent, mount the library as a
virtual drive, and stream media straight from the browser.

By default it talks to the TorBox API directly — no browser process runs after sign-in.
A Playwright-driven browser backend is retained and can be switched back on at any time.

## How it talks to TorBox

Two backends, selected by the `fetch_backend` setting. **`http` is the default.**

### `http` (default) — the TorBox API, no browser

torbox.app is itself a client of the TorBox API at `api.torbox.app`, and its own bundle
authenticates **free** accounts against it with the Supabase session token already in
`localStorage["sb-db-auth-token"]` — a real API key is only read for paid plans. This app
takes the same path, so **no Pro plan and no API key are required.**

Three calls replace all scraping:

| Operation | Call |
|---|---|
| List torrents | `GET /v1/api/torrents/mylist?limit=1000` |
| Expand a torrent | `GET /v1/api/torrents/mylist?id=<torrent_id>` |
| Download link | `GET /v1/api/torrents/requestdl?torrent_id=&file_id=&token=` |

`mylist` already returns each torrent's full `files` array, where every entry carries its
own numeric `id`, its `size` and its torrent-root-relative `name`. Folder structure is
therefore recovered by splitting a string rather than by traversing a DOM, and a file is
addressed exactly by `(torrent_id, file_id)` instead of by guessing from its name and size.

**Only downloadable torrents are listed.** TorBox can only serve files it has actually
cached, and `requestdl` answers `HTTP 500 DATABASE_ERROR` for a torrent it has not — or no
longer has. Measured on the live API:

| `download_state` | `requestdl` |
|---|---|
| `cached` | 200, resolves |
| `completed` | 200, resolves |
| `expired` | 500 `DATABASE_ERROR` |
| `incomplete` | 500 `DATABASE_ERROR` |
| `stalled (no seeds)` | no files at all |

So anything outside `{cached, completed}` is filtered out at `scan_torrents()`, which every
surface goes through — the dashboard, the Stream tab, the drive mount and the CLI. Listing a
torrent that cannot produce a link is worse than not listing it. Set
`show_uncached_torrents` to `true` (or pass `--show-uncached`) to see the full library.

If TorBox ever renames `download_state`, the filter hides everything and logs an error
saying so, rather than leaving you with a silently empty library.

What that removes:

- No Chromium process, so no browser memory or CPU while the app runs.
- No DOM snapshot or BeautifulSoup parse per operation.
- No global lock. The browser needed one shared lock because every operation mutated the
  same page; the HTTP backend has no page, so link generation, folder expansion and stream
  resolution now run concurrently instead of queueing behind a 60-second timeout.
- Links for a whole torrent resolve in one parallel batch (`link_concurrency`, default 6)
  rather than one page interaction per file.
- Per-torrent **progress, ETA and state** come from the same response the list already
  needs, so the dashboard can show them.

Session lifetime is unchanged: TorBox signs in with Supabase, whose access token lasts
about an hour. The browser used to renew that silently, so `TokenManager` now does it
against `db.torbox.app`, collapsing concurrent callers onto a single exchange because
GoTrue *rotates* the refresh token. Each profile still owns its own session, so accounts
cannot invalidate one another.

### `browser` — the original Playwright scraper

Kept intact and reachable as an escape hatch. Set `fetch_backend` to `browser` in
`settings.json` (or pass `--fetch-backend browser`) and restart. Behaviour is as it was,
including the shared browser-op lock.

This is not a fallback that engages automatically. If the HTTP path ever fails, the error
surfaces with a log line telling you the setting to flip — one path stays live and
testable rather than two that half-work.

### What the HTTP backend does *not* do

`interactive_login` still needs a real browser: Google OAuth and Cloudflare Turnstile
cannot be driven headlessly. So `--login` and **Account → Add…** still open Chrome. That
is the only time Playwright is used, and it is why the dependency stays in
`requirements.txt`.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- `playwright install chromium` (or `firefox` / `webkit`) — **only to sign in.** The
  default fetch backend talks to the TorBox API directly and never starts a browser.

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
| `--fetch-backend` | `http` (default) calls the TorBox API with no browser; `browser` drives the site with Playwright |
| `--link-concurrency` | Parallel download-link requests on the `http` backend (default 6) |
| `--show-uncached` | Also list torrents TorBox cannot serve yet (hidden by default) |
| `--no-headless` | Show the browser window (needed for a first manual login) |
| `--token`, `-t` | Supply the `torbox_session_token` directly, skipping the login redirect |
| `--dir` | Destination for downloads (default `downloads`) |
| `--parallel-files` | How many files to download at once |
| `--disk-profile` | `auto` / `hdd` / `ssd` — disk I/O strategy |
| `--no-gui` | Run the CLI pipeline without the dashboard |
| `--profile`, `-p` | Which account profile to use (default: the last active one) |
| `--login NAME` | Open a browser, sign in once, and save that account as `NAME`, then exit |

## Accounts (profiles)

The app can hold several TorBox accounts and switch between them. One account is
active at a time and downloads never span accounts concurrently, so a single
browser session and the existing browser-op lock are shared by all of them.

**A profile is not a token.** It is the account's whole saved session, exported by
Playwright as *storage state* — cookies plus `localStorage`. That distinction is the
whole point: TorBox signs in with Supabase, whose page keeps a
`refresh_token` and silently exchanges it for a new `access_token` about every
hour. Paste a token by hand and it expires; let the app own the session and it
renews itself indefinitely.

The `http` backend reads that same file and does the hourly exchange itself, so
the storage-state format is shared by both backends and existing profiles need
no migration.

| Path | What it holds |
|---|---|
| `profiles.json` | The registry — which accounts exist and which is active |
| `profiles/<name>.json` | That account's saved session |

Both hold live credentials and both are gitignored.

Add an account from the dashboard (**Account → Add…**) or from a shell:

```bash
python main.py --login             # interactive: lists accounts, asks for a name
python main.py --login backup      # or name it up front
python main.py --profile backup    # use it from then on
```

Sign-in is a guided exchange rather than a silent wait. The shell lists the
accounts you already have, asks for a name, confirms before replacing a session
that still works, then reports progress with a running countdown while you log in
in the browser. If it times out — or you close the window — it says why and
offers to try again; `Ctrl+C` cancels cleanly. The dashboard does the same: the
modal shows an indeterminate bar and the live status, and on failure it stays
open with a **Try again** button instead of losing the name you typed.

**Cloudflare Turnstile note.** Playwright-launched browsers carry subtle CDP
fingerprints that Cloudflare's managed challenge picks up, and refuse the login
with "Verification failed" — even with real Chrome launched via
`channel="chrome"`. What works is a Chrome that Playwright never *launched*:
it only **attaches** to it over the DevTools port.

So `--login` does this automatically. It looks for an installed Chrome or Edge,
starts it as a plain subprocess with its own throwaway profile, and connects to
it over CDP:

```
chrome.exe --remote-debugging-port=9222 \
    --user-data-dir=<project>/.chrome-debug \
    --no-first-run --no-default-browser-check
```

That browser is left open afterwards so you can carry on using it; close it
when you're done. If something is already listening on 9222 the app just
attaches to that instead, so you can also start Chrome yourself:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="D:\Projects\torbox\.chrome-debug" `
  --no-first-run --no-default-browser-check
```

Only if no real browser can be found does it fall back to launching its own
Chrome / Edge / Chromium, which is the path Turnstile tends to block.
`TORBOX_CDP_URL` overrides the attach target; `.chrome-debug/` is gitignored.

The refreshed session is written back while the app runs, so accounts stay signed
in across restarts. A pre-existing `token.txt` is imported into the `default`
profile on first run, so pasting a token still works exactly as before.

Two behaviours worth knowing:

- **Switching accounts stops the browser session, unmounts any virtual drive and
  clears the queue.** A mounted drive and a queued batch belong to one account,
  so they are torn down rather than silently re-pointed at another.
- **Each account can have its own download directory.** Accounts without one fall
  back to the global path, and never inherit the previous account's.

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
- Link generation is serialized behind a per-operation lock on the `browser` backend, so a
  stream resolution can slip between a download's per-file generations instead of waiting
  for the whole transfer. On the default `http` backend there is no lock at all: there is
  no shared page to protect, so resolutions and downloads proceed concurrently.

### Development flag

`TORBOX_GUI_NOCACHE=1` disables the in-memory cache for `gui/*.js` and `gui/*.css`, so
frontend edits appear on reload without restarting the server. Unset it in normal use.

### Settings

Alongside the usual token and destination directory, the dashboard exposes:

| Setting | Purpose |
|---|---|
| `fetch_backend` | `http` (default) or `browser`. Switching drops the current session and the shared VFS, so the next operation builds the right kind of object. |
| `link_concurrency` | Parallel `requestdl` requests on the `http` backend, 1–12 (default 6). |
| `show_uncached_torrents` | Show torrents TorBox cannot serve yet (default `false`). See *How it talks to TorBox*. `http` backend only — the browser scraper never reports a `download_state` to filter on. |
| `browser_op_lock_timeout_s` | How long a *browser* operation waits for the shared lock before failing with "browser is busy". Default 60 s. Ignored on the `http` backend, which passes no lock. |

**Rollback:** set `fetch_backend` to `browser` in `settings.json` and restart. No code
change, no reinstall.

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

`tests/test_profiles.py` covers the account store: name validation (a profile name
becomes a filename, so path traversal is rejected), wrapping a Supabase session as
storage state, the save/load round trip, and migration of a legacy `token.txt`.

`tests/test_api_session.py` covers the HTTP backend against an injected fake transport and
a temp directory: path derivation, the id-0 file that a truthiness check would drop, the
single-flight refresh under 20 threads, one-retry-on-401, and profile write-back
preserving cookies and unrelated `localStorage` keys. No network, no browser.

`tests/test_pipeline_links.py` covers the batched link producer: one prefetch call for a
whole torrent, exact ids in the batch, per-file retry only for stragglers, one call per
file on the browser backend, and cancellation.

`tests/test_browser_lock.py` covers both sides of the lock: mutual exclusion and
`BrowserBusyError` when a lock is supplied, and overlap plus `file_id` plumbing when
`None` is supplied.

`tests/test_queue_update.py` covers the rescan path: a torrent whose cache expires
mid-transfer keeps its queue row and its live progress instead of being dropped, terminal
rows are cleared normally, the name→index map stays consistent, and
`trigger_torrent_scan()` cannot be called from inside `app_state.lock`.

## More detail

- `DISK_IO.md` — why the HDD disk profile exists and how it reduces seek thrashing.
- `HTTP_FETCH_ARCH_IMPLEMENTATION_PLAN.md` — the design, evidence and staged rollout
  behind the browser-free fetch backend.
- `.kilo/plans/STREAMING_PAGE_IMPLEMENTATION_PLAN.md` — the design and staged rollout
  behind the Stream tab.
