# HTTP Fetch Architecture: replace headless-browser scraping with direct TorBox HTTP calls

## Implementation outcome

Implemented on `New-fetch-Arch` in four commits. Stages 1–5 and the post-implementation
items are done; Stage 6 (`user/refreshtoken`, which rotates the account's API key) was
deliberately **not** run, and Stage 7 (deleting the browser backend) is not started.

### Measured, on the real free account

Same 218-file torrent, same machine, back to back. Browser figures for link generation are
sampled over 5 files and extrapolated; the HTTP figure in the table is *sequential*, so it
is a conservative floor — the downloader uses the 6-worker batch measured at 51 ms/file.

| Stage | HTTP | Browser | Ratio |
|---|---|---|---|
| session start | 0.000 s | 2.199 s | — |
| `scan_torrents` | 0.63 s | 4.45 s | 7x |
| `fetch_structure` (218 files) | 0.16 s | 0.92 s | 6x |
| download link, sequential | 0.55 s | 2.06 s | 4x |
| download link, as the downloader runs it (batched) | 0.051 s | 2.06 s | 40x |
| all 218 links | 11.1 s | ~449 s | 40x |
| **RSS growth** | **+6 MB** | **+717 MB** | **120x** |

The memory figure is the answer to the question this plan was written for. Everything after
sign-in is a `requests` session.

### Verified end to end

- 292 tests pass, no network required except where a test opts in.
- GUI server on the `http` backend: scan, 218-file structure, link generated and CDN `HEAD`
  200, VFS link resolved, rescan, settings switch — and `playwright` never enters
  `sys.modules`.
- A real download through `main.run_downloader`: 175,335 bytes written, matching the API's
  `size_bytes` exactly, at exactly the folder path the API reported, in 2.3 s.
- GUI server on the `browser` backend: `TorboxBrowserSession` built, op lock retained,
  scanned and expanded 218 files, returned no `file_id`; switching back to `http` at runtime
  swapped the session and rebuilt the VFS.

### Divergences from the plan

1. **`viewing_torrent_name()` returns a dict, not a string.** §2.7 said it would return
   `current_torrent_name`. It returns `{"torrent", "on_download_view"}` because the
   condition it replaces was two-part — `session.current_torrent_name != torrent_name`
   *and* `"download" not in session.page.url` — and the second half can only be read on the
   Playwright worker thread. Both backends now answer both halves on their own thread.
2. **The anon key is tried without network.** §1.5 said to re-derive it on first call,
   which put a network round trip in startup and made the key untestable offline. It is now
   seeded, returned offline, and re-derived only after GoTrue rejects it with 400/401/403.
3. **The `fetch_lock` guards in `/api/fetch_structure` and `/api/generate_link` were left
   alone.** The plan removed the `browser_op` lock but said nothing about these. They are
   request-level double-submit guards, and at ~50 ms per call their serialization costs
   nothing. Removing them would change 429 semantics for no gain.
4. **`format_size` never emits TB.** `downloader.parse_size_to_bytes` only understands
   GB/MB/KB/B, so a 2 TB torrent reports as ~2048 GB. Documented on the function.
5. **`_is_http_session` uses `callable()`, not `hasattr()`.** A session that names
   `prefetch_links` without implementing it would otherwise be mistaken for the HTTP
   backend. Found by a test, not by inspection.
6. **No `--target` CLI flag was added.** The CLI has no way to select a torrent, so
   verifying the CLI download path meant driving `run_downloader` in-process with
   `selected_files`. Adding the flag would be a feature, not part of this refactor.

### Findings that were not in the plan

- **TorBox ships files with `id: 0`.** At least one file in a real 218-file torrent has
  `id: 0`, and a truthiness check silently dropped a 1.79 GB file from the tree. `file_id=0`
  addresses that single file, not the whole torrent — only `zip_link=true` does that — which
  was confirmed by inspecting the returned `Content-Disposition` and length. Ids are now
  never tested for truthiness, and `test_structure_keeps_a_file_whose_id_is_zero` covers it.
- **`requestdl` returns HTTP 500 `DATABASE_ERROR` for torrents that are not cached.**
  `cached` and `completed` torrents resolve cleanly; `expired`, `incomplete` and
  `stalled (no seeds)` ones do not. This is server-side and the browser backend hits it
  identically, so it is not a regression — but 5xx bodies now carry the server's own
  `error`/`detail` text, because an opaque status code told the user nothing.
- **`fetch_structure` matches torrent names exactly.** `run_downloader` filters by
  substring and then passes the exact name, so the two agree; recorded because it is an
  interface fact a caller could trip over.

### Still outstanding

- Stage 6: `POST /v1/api/user/refreshtoken` for a long-lived API key. **Mutates the
  account by rotating its API key, so it needs explicit approval and was not run.**
- Stage 7: deleting `browser_session.py` and `detector.py` and demoting Playwright to an
  optional login-only extra. Deliberately deferred until the HTTP path has been used in
  anger.
- The free-plan JWT path is undocumented by TorBox. If they close it, the HTTP backend
  stops working and `fetch_backend=browser` is the only recourse. This risk is unchanged by
  anything in the implementation and cannot be engineered away.

## Scope and evidence

Branch: `New-fetch-Arch`. This document is an implementation plan, not a claim that it is implemented or tested.

**Files reviewed:** `main.py` (634 L), `gui_server.py` (1872 L), `torbox_downloader/browser_session.py` (1063 L), `torbox_downloader/detector.py` (330 L), `torbox_downloader/creator.py`, `torbox_downloader/profiles.py` (385 L), `torbox_downloader/browser_lock.py` (67 L), `torbox_downloader/mount/vfs.py` (634 L), `torbox_downloader/mount/streaming.py`, `torbox_downloader/mount/manager.py`, `torbox_downloader/mount/dav_provider.py`, `torbox_downloader/mount/winfsp_fs.py`, `gui/app.js`, `requirements.txt`, `tests/*`. No `AGENTS.md`; no `pyproject.toml` or lock file.

**Established by reading the code:**

- All Playwright usage is confined to `browser_session.py`. The rest of the app consumes exactly five methods: `scan_torrents`, `fetch_structure`, `generate_download_link`, `go_to_dashboard`, `update_token` — plus `get_page_content` from `main.py:335` — and `interactive_login` for onboarding. That is the entire seam.
- `browser_session.py:872-882` already steals the CDN URL from a `requestdl` XHR response body (`body["data"]`), so the browser is acting as an authenticated HTTP proxy for an endpoint we can call directly.
- `profiles.py:337 extract_session()` already returns the Supabase session dict (`access_token` + `refresh_token`) from the existing profile file, so no profile migration is required.
- `gui_server.py:518-530 update_queue()` hardcodes `progress: 0` and `speed: "0 KB/s"`; `gui/app.js:1200-1204` already renders `.torrent-status .dot` and `.torrent-percent`. The GUI scaffolding for progress exists and is unwired.

**Established by live measurement against the user's own free account** (probes in `%TEMP%\kilo\probe_api.py`, `probe2.py`, `probe3.py`, `bench_http.py`):

- The live SPA bundle `torbox.app/assets/index-38a155b4.js`, function `Vt` (byte offset 1179966), resolves the `api.torbox.app` bearer as: `if (n.plan === 0) return session.access_token` — i.e. **free accounts send the Supabase session JWT**; the `api_tokens` table (a real API key) is read only for paid plans. The website itself depends on this path.
- `GET /v1/api/torrents/mylist` with that JWT → `200 success:true`, 6 torrents, 373 files.
- `GET /v1/api/torrents/mylist?id=N` → `200`, and `data` is a **dict** (not a list) carrying `files[]`, `progress`, `eta`, `download_state`, `download_speed`.
- `files[]` items carry `{id, hash, name, size, s3_path, short_name, absolute_path, mimetype, zipped, infected}`. `name` is the torrent-root-relative path; `s3_path` and `absolute_path` confirm the same folder layout. **No DOM parsing is needed to recover folder structure.**
- `GET /v1/api/torrents/requestdl?torrent_id=&file_id=&token=` → `200`, CDN URL on `*.tb-cdn.io`; `HEAD 200`, ranged `GET 206`, `accept-ranges: bytes`.
- `POST https://db.torbox.app/auth/v1/token?grant_type=refresh_token` with the public anon key from the bundle → `200`, new 1 h access token, refresh token rotates.
- Timing: full library scan 3.1 s cold / 220 ms warm; structure build 0 ms; `requestdl` 54 ms/file at 8 workers, 210 ms sequential; **all 218 files of the largest torrent linked end-to-end in 14.9 s**.

**Explicitly NOT established:**

- No measurement of the current browser path's wall-clock or RSS was taken at the time of
  review. **Since implemented** — see *Implementation outcome* above, which records both
  backends measured back to back on the same 218-file torrent.
- Rate-limit behaviour under sustained load was not tested. 40 parallel `requestdl` calls succeeded, which is far below the documented 300/min ceiling.
- The `anon` key was extracted from the live bundle today. Whether TorBox rotates it, and how a rotation would present, is unknown.
- Whether the free-plan JWT path stays open is not something TorBox documents or guarantees.
- `POST /v1/api/user/refreshtoken` (documented, returns a long-lived API key) was **not** called, because it mutates the account by rotating the API key. It is recorded as an optional Stage 6 experiment requiring explicit approval.

## Goals

- Replace every scrape with a direct HTTPS call; no browser process may exist during normal operation.
- Preserve the existing five-method session contract so `main.py`, `mount/vfs.py`, `gui_server.py`, `winfsp_fs.py` and `dav_provider.py` require no signature changes.
- Eliminate the global `browser_op` serialization for the HTTP backend so link generation, folder expansion, and stream resolution run concurrently.
- Identify files by `(torrent_id, file_id)` instead of by name-and-size matching.
- Surface real per-torrent `progress` and `eta` in the GUI.
- With `fetch_backend=browser`, behavior must be identical to today.
- Add no dependency. `requests` is already required.

## Decisions (resolved with the user)

1. **Playwright is retained for `interactive_login` only.** Google OAuth and Cloudflare Turnstile cannot be driven without a browser. It must not run during scan, structure, link, stream, mount or download. *(User-confirmed.)*
2. **HTTP is the default backend; the browser backend stays reachable behind a `fetch_backend` setting (`http` | `browser`).** Rollback is a settings change, not a revert. The browser backend is deleted only after the HTTP path is confirmed on the real account. *(User-confirmed.)*
3. **Link generation defaults to 6 concurrent `requestdl` requests**, configurable. Chosen to stay well under the documented 300/min ceiling while covering the 54 ms/file measurement. *(User-confirmed.)*
4. **Per-torrent `progress` and `eta` are added to the GUI**, not just the API. *(User-confirmed.)*
5. **A Pro API key is not required and will not be used.** Verified: the free-plan path authenticates through the Supabase session JWT, which is what the TorBox website itself does for free accounts.
6. **`size` stays a human-readable string; `size_bytes` is added alongside it.** The existing contract is string-typed in at least six places, and rewriting them all is out of scope. *(Conflict handling: extend the existing design.)*
7. **`selector` is retained as a key but always `None` for the HTTP backend.** Consumers keep reading it; nothing breaks. *(Conflict handling: extend.)*
8. **No automatic browser fallback at runtime.** On HTTP failure the error surfaces with a log hint to flip `fetch_backend`. Keeps one code path live and testable. *(Conflict handling: isolate.)*

## Route selection

| | A: Parallel session class behind a setting | B: Extract a `Session` protocol, then two adapters | C: Rewrite `browser_session.py` in place |
|---|---|---|---|
| Diff size | Small — one new module + wiring | Large — every call site retyped | Smallest |
| Browser fallback survives | Yes, free | Yes | No |
| Honesty of naming | Good | Good | Bad (class named `Browser*` doing HTTP) |
| Risk | Low | Medium (wide signature churn) | Medium-high, no rollback |
| Removes the op lock cleanly | Yes | Yes | Awkward |

**Chosen: Route A.** The existing five-method contract is already exactly the right seam, so a sibling class implementing it is the smallest change that still yields a one-setting rollback. Route B is better long-term architecture but retypes every call site for no functional gain here. Route C is rejected outright: it makes rollback impossible and leaves a class called `TorboxBrowserSession` issuing HTTPS requests.

## Target architecture

```
                       fetch_backend setting (settings.json)
                                     |
                 +-------------------+-------------------+
                 |                                       |
         "http" (default)                          "browser"
                 |                                       |
      +----------v-----------+              +------------v-------------+
      |  TorboxApiSession    |              | TorboxBrowserSession    |
      |  (requests only)    |              | (Playwright, unchanged)  |
      +----------+-----------+              +------------+-------------+
                 |                                       |
      +----------v-----------+                           |
      | TokenManager         |                           |
      |  - JWT exp check    |                           |
      |  - GoTrue refresh    |                           |
      |  - write back to     |                           |
      |    profiles/<n>.json |                           |
      +----------+-----------+                           |
                 |                                       |
      +----------v---------------------------------------v---------+
      |            shared five-method session contract            |
      |  scan_torrents / fetch_structure / generate_download_link |
      |  go_to_dashboard / update_token                           |
      +----------+-----------------------------------+------------+
                 |                                   |
      +----------v-----------+          +------------v------------+
      | main.py              |          | mount/vfs.py             |
      |  run_downloader     |          |  _expand / _resolve_node |
      |  pipeline           |          |  winfsp + dav + stream   |
      +----------------------+          +-------------------------+
```

Data flow for one file, HTTP backend:

```
scan_torrents()      GET mylist?limit=1000        -> [{name, size, progress, eta, ...}]
fetch_structure(n)   GET mylist?id=N              -> files[] with {file_id, torrent_id,
                                                       name, relative path, size}
                      path = [torrent.name] + dirname(file.name)     # 0 ms, pure string ops
generate_download_link(...)  GET requestdl?torrent_id&file_id&token  -> CDN url
                      6x ThreadPoolExecutor, one requestdl each
```

## Invariants

1. Never import `playwright` at module scope in any file on the HTTP code path. `main.py:19-23` currently `sys.exit(1)` when it is missing and must stop doing so.
2. Never hold the `browser_op` lock when `fetch_backend == "http"`. HTTP operations are thread-safe and must not serialize.
3. At most one GoTrue refresh may be in flight per profile. A second caller must wait for the first, never issue a second refresh (the refresh token rotates, so a concurrent refresh invalidates the other).
4. Every token refresh must be written back to the profile before the rotated refresh token is used again, so a crash cannot lose it.
5. A link is identified by `(torrent_id, file_id)`. Never match files by name or size.
6. `path[0]` of every file entry is always the torrent name, and `size` is always a string. `mount/vfs.py:248` strips `segments[0]` on this assumption and `main.py:370` matches `"/".join(path + [name])` against it.
7. Every failure of the HTTP backend must surface as a normal exception with an actionable message. Never silently fall back to the browser.
8. Never delete or overwrite a profile file without a successful atomic write. Reuse `ProfileStore._atomic_write` (`profiles.py:106`).
9. With `fetch_backend == "browser"`, no HTTP module may be imported at all.
10. Never send more than the configured concurrency of `requestdl` requests, and never exceed the documented rate limit across the whole process.

## 1. Token and session management

**Files:** new `torbox_downloader/api_session.py`; reuses `torbox_downloader/profiles.py`.

**Current constraints:** `profiles.py:13-18` states the design intent — the browser performs the refresh itself, so the app never parses tokens. `gui_server.py:665-672` documents that `app_state.token` goes stale within an hour and that the session is the source of truth. `browser_session.py:443-462 save_state()` persists the browser-refreshed state at most every 60 s.

**Changes:**

1. Add a `TokenManager` holding `(access_token, refresh_token, expires_at, profile_name)`.
2. Load the initial session with `profiles.extract_session(ProfileStore().load_state(name))` — reuse, do not reimplement the localStorage walk. Accept a raw pasted token by reusing `profiles.session_to_storage_state` first.
3. `ensure_valid()` must decode the JWT payload without signature verification (base64url-decode segment 2, read `exp`) and refresh when fewer than 300 s remain. Reuse the decoding approach already in `profiles.py:371-385 extract_expiry` — prefer calling it over duplicating.
4. Perform the refresh with `requests.post("https://db.torbox.app/auth/v1/token?grant_type=refresh_token", headers={"apikey": anon}, json={"refresh_token": ...})`.
5. Resolve the anon key through `resolve_anon_key()`: return the cached value if present; otherwise `GET https://torbox.app/`, regex `src="/assets/index-[^"]+\.js"`, fetch that bundle, regex `VITE_SUPABASE_KEY:"([^"]+)"`, cache it. Seed the cache with the key observed on 2026-09-28 so a bundle change does not block startup, and log a warning when a fetch-derived key is used because it means the seed is stale.
6. On successful refresh, write the rotated session back into the profile storage state, **preserving every other key and all cookies** — mutate only the `sb-db-auth-token` and `torbox_session_token` entries — via `ProfileStore.save_state`. Never write a partial file.
7. Guard refresh with a `threading.Lock` plus a "refresh in progress" event so concurrent callers await one refresh (Invariant 3). Hold the lock only across the HTTP call, never across a download.
8. On a `401`, invalidate the cached token once and retry exactly once. On a second `401`, raise an error whose message contains the existing sentinel `"session token has expired or is invalid"` so `gui_server.py:686` and `main.py`'s callers keep matching it.
9. Do not call `POST /v1/api/user/refreshtoken`. It rotates the account's API key and requires explicit approval.

**Why:** The browser's only real contribution to auth is the hourly GoTrue exchange. Owning it directly removes the browser and keeps multi-account isolation intact, because each profile still owns a distinct refresh token.

**Acceptance criteria:**

- [ ] A profile with an expired access token refreshes and succeeds without a browser.
- [ ] Refreshed tokens are visible in `profiles/<name>.json` afterwards, with cookies and unrelated localStorage keys intact.
- [ ] Twenty threads calling `ensure_valid()` on an expired token produce exactly one refresh request.
- [ ] A `401` triggers exactly one retry, and a second `401` raises the sentinel-matching message.
- [ ] `resolve_anon_key()` returns a key without network access when seeded, and re-derives it when the seed fails against GoTrue.

## 2. The HTTP session

**Files:** new `torbox_downloader/api_session.py`.

**Current constraints:** The contract is `browser_session.py:250-270`. `gui_server.py:1560` reads `session.page.url` and `session.current_torrent_name` from an HTTP handler thread, which is both a Playwright thread-affinity violation and meaningless for HTTP.

**Changes:**

1. Implement `TorboxApiSession` with the same five methods plus `current_torrent_name`, `start()`, `stop()`, and `update_token(new_token)`.
2. `scan_torrents()` must call `GET /v1/api/torrents/mylist` with `limit=1000` and `bypass_cache=false`, and return one dict per torrent containing `name`, `size` (formatted string), `size_bytes`, `progress` (percent, `int(progress * 100)`), `eta`, `download_state`, `download_speed`, `download_finished`, and `id`. Sort by descending `id` to match the observed server order.
3. `fetch_structure(name)` must resolve the torrent by name from a cached scan, call `GET mylist?id=<id>`, and map each file to `{name, path, size, size_bytes, selector=None, torrent_id, file_id}`, where `name = file["short_name"] or rel.rsplit("/", 1)[-1]` and `path = [torrent_name] + rel.split("/")[:-1]`. Do not use `s3_path`; it is prefixed with the content hash and would create a spurious top-level folder.
4. Cache the per-torrent file list so a `generate_download_link` call does not re-fetch. Never cache across a `refresh()`.
5. `generate_download_link(name, size, selector)` is browser-shaped, so it must accept a `file_id` via a new optional keyword and prefer it. When `file_id` is absent, resolve it from a name-and-size lookup against the cached structure as a compatibility path for `main.py:155`, and log once that the compat path is in use.
6. `update_token(new_token)` must accept a raw JWT or a session dict and reset the `TokenManager`.
7. Add `viewing_torrent_name()` returning `current_torrent_name`, and change `gui_server.py:1560` to use it instead of touching `session.page.url`. This removes the cross-thread Playwright read.
8. Provide a `prefetch_links(file_refs)` helper that resolves many `(torrent_id, file_id)` pairs through a `ThreadPoolExecutor` at the configured concurrency (default 6) and returns a dict. Reuse it from the downloader path in Stage 3.
9. `stop()` must be a no-op. There is nothing to tear down beyond closing the `requests.Session`.
10. Use one module-level `requests.Session` with a connection pool sized to the concurrency. Do not create a session per call.

**Why:** This class is the only new thing standing between the app and TorBox. Keeping the contract identical is what makes the toggle a one-line change.

**Acceptance criteria:**

- [ ] `scan_torrents()` returns the same `name` and `size` strings the detector produced, plus the new fields.
- [ ] `fetch_structure()` returns `path[0] == torrent name` for every entry (Invariant 6).
- [ ] `generate_download_link` with an explicit `file_id` issues exactly one `requestdl` call.
- [ ] No module in the HTTP path imports `playwright`.
- [ ] `viewing_torrent_name()` replaces every `session.page` access outside `browser_session.py`.

## 3. Wiring, settings and the op lock

**Files:** `gui_server.py`, `main.py`, `settings.json` handling.

**Current constraints:** `AppState.browser_op_lock` (`gui_server.py:120`, aliased `fetch_lock`) is passed to every operation and defaults to a 60 s timeout (`browser_lock.py:22`). `mount/vfs.py:235-239` catches `BrowserBusyError` to distinguish "retry" from "broken". `main.py:19-23` hard-exits without Playwright. `main.py:284-292 get_torrent_files_structure` is imported at `gui_server.py:1507` and never called.

**Changes:**

1. Add a `fetch_backend` setting, default `"http"`, validated against `("http", "browser")` in the `/api/settings` handler.
2. Add a `link_concurrency` setting, default 6, clamped to 1..12.
3. `get_or_create_session()` must construct `TorboxApiSession` when the backend is `http` and `TorboxBrowserSession` when it is `browser`. Log which backend was chosen at INFO on every construction.
4. `AppState` must hand `browser_op_lock = None` to callers when the backend is `http`, so `browser_op` becomes the documented no-op at `browser_lock.py:39-41`. This is the change that actually removes the serialization (Invariant 2). `mount/vfs.py` needs no edit — it already passes whatever lock it was given.
5. Change `main.py:19-23` from a hard `sys.exit(1)` to a lazy import inside the browser-only paths, so an HTTP-mode run does not require Playwright at all.
6. Delete the dead `get_torrent_files_structure` (`main.py:284-292`) and its unused import at `gui_server.py:1507`.
7. Remove the `torbox_debug.html` write at `main.py:434-437`; it captures a DOM that no longer exists. Keep an equivalent diagnostic that logs the failing torrent id and the API error text.
8. Persist `fetch_backend` and `link_concurrency` in `settings.json` through the existing save path, and log the value at startup.

**Why:** The lock is the single biggest source of the reported overhead — every link, every folder expansion and every stream resolution queues behind it. On the HTTP path it protects nothing.

**Acceptance criteria:**

- [ ] With `fetch_backend=http`, `browser_op` is never acquired (assert via a lock probe in tests).
- [ ] With `fetch_backend=browser`, a stream-page resolve still interleaves between per-file link generations exactly as today.
- [ ] `main.py` runs with `playwright` absent when the backend is `http`.
- [ ] Toggling the setting requires no code change and no restart of the download path.

## 4. Downloader and mount consumers

**Files:** `main.py`, `torbox_downloader/creator.py`, `torbox_downloader/mount/vfs.py`, `torbox_downloader/mount/streaming.py`.

**Current constraints:** `main.py:335-337` parses the current page first, which only makes sense with a browser. `main.py:146-160` retries link generation 3 times with a 2 s sleep while holding the op lock. `main.py:60,84` and `vfs.py:208` call `parse_size_to_bytes` on a string. `vfs.py:261-267` stores `selector` on `FileEntry`. `vfs.py:33 CDN_LINK_TTL_S = 1800`.

**Changes:**

1. In `main.py:332-341`, when the backend is `http`, skip the `get_page_content()` / `TorboxDetector` probe entirely and go straight to `scan_torrents()`. The "parse the current page" heuristic has no meaning without a page.
2. `main.py:335-337` and `main.py:424-426` keep their current behavior under the browser backend.
3. `main.py:155` must pass `file_id` and `torrent_id` to `generate_download_link`. Under the browser backend, fall back to the existing `(name, size, selector)` call.
4. Replace the serial 3-try loop at `main.py:146-160` with a bounded parallel prefetch when the backend is `http`: use `TorboxApiSession.prefetch_links` at the configured concurrency, keep the existing retry count for individual failures, and preserve the existing cancellation checks against `cancel_event` before each request.
5. Do not change `parse_size_to_bytes` call sites. Add `size_bytes` alongside `size` and prefer it only where a new comparison is needed.
6. `vfs.py:261-267` must additionally store `torrent_id` and `file_id` on `FileEntry`, and `vfs.py:436` must pass them to `generate_download_link` so stream and mount resolution becomes exact instead of name-and-size matched. Default both to `None` so browser-built trees still work.
7. Leave `CDN_LINK_TTL_S` and the `LinkExpiredError` re-resolve loop in `streaming.py:128-139` as they are. The measurement showed 3 h link validity, so 1800 s stays conservative.
8. `creator.py` requires no change: it consumes `name` and `path` only.

**Why:** Consumers above the seam should not know which backend they are talking to, and the only reason they would is if the seam leaked browser-shaped arguments.

**Acceptance criteria:**

- [ ] `main.py` with the HTTP backend never calls `get_page_content` or `TorboxDetector`.
- [ ] A torrent of 200+ files links and starts downloading without the 60 s op lock being contended.
- [ ] Stream resolution of a file resolves to the same file the downloader resolved, verified by `file_id` equality.
- [ ] `creator.py` and `downloader.py` are byte-for-byte unchanged.

## 5. GUI progress

**Files:** `gui_server.py`, `gui/app.js`.

**Current constraints:** `gui_server.py:518-530 update_queue()` hardcodes `progress: 0` and `speed: "0 KB/s"`. It is called both with torrent dicts (`gui_server.py:676`) and with file dicts (`gui_server.py:725`). `gui/app.js:1196-1204` renders name, size, a status dot and a percent span, and `app.js:1220-1226` updates them in place.

**Changes:**

1. `update_queue()` must read `progress`, `eta` and `download_state` from the entry when present, and fall back to the current hardcoded defaults when absent, since file dicts carry none of them.
2. Add `eta` to the queue entry so the UI can show it.
3. `gui/app.js`: render `eta` next to the percent, and map `download_state` to the existing status dot. Keep the markup stable for `app.js:1256` (`updateQueue` diffing) and `app.js:1220-1226` (in-place update) so no further front-end refactor is needed.
4. The percent must be rendered as an integer 0..100; the API returns 0..1.
5. Leave the manual rescan button (`app.js:797-808`) and `/api/refresh_torrents` alone.

**Why:** The data arrives in the same response that lists torrents, so the GUI cost is a few lines and the existing markup already supports it.

**Acceptance criteria:**

- [ ] A completed torrent shows 100% and a `cached` dot; an in-progress one shows its real percent and ETA.
- [ ] `update_queue()` called with file dicts produces the current default output, unchanged.
- [ ] No front-end refactor beyond the progress/ETA/dot fields.

## 6. Tests and diagnostics

**Files:** new `tests/test_api_session.py`; updates to `tests/test_mount.py`, `tests/test_shared_vfs.py`, `tests/test_browser_lock.py`, `tests/test_detector.py`.

**Current constraints:** Tests are `unittest` (`tests/test_profiles.py:1-4`, `tests/test_detector.py:1-2`); `pytest` is not installed, so the suite runs under `python -m unittest discover tests`. `tests/test_browser_session.py` fakes `session.page`. `tests/test_integration.py:114-172` is the only test that launches a real browser.

**Changes:**

1. `tests/test_api_session.py` must inject a fake transport — no network, no real profile files. Cover: path derivation for nested and flat torrents, `short_name` fallback, `selector is None`, `size` string formatting, `mylist?id=` returning a dict, `requestdl` parameter construction, one-retry-on-401, sentinel message on second 401, and concurrent `ensure_valid()` producing one refresh.
2. Use `tempfile.mkdtemp` and a `ProfileStore(base_dir=tmp)` per test, and tear down in `tearDown`. Never point a test at the real `profiles/` directory.
3. If a test must change an environment variable, snapshot the original and restore it in `finally`, asserting restoration even on failure.
4. Extend the `FakeSession` doubles in `tests/test_mount.py:55`, `tests/test_shared_vfs.py:19` and `tests/test_browser_lock.py:27` with `file_id`/`torrent_id` passthrough and `viewing_torrent_name()`.
5. Add a test asserting `browser_op` is a no-op when the lock is `None`, which is the mechanism behind Invariant 2.
6. Keep `tests/test_detector.py` and `tests/test_integration.py` exactly as they are; they cover the browser backend, which is being retained.
7. Add a `--probe` style diagnostic that logs, for one torrent, the time and result count of each stage, so a regression can be attributed to scan, structure, or link generation.

**Why:** The HTTP backend is a new network surface with no browser to make failures obvious. Isolation and fake transports are what make it testable at all.

**Acceptance criteria:**

- [ ] `python -m unittest discover tests` passes with no network access.
- [ ] No test reads or writes the real `profiles/` directory or `settings.json`.
- [ ] The browser-backend tests still pass unchanged.

## Failure modes and fallbacks

| Situation | Procedure |
|---|---|
| Access token expired mid-download | Refresh transparently, retry the single request once. No user-visible error. |
| Refresh token also expired | Raise the sentinel `"session token has expired or is invalid"`; `gui_server.py:686` routes to `handle_token_expired()`. Never retry-loop. |
| GoTrue unreachable at startup | Log the failure with the profile name, raise. Do not fall back to the browser (Invariant 7). |
| `mylist` returns HTTP 5xx or times out | Retry at most 3 times with 0.5/1/2 s backoff, then raise. A scan that cannot reach the network must not empty the existing queue. |
| `mylist` returns an empty list | Log a warning distinguishing "no torrents" from "auth lost" by checking the response `success` flag. Keep the previous queue. |
| Single `requestdl` returns `success: false` | Retry that file up to 3 times with 1 s backoff, honoring `cancel_event` before each attempt. Mark only that file failed. |
| Rate limited (HTTP 429) | Global pause of 60 s, then one retry. Never exceed the configured concurrency. |
| CDN link expires during streaming | Existing `LinkExpiredError` path in `streaming.py:128-139` forces one re-resolve. Unchanged. |
| Torrent deleted server-side mid-session | `mylist?id=` returns no data. Raise `VfsNotFound`-compatible error; drop the torrent from the tree rather than leaving a broken node. |
| `file_id` missing (browser-built tree) | Fall back to name-and-size lookup and log once. Never fail the resolve. |
| Crash mid-download | Unchanged: partial files resume by size via `is_file_size_matching` at `main.py:57-60`. No destructive cleanup is added. |
| User cancels a download | `cancel_event` is checked before every `requestdl`, preserving `main.py:128,147`. The executor drains and exits. |
| Profile file unwritable | Log and continue with the in-memory token. Never crash the download for a persistence failure. |
| HTTP backend appears broken in the field | Set `fetch_backend=browser` in `settings.json` and restart. No code change, no revert. |

## Smaller but definite fixes / considerations

| Location | Problem | Required change |
|---|---|---|
| `main.py:19-23` | Hard `sys.exit(1)` when Playwright is missing, though `main.py` never uses it | Make the import lazy and browser-only |
| `main.py:284-292` | `get_torrent_files_structure` is dead — imported at `gui_server.py:1507`, never called | Delete both |
| `browser_session.py:152-184` | `is_token_expired` has no caller | Leave it; it belongs to the retained browser backend |
| `gui_server.py:1560` | Reads `session.page.url` from a request thread — a Playwright thread-affinity violation | Use `viewing_torrent_name()` |
| `main.py:60,84`, `vfs.py:208` | `parse_size_to_bytes` on a string at six sites | Leave unchanged; add `size_bytes` beside it |
| `creator.py:6-20` | `os.path.join` with unsanitized `path` segments; `vfs.py` sanitizes but the downloader path does not | Note only. Out of scope — the new path strings are derived from server data exactly as the DOM text was |
| `browser_session.py:35-48` | `_launch_engine` maps `chrome`/`msedge`/engine names | Leave unchanged; browser backend is retained |
| `README.md:7-9` | States that everything happens through a real signed-in session rather than an API | Rewrite to describe the HTTP backend and the login-only browser |

## Non-goals and shortcomings

- **No Pro API key, and no `user/refreshtoken`.** The free-plan JWT path works today but is undocumented by TorBox. If TorBox closes it, the HTTP backend stops working and the browser toggle is the only recourse. This is the plan's central risk and it cannot be engineered away.
- **The anon key is a moving target.** It is read from the public bundle, but a rotation that also changes the Supabase project ref would break refresh. Detected at runtime, not silently.
- **No automatic browser fallback.** Chosen deliberately; it means a silent upstream change surfaces as an error rather than a slow success.
- **No progress reporting for in-flight downloads.** `download_speed` and `eta` come from the server's view at scan time and are not polled during a transfer. Live progress still comes from the existing per-file byte counters.
- **The 6-worker default is a guess.** It is 12x under the documented ceiling but TorBox has an open complaint about `requestdl` failures under load that was not reproduced here.
- **No measurement of the current browser path.** **Resolved** — both backends were measured back to back; see *Implementation outcome*.
- **Streaming, WinFsp and WebDAV are not otherwise touched.** They consume the seam and gain concurrency, nothing more.
- Open question: should `zip_link=true` be exposed to download a whole torrent as one archive? It exists in the API and would cut `requestdl` calls from N to 1, at the cost of a large temporary file. Deliberately deferred.

## Implementation sequence

Stages 1–4 are risk-ordered. Stages 1 and 2 ship together. **Do not start Stage 3 until Stage 2's acceptance criteria pass on the real account.**

- [ ] **Stage 1 — Token layer.** `TokenManager` with `ensure_valid`, `resolve_anon_key`, single-flight refresh, write-back. Roll back if any existing profile stops loading or a refresh loses cookies.
- [ ] **Stage 2 — `TorboxApiSession`.** All five methods against the real account, plus `viewing_torrent_name`. Roll back to Stage 1 alone (dead code, no behavior change) if anything is wrong here.
- [ ] **Stage 3 — Wiring.** `fetch_backend` setting, backend-aware `get_or_create_session`, `browser_op_lock = None` on HTTP, lazy Playwright import, delete dead code. Verify both backends end-to-end before continuing. Roll back to Stage 2 by setting `fetch_backend=browser`.
- [ ] **Stage 4 — Consumers.** `main.py` prefetch, `vfs.py` file_id plumbing. Roll back to Stage 3 by reverting these two files; the seam is unchanged so the app still works.
- [x] **Stage 5 — GUI progress.** `update_queue` fields and the three front-end lines. Purely additive; no rollback needed.
- [ ] **Stage 6 — Optional, needs explicit approval.** Evaluate `POST /v1/api/user/refreshtoken` for a long-lived key. **This rotates the account API key and must not be run without asking.** Not run; the free-plan refresh path works and is covered by tests.
- [ ] **Stage 7 — Only after Stages 1-6 are verified in production.** Consider deleting `browser_session.py` and `detector.py` and dropping Playwright to a login-only extra.

## Required verification

The following were required tests. **Checked items were actually executed**; unchecked items
have not been and remain outstanding. Mocked and fake-server tests first, real integration
second.

**Isolation rules for everything below:** tests run against `tempfile.mkdtemp` directories, in-memory fakes and injected transports; they never touch the real `profiles/` directory, the real `settings.json`, or production URLs. Any test that must change an env var snapshots it and restores it in `finally`, asserting restoration on both the success and failure paths.

- [x] `tests/test_api_session.py` — path derivation (nested, flat, unicode), `short_name` fallback, `selector is None`, `size` string, `mylist?id=` dict shape, `requestdl` params, one-retry-on-401, sentinel on second 401, single-flight refresh under 20 threads, anon-key seed and re-derivation, 5xx detail retention, the `id: 0` file, the escape-hatch log firing once.
- [x] `tests/test_browser_lock.py` — extend: `browser_op(None, ...)` is a no-op and never raises `BrowserBusyError`; concurrent resolves overlap with no lock; `file_id` plumbing including `id: 0`; browser-style fallback.
- [x] `tests/test_mount.py`, `tests/test_shared_vfs.py` — extend `FakeSession` with `file_id`/`torrent_id` and `viewing_torrent_name`.
- [x] `tests/test_pipeline_links.py` — the batched producer: one prefetch per torrent, exact ids, per-file retry for stragglers, one call per file on the browser backend, cancellation, skipping complete files.
- [x] Regression: `tests/test_detector.py` and `tests/test_browser_session.py` pass unchanged, proving the browser backend is intact.
- [x] Regression: `tests/test_profiles.py` passes unchanged, proving profile persistence is intact.
- [x] `python -m unittest` over all 17 modules: 292 tests pass, 1 skipped (needs FFmpeg).
- [x] Real integration: GUI server on `http` — scan, 218-file structure, link generated with CDN `HEAD` 200, VFS link resolved, rescan, settings switch, and `playwright` absent from `sys.modules`.
- [x] Real integration: GUI server on `browser` — session built, lock retained, 218 files expanded, no `file_id` returned, and a runtime switch back to `http` swaps the session and rebuilds the VFS.
- [x] Real integration: a real download through `main.run_downloader` — 175,335 bytes at exactly the API's `size_bytes`, at exactly the API's folder path, 2.3 s, no browser.
- [x] Measure and record: peak RSS and wall-clock on both backends. Done — +6 MB vs +717 MB, and 11.1 s vs ~449 s for a 218-file link pass.
- [ ] Real integration: mount the drive and unmount against a live CDN file. The VFS link path is verified; WinFsp and WebDAV mounting have not been exercised against a live link.
- [ ] Real integration: kill the app mid-download, restart, confirm resume by size. The resume check is unchanged and unit-covered, but not run live.
- [ ] Real integration: leave the app idle past the 1 h token expiry, confirm a later operation refreshes without user action. The refresh itself is tested (single-flight, write-back, cookie preservation) and a 43-minute-old token was loaded successfully during verification, but the full hour boundary has not been waited out.

## Post-implementation additions

- [ ] Rewrite `README.md:7-9` and the setup steps that say `playwright install chromium`; note that Playwright is now only needed to sign in.
- [ ] Update `requirements.txt` with a comment marking `playwright` as login-only; do not remove it yet (Stage 7).
- [ ] Add `fetch_backend` and `link_concurrency` to the settings documentation and to any settings reset path.
- [ ] Log the chosen backend, the profile name, and the anonymous-key source at startup.
- [ ] Log a one-time hint on any HTTP backend failure telling the user `fetch_backend=browser` is the escape hatch.
- [ ] No data migration is needed. Existing profile files are read as-is; the first refresh rewrites them in place, atomically.
- [ ] Record the rollback instruction in the README: set `fetch_backend` to `browser` and restart.

## Rollout

1. Back up `profiles/` and `settings.json`.
2. Ship Stages 1 and 2 with the HTTP backend **not yet selectable** — it exists but nothing calls it.
3. Ship Stage 3 with `fetch_backend` defaulting to `browser`, so the released behavior is unchanged.
4. Flip the default to `http` on this machine only. Run the full manual sequence.
5. Compare against the browser baseline: wall-clock, peak RSS, and whether the resulting folder tree is identical.
6. Only then consider flipping the shipped default.

## Final acceptance criterion

With `fetch_backend` set to `http`, the app signs in, lists torrents, expands folder structure, generates every download link, downloads, streams and mounts with no Chromium process running at any point after sign-in; a torrent of 200+ files links completely in seconds rather than minutes; the local folder tree is identical to what the browser backend produces; an access token older than an hour refreshes silently; and setting `fetch_backend` back to `browser` restores today's behavior exactly, with no code change and no reinstall.
