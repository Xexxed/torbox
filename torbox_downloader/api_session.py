"""Direct TorBox HTTP access -- the browser-free fetch backend.

The web app at torbox.app is itself a client of the documented TorBox API at
``https://api.torbox.app/v1/api``. What its bundle does *not* advertise is that
free-plan accounts are allowed to use that API too: the bundle's auth helper
(``Vt``) resolves the bearer token as ``session.access_token`` -- the Supabase
JWT that already sits in ``localStorage["sb-db-auth-token"]`` -- whenever
``users.plan === 0``, and only falls back to reading a real API key out of the
``api_tokens`` table for paid plans.

That is the path this module takes, so a Pro plan and an API key are not
required. Everything the browser used to scrape out of the DOM comes back as
plain JSON instead:

    scan_torrents()        GET  /torrents/mylist?limit=1000
    fetch_structure(name)  GET  /torrents/mylist?id=<torrent_id>
    generate_download_link GET  /torrents/requestdl?torrent_id=&file_id=&token=

``mylist`` already returns each torrent's full ``files`` array, and every file
entry carries ``name`` (the torrent-root-relative path), ``short_name``,
``size`` and its own numeric ``id``. Folder structure therefore falls out of
string splitting instead of DOM traversal, and a file is identified exactly by
``(torrent_id, file_id)`` instead of by guessing from its name and size.

The one thing the browser genuinely provided was hourly token refresh: Supabase
access tokens last an hour, and the SPA silently exchanged the refresh token
behind the scenes. ``TokenManager`` takes that over, using the same public
``apikey`` the bundle ships.

Nothing in this module imports Playwright.
"""

import base64
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from torbox_downloader import profiles as profiles_mod

logger = logging.getLogger(__name__)

API_BASE = "https://api.torbox.app/v1/api"
SUPABASE_AUTH_URL = "https://db.torbox.app/auth/v1/token"
SITE_URL = "https://torbox.app"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Refresh this many seconds before the token actually expires, so a request
# never races the expiry boundary.
TOKEN_EXPIRY_SKEW_S = 300.0

# Public Supabase anon key, read out of the live site bundle on 2026-09-28.
# It is embedded in client-side JavaScript, so it is not a secret; it is used
# only to identify us to GoTrue when exchanging a refresh token. Seeded here so
# a bundle change cannot block startup, and re-derived from the live bundle if
# the seed is ever rejected.
ANON_KEY_SEED = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJlenhmeXRrbm5rb2V"
    "dHRldHpzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MjkxMjgzMzAsImV4cCI6MjA0NDcwNDMzMH0."
    "vIQWcZuN6Nx3DnkmsWLK25J8BM3TTA_8Tb4GoK99MqM"
)

DEFAULT_LINK_CONCURRENCY = 6
MAX_LINK_CONCURRENCY = 12

# Matches the substring that gui_server.py and main.py already grep for when
# deciding that a session is dead and should be logged out.
SESSION_EXPIRED_SENTINEL = "session token has expired or is invalid"


class ApiError(RuntimeError):
    """A TorBox API call failed."""


class SessionExpiredError(ApiError):
    """The stored session cannot be refreshed; the user must sign in again.

    The message carries ``SESSION_EXPIRED_SENTINEL`` so existing handlers that
    match on that substring keep working.
    """

    def __init__(self, detail=""):
        super().__init__(
            "Your Torbox session token has expired or is invalid."
            + (f" ({detail})" if detail else "")
        )


class RateLimitedError(ApiError):
    """TorBox answered 429. The caller should back off, not retry immediately."""


# --- anonymous key -------------------------------------------------------

_anon_lock = threading.Lock()
# "seed" until derive_anon_key() proves a live key, "live" once it has.
_anon_cache = {"key": ANON_KEY_SEED, "source": "seed", "warned": False}

_BUNDLE_SRC_RE = re.compile(r'src="(/assets/index-[^"]+\.js)"')
_ANON_KEY_RE = re.compile(r'VITE_SUPABASE_KEY:"([^"]+)"')


def resolve_anon_key():
    """Return the cached anon key. Never touches the network.

    The seed is tried first on purpose: it keeps startup offline-safe and makes
    a bundle rotation a recoverable event (GoTrue answers 401, we re-derive)
    rather than a startup failure.
    """
    return _anon_cache["key"]


def anon_key_source():
    return _anon_cache["source"]


def derive_anon_key():
    """Re-read the anon key from the live site bundle. Returns it, or None.

    Called only after GoTrue rejects the cached key, so a TorBox rotation is
    handled without a code change.
    """
    try:
        page = requests.get(SITE_URL, timeout=15, headers={"User-Agent": USER_AGENT})
        src = _BUNDLE_SRC_RE.search(page.text)
        if not src:
            raise ApiError("no JS bundle referenced by the TorBox page")
        bundle = requests.get(
            SITE_URL + src.group(1), timeout=30, headers={"User-Agent": USER_AGENT}
        )
        found = _ANON_KEY_RE.search(bundle.text)
        if not found:
            raise ApiError("no anon key in the TorBox bundle")
    except Exception as e:
        logger.warning("Could not re-derive the TorBox anon key: %s", e)
        return None
    with _anon_lock:
        if found.group(1) != _anon_cache["key"]:
            logger.info("The TorBox anon key changed; using the freshly derived one.")
        _anon_cache["key"] = found.group(1)
        _anon_cache["source"] = "live"
    return found.group(1)


# --- helpers -------------------------------------------------------------


def _b64url_json(segment):
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))


def jwt_expiry(token):
    """Return the ``exp`` claim of a JWT, or None. Signature is not verified:
    the token is only used to decide *when* to refresh, never to trust."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        exp = _b64url_json(parts[1]).get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def format_size(num_bytes):
    """Render a byte count the way the rest of the app expects.

    ``downloader.parse_size_to_bytes`` only understands GB/MB/KB/B, so this
    deliberately never emits TB -- a 2 TB torrent is reported as ~2048 GB.
    """
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "Unknown"
    if value <= 0:
        return "0 B"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= scale:
            return f"{value / scale:.2f} {unit}"
    return f"{value:.0f} B"


def _coerce_session(token):
    """Accept a Supabase session dict, a JSON string of one, or a bare JWT.

    Returns ``(access_token, refresh_token, expires_at)``; refresh_token and
    expires_at may be None/0 when only a JWT was supplied.
    """
    if not token:
        return None, None, 0.0
    if isinstance(token, dict):
        parsed = token
    else:
        text = str(token).strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                return None, None, 0.0
        else:
            return text, None, jwt_expiry(text) or 0.0
    access = parsed.get("access_token")
    if not access:
        return None, None, 0.0
    return access, parsed.get("refresh_token"), float(parsed.get("expires_at") or 0.0)


# --- token management ----------------------------------------------------


class TokenManager:
    """Owns the Supabase session and keeps it fresh.

    The browser used to do this silently via the SPA's auth client. Taking it
    over is what lets the fetch path drop the browser entirely.
    """

    def __init__(self, http=None, profile_store=None, profile_name=None, token=None):
        self._http = http or requests.Session()
        self._lock = threading.Lock()
        # An immutable tuple, so a reader always sees a consistent pair without
        # taking the lock. CPython attribute reads are atomic.
        self._state = (None, None, 0.0)  # (access_token, refresh_token, expires_at)
        self._profile_store = profile_store
        self._profile_name = profile_name
        self._warned_compat = False
        if token:
            self.set_token(token)
        elif profile_store is not None and profile_name:
            self._load_from_profile()

    # --- loading / persisting ----------------------------------------

    def _load_from_profile(self):
        state = self._profile_store.load_state(self._profile_name)
        if not state:
            raise SessionExpiredError("no saved session for this profile")
        session = profiles_mod.extract_session(state)
        access, refresh, expires = _coerce_session(session)
        if not access:
            raise SessionExpiredError("profile holds no Supabase session")
        self._state = (access, refresh, expires or jwt_expiry(access) or 0.0)
        logger.info(
            "Loaded the TorBox session for profile '%s' (expires in %.0f min).",
            self._profile_name, max(0.0, self._state[2] - time.time()) / 60.0,
        )

    def _persist(self):
        """Write the rotated session back to the profile, atomically.

        Only the two session keys are touched: cookies and every unrelated
        localStorage entry are preserved exactly as the browser left them.
        """
        if self._profile_store is None or not self._profile_name:
            return
        access, refresh, _ = self._state
        if not access:
            return
        state = self._profile_store.load_state(self._profile_name)
        if not isinstance(state, dict):
            return
        payload = json.dumps({
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": time.time() + 3600.0,
            "token_type": "bearer",
        })
        for origin in state.get("origins") or []:
            if not isinstance(origin, dict):
                continue
            entries = origin.get("localStorage")
            if not isinstance(entries, list):
                continue
            touched = False
            for item in entries:
                if isinstance(item, dict) and item.get("name") in profiles_mod.SESSION_STORAGE_KEYS:
                    item["value"] = payload
                    touched = True
            if not touched:
                entries.append({"name": profiles_mod.SESSION_STORAGE_KEYS[0], "value": payload})
        try:
            self._profile_store.save_state(self._profile_name, state)
        except Exception as e:
            # A persistence failure must never abort a download; the in-memory
            # token is still valid for this run.
            logger.warning("Could not persist the refreshed session: %s", e)

    # --- public API ---------------------------------------------------

    def set_token(self, token):
        """Replace the session from a raw JWT, a session dict, or JSON text."""
        access, refresh, expires = _coerce_session(token)
        if not access:
            raise SessionExpiredError("no usable access token supplied")
        with self._lock:
            self._state = (access, refresh, expires or jwt_expiry(access) or 0.0)
        logger.info("TorBox session token replaced.")

    def invalidate(self):
        """Force the next access to re-authenticate. Used after a 401."""
        with self._lock:
            access, refresh, _ = self._state
            self._state = (access, refresh, 0.0)

    def has_session(self):
        return bool(self._state[0])

    def access_token(self):
        """Return a currently-valid access token, refreshing if needed.

        Concurrent callers collapse onto a single refresh: the expiry check and
        the exchange both happen under the lock, and the state is re-read after
        acquiring it. This matters because GoTrue *rotates* the refresh token,
        so two parallel refreshes would invalidate each other.
        """
        access, _refresh, expires = self._state
        if access and expires - TOKEN_EXPIRY_SKEW_S > time.time():
            return access

        with self._lock:
            access, refresh, expires = self._state
            if access and expires - TOKEN_EXPIRY_SKEW_S > time.time():
                return access
            if not access:
                raise SessionExpiredError("no session loaded")
            if not refresh:
                # A bare JWT with no refresh token cannot be renewed.
                raise SessionExpiredError("stored session has no refresh token")
            self._refresh_locked(refresh)
            return self._state[0]

    def _refresh_locked(self, refresh_token, _retried=False):
        logger.info("Refreshing the TorBox session token...")
        try:
            response = self._http.post(
                SUPABASE_AUTH_URL,
                params={"grant_type": "refresh_token"},
                headers={"apikey": resolve_anon_key(), "Content-Type": "application/json"},
                json={"refresh_token": refresh_token},
                timeout=30,
            )
        except requests.RequestException as e:
            raise ApiError(f"Could not reach the TorBox sign-in service: {e}") from e

        if response.status_code in (400, 401, 403) and not _retried:
            # GoTrue answers "Invalid API key" when the anon key has been
            # rotated out from under us. Re-derive it once and try again.
            if anon_key_source() == "seed" and derive_anon_key():
                return self._refresh_locked(refresh_token, _retried=True)
        if response.status_code != 200:
            raise SessionExpiredError(
                f"refresh rejected with HTTP {response.status_code}")
        payload = response.json()
        access = payload.get("access_token")
        if not access:
            raise SessionExpiredError("refresh returned no access token")
        self._state = (
            access,
            payload.get("refresh_token") or refresh_token,
            time.time() + float(payload.get("expires_in") or 3600.0),
        )
        self._persist()
        logger.info("TorBox session token refreshed.")


# --- the session ---------------------------------------------------------


class TorboxApiSession:
    """Browser-free implementation of the fetch-backend session contract.

    Mirrors ``TorboxBrowserSession``'s public surface (``scan_torrents``,
    ``fetch_structure``, ``generate_download_link``, ``go_to_dashboard``,
    ``update_token``) so everything above the seam -- ``main.py``,
    ``mount/vfs.py``, ``gui_server.py``, WinFsp, WebDAV -- is unchanged.

    Unlike the browser session, every method here is safe to call from several
    threads at once, which is why callers stop serialising on the browser lock
    when this backend is active.
    """

    def __init__(self, token=None, profile=None, profile_store=None,
                 base_url=API_BASE, http=None, link_concurrency=DEFAULT_LINK_CONCURRENCY):
        self.base_url = base_url.rstrip("/")
        self.current_torrent_name = None
        self.link_concurrency = max(1, min(MAX_LINK_CONCURRENCY, int(link_concurrency)))
        store = profile_store
        if store is None and profile:
            store = profiles_mod.ProfileStore()
        self.tokens = TokenManager(http=http, profile_store=store, profile_name=profile, token=token)
        self._http = http or requests.Session()
        self._http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._lock = threading.Lock()
        self._torrents = []
        self._structures = {}
        self._warned_compat = False
        self._closed = False

    # --- lifecycle ----------------------------------------------------

    def start(self):
        """No browser to launch; validates the session instead."""
        self.tokens.access_token()
        return self

    def stop(self):
        self._closed = True
        try:
            self._http.close()
        except Exception:
            pass

    def update_token(self, new_token):
        self.tokens.set_token(new_token)
        with self._lock:
            self._torrents = []
            self._structures = {}

    def viewing_torrent_name(self):
        """Report which torrent the structure cache currently holds.

        Mirrors ``TorboxBrowserSession.viewing_torrent_name`` so callers can ask
        without knowing the backend, and without reaching into browser state
        from an unrelated thread the way the old ``session.page.url`` check did.

        There is no page here, so the cache is always considered live.
        """
        return {"torrent": self.current_torrent_name, "on_download_view": True}

    def go_to_dashboard(self):
        """No-op: there is no page to navigate back to."""
        return None

    def get_page_content(self):
        raise ApiError(
            "This backend has no page to read. main.py must not call "
            "get_page_content() when fetch_backend is 'http'."
        )

    # --- transport ----------------------------------------------------

    def _request(self, path, params=None, method="get", auth=True, _retried=False):
        """One API call, with a single transparent retry after a 401."""
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.tokens.access_token()}"
        try:
            response = self._http.request(
                method, f"{self.base_url}{path}", params=params,
                headers=headers, timeout=60,
            )
        except requests.RequestException as e:
            raise ApiError(f"Could not reach the TorBox API: {e}") from e

        if response.status_code == 401 and not _retried:
            logger.info("TorBox rejected the token; refreshing and retrying once.")
            self.tokens.invalidate()
            return self._request(path, params=params, method=method, auth=auth, _retried=True)
        if response.status_code == 401:
            raise SessionExpiredError("the API rejected the session twice")
        if response.status_code == 429:
            raise RateLimitedError("TorBox rate limited this client")
        if response.status_code >= 500:
            raise ApiError(f"TorBox API returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _payload(response, what):
        try:
            body = response.json()
        except ValueError as e:
            raise ApiError(f"TorBox sent a non-JSON response for {what}") from e
        if not isinstance(body, dict) or not body.get("success"):
            detail = (body or {}).get("detail") or (body or {}).get("error") or "unknown error"
            raise ApiError(f"TorBox rejected {what}: {detail}")
        return body.get("data")

    # --- the contract -------------------------------------------------

    def scan_torrents(self):
        """List the account's torrents.

        Returns one dict per torrent with the ``name``/``size`` pair the rest of
        the app expects, plus the progress fields the browser scraper had no way
        to obtain.
        """
        data = self._payload(self._request("/torrents/mylist", {"limit": 1000}), "the torrent list")
        torrents = []
        for item in data or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            progress = item.get("progress")
            try:
                percent = int(round(float(progress) * 100)) if progress is not None else 0
            except (TypeError, ValueError):
                percent = 0
            torrents.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "size": format_size(item.get("size")),
                "size_bytes": item.get("size") or 0,
                "progress": max(0, min(100, percent)),
                "eta": item.get("eta"),
                "download_state": item.get("download_state"),
                "download_finished": bool(item.get("download_finished")),
                "download_speed": item.get("download_speed") or 0,
                "file_count": len(item.get("files") or []),
            })
        torrents.sort(key=lambda t: (t["id"] is None, t["id"] or 0), reverse=True)
        with self._lock:
            self._torrents = torrents
            self._structures.clear()
        return torrents

    def _torrent_for(self, name):
        with self._lock:
            for t in self._torrents:
                if t["name"] == name:
                    return t
        if not self._torrents:
            self.scan_torrents()
            with self._lock:
                for t in self._torrents:
                    if t["name"] == name:
                        return t
        raise ApiError(f"No torrent named '{name}' in the account")

    def fetch_structure(self, torrent_name):
        """Return the file tree for one torrent.

        Each entry keeps the shape the downloader and VFS already expect
        (``name``, ``path``, ``size``) and adds the exact identifiers
        (``torrent_id``, ``file_id``) that replace name-and-size matching.
        ``path[0]`` is always the torrent name, which is what
        ``mount/vfs.py`` assumes when it strips the root segment.
        """
        torrent = self._torrent_for(torrent_name)
        with self._lock:
            cached = self._structures.get(torrent["id"])
        if cached is not None:
            self.current_torrent_name = torrent_name
            return [dict(f) for f in cached]

        data = self._payload(
            self._request("/torrents/mylist", {"id": torrent["id"]}),
            f"the file list for '{torrent_name}'",
        )
        # With ?id= the API answers with a single object rather than a list.
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise ApiError(f"TorBox returned no file list for '{torrent_name}'")

        entries = []
        for f in data.get("files") or []:
            # A file id of 0 is legitimate -- TorBox ships at least one such
            # entry, and it addresses that single file rather than the whole
            # torrent (only zip_link=true does that). Never test ids for
            # truthiness here, or a real file is dropped from the tree.
            if not isinstance(f, dict) or f.get("id") is None:
                continue
            relative = f.get("name") or ""
            segments = [s for s in relative.split("/") if s]
            file_name = f.get("short_name") or (segments[-1] if segments else relative)
            if not file_name:
                continue
            size_bytes = f.get("size") or 0
            entries.append({
                "name": file_name,
                "path": [torrent_name] + segments[:-1],
                "size": format_size(size_bytes),
                "size_bytes": size_bytes,
                # Kept for contract compatibility; meaningless without a DOM.
                "selector": None,
                "torrent_id": torrent["id"],
                "file_id": f["id"],
            })

        with self._lock:
            self._structures[torrent["id"]] = entries
        self.current_torrent_name = torrent_name
        return [dict(e) for e in entries]

    def _file_id_for(self, name, size, selector):
        """Compatibility lookup for callers that only have name and size."""
        with self._lock:
            for entries in self._structures.values():
                for entry in entries:
                    if entry["name"] == name and entry["size"] == size:
                        return entry["torrent_id"], entry["file_id"]
        if not self._warned_compat:
            logger.info(
                "Link generation is falling back to name-and-size matching; "
                "pass file_id/torrent_id to avoid it."
            )
            self._warned_compat = True
        return None, None

    def generate_download_link(self, name, size=None, selector=None,
                               file_id=None, torrent_id=None):
        """Exchange ``(torrent_id, file_id)`` for a CDN URL.

        The ``name``/``size``/``selector`` arguments exist only so this stays
        signature-compatible with the browser backend; they are ignored when
        ``file_id`` is supplied.
        """
        if file_id is None:
            if not self.current_torrent_name:
                raise ApiError(
                    "generate_download_link needs file_id, or an expanded torrent first"
                )
            self.fetch_structure(self.current_torrent_name)
            torrent_id, file_id = self._file_id_for(name, size, selector)
            if file_id is None:
                raise ApiError(f"No file_id known for '{name}'")

        data = self._payload(self._request("/torrents/requestdl", {
            "torrent_id": torrent_id,
            "file_id": file_id,
            "token": self.tokens.access_token(),
        }, auth=False), f"a download link for file {file_id}")
        if not data or not isinstance(data, str):
            raise ApiError(f"TorBox returned no download link for file {file_id}")
        return data

    def prefetch_links(self, refs, cancel_event=None, workers=None):
        """Resolve many ``(torrent_id, file_id, key)`` refs concurrently.

        Returns ``{key: url}`` for the refs that resolved. Failures are logged
        and omitted so one bad file cannot fail a whole batch.
        """
        refs = list(refs)
        if not refs:
            return {}
        limit = max(1, min(workers or self.link_concurrency, MAX_LINK_CONCURRENCY))
        results = {}
        logger.info("Resolving %d download link(s) with %d workers.", len(refs), limit)

        def resolve(ref):
            torrent_id, file_id, key = ref
            if cancel_event is not None and cancel_event.is_set():
                return key, None
            try:
                return key, self.generate_download_link(
                    None, file_id=file_id, torrent_id=torrent_id)
            except Exception as e:
                logger.warning("Could not resolve a download link (file %s): %s", file_id, e)
                return key, None

        with ThreadPoolExecutor(max_workers=limit) as pool:
            for key, url in pool.map(resolve, refs):
                if url:
                    results[key] = url
        return results

    def clear_cache(self):
        with self._lock:
            self._torrents = []
            self._structures.clear()
