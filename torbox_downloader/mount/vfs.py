"""Virtual filesystem model over the TorBox browser session.

The drive root lists every torrent card from the dashboard; each torrent
directory lazily expands into its scraped folder structure the first time
something lists or opens inside it. Paths use forward slashes with a
leading slash:  "/Torrent Name/SubFolder/file.mkv"

Display names are sanitized for Windows (invalid characters removed,
duplicates suffixed) while original names/selector/size are preserved for
CDN link generation.
"""

import logging
import re
import threading
import time

from torbox_downloader.browser_lock import (
    BrowserBusyError,
    DEFAULT_BROWSER_OP_TIMEOUT_S,
    browser_op,
)
from torbox_downloader.downloader import parse_size_to_bytes
from torbox_downloader.mount.streaming import ChunkedRemoteReader
from torbox_downloader import remux

logger = logging.getLogger(__name__)

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# How long a resolved CDN link is reused before it is regenerated. Inherited
# from the mount feature; see the plan's "CDN link TTL is an assumption" note.
CDN_LINK_TTL_S = 1800

# Bounds for the shared reader cache (one ChunkedRemoteReader per open file).
# Consecutive HTTP range requests for the same file reuse the reader's in-RAM
# chunk cache instead of refetching; idle readers are closed and evicted.
DEFAULT_READER_CACHE_MAX = 4
DEFAULT_READER_IDLE_TTL_S = 300

# Media classification. "video"/"image" drive the Stream page's card kind;
# only _BROWSER_PLAYABLE_EXTS can be decoded inline by a browser (the proxy
# relays bytes but never transcodes, so e.g. mkv resolves to a download).
_VIDEO_EXTS = frozenset({
    "mkv", "mp4", "m4v", "webm", "mov", "avi", "ts", "m2ts",
    "mpg", "mpeg", "wmv", "flv",
})
_IMAGE_EXTS = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "avif", "tiff", "tif", "heic",
})
_BROWSER_PLAYABLE_EXTS = frozenset({"mp4", "m4v", "webm", "mov", "ogg"})


def _file_extension(name):
    """Lower-cased extension without the dot; '' when there is none."""
    text = str(name or "")
    if "." not in text:
        return ""
    return text.rsplit(".", 1)[1].strip().lower()


def classify_kind(name):
    """Return 'video' | 'image' | 'other' for a filename."""
    ext = _file_extension(name)
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    return "other"


def is_browser_playable(name):
    """True only for containers a browser can decode natively."""
    return _file_extension(name) in _BROWSER_PLAYABLE_EXTS


def sanitize_fs_name(name):
    """Make a torrent/file name safe as a Windows path component."""
    clean = _INVALID_FS_CHARS.sub("_", str(name)).rstrip(" .")
    return clean if clean else "_"


class VfsError(Exception):
    """Base error for virtual filesystem operations."""


class VfsNotFound(VfsError):
    pass


class VfsNotADirectory(VfsError):
    pass


class VfsIsADirectory(VfsError):
    pass


class FileEntry:
    __slots__ = ("name", "display_name", "size_str", "size_bytes", "selector", "torrent",
                 "torrent_id", "file_id", "cdn_url", "cdn_url_expires")

    def __init__(self, name, display_name, size_str, selector, torrent,
                 torrent_id=None, file_id=None, size_bytes=None):
        self.name = name                    # original scraped filename
        self.display_name = display_name    # sanitized, unique among siblings
        self.size_str = size_str or "Unknown"
        # Prefer an exact byte count when the backend supplies one; fall back
        # to parsing the display string for browser-built trees.
        self.size_bytes = size_bytes or parse_size_to_bytes(self.size_str) or 0
        self.selector = selector
        self.torrent = torrent              # owning TorrentDir
        # Exact identifiers, when the backend provides them. These replace
        # name-and-size guessing when resolving a link.
        self.torrent_id = torrent_id
        self.file_id = file_id
        self.cdn_url = None
        self.cdn_url_expires = 0

    @property
    def is_dir(self):
        return False


class _DirBase:
    __slots__ = ("display_name", "children")

    def __init__(self, display_name):
        self.display_name = display_name
        self.children = {}                  # sanitized child name -> node

    @property
    def is_dir(self):
        return True

    def _add_child(self, node):
        base = node.display_name
        candidate = base
        suffix = 2
        while candidate in self.children:
            candidate = f"{base} ({suffix})"
            suffix += 1
        node.display_name = candidate
        self.children[candidate] = node
        return node


class SubDir(_DirBase):
    __slots__ = ()


class TorrentDir(_DirBase):
    __slots__ = ("original_name", "total_size_str", "expanded")

    def __init__(self, display_name, original_name, total_size_str="Unknown"):
        super().__init__(display_name)
        self.original_name = original_name  # exact dashboard card title
        self.total_size_str = total_size_str
        self.expanded = False


class TorboxVfs:
    """Read-only tree over scan_torrents()/fetch_structure()/link generation."""

    def __init__(self, session, reader_cache_max=DEFAULT_READER_CACHE_MAX,
                 reader_idle_ttl=DEFAULT_READER_IDLE_TTL_S,
                 browser_lock=None, browser_op_timeout=DEFAULT_BROWSER_OP_TIMEOUT_S):
        self._session = session
        self._lock = threading.RLock()
        self._torrents = {}                 # display name -> TorrentDir
        self.total_bytes = 0
        self.file_count = 0

        # Shared lock guarding every browser-mutating operation. Injected by
        # AppState so the mount, the Stream page and the downloader all
        # serialize against each other; None means "own the browser outright".
        self._browser_lock = browser_lock
        self._browser_op_timeout = browser_op_timeout

        # Per-file locks so two rapid clicks on one file coalesce into a single
        # browser link generation instead of racing.
        self._link_locks = {}               # path -> threading.Lock
        self._link_locks_guard = threading.Lock()

        # Bounded cache of open readers keyed by normalized vfs path, so a
        # player's successive range requests share one chunk cache.
        self._reader_lock = threading.RLock()
        self._readers = {}                  # path -> {"reader", "last_used"}
        self._reader_cache_max = max(1, int(reader_cache_max))
        self._reader_idle_ttl = max(0.0, float(reader_idle_ttl))

        # Codec/duration probes keyed by normalized path. A file's codecs never
        # change, so a successful probe is cached for the life of the tree.
        # Only successes are stored: a transient ffprobe failure must not
        # permanently demote a playable file to the download panel.
        self._probe_lock = threading.Lock()
        self._probes = {}

    # --- population ---------------------------------------------------------

    def scan(self):
        """Refresh the torrent list from the dashboard. Returns the raw scan
        result (list of {'name', 'size'}) so callers can sync the GUI queue."""
        torrents = self._session.scan_torrents() or []
        with self._lock:
            self._torrents = {}
            total = 0
            count = 0
            root = TorrentDir("/", "/", "Unknown")
            for t in torrents:
                name = t.get("name", "")
                if not name:
                    continue
                node = TorrentDir(sanitize_fs_name(name), name, t.get("size", "Unknown"))
                root._add_child(node)
                size = parse_size_to_bytes(t.get("size", "")) or 0
                total += size
                count += 1
            self._torrents = root.children
            self.total_bytes = total
            self.file_count = count
        return torrents

    def refresh(self):
        # Drop open readers too: the tree they were resolved against is gone.
        self.close_all_readers()
        with self._lock:
            self._torrents = {}
        # Probes are keyed by path, and a refresh can hand the same path to a
        # different file, so they go as well.
        with self._probe_lock:
            self._probes = {}
        return self.scan()

    # --- internals ----------------------------------------------------------

    def _expand(self, torrent_dir):
        """Scrape and build the subtree for one torrent. Caller holds lock."""
        try:
            # Scraping navigates the shared browser, so it takes the same lock
            # as link generation. BrowserBusyError is not a VfsError: callers
            # distinguish "try again" from "this torrent is broken".
            with browser_op(self._browser_lock, self._browser_op_timeout,
                            "expand a torrent"):
                files = self._session.fetch_structure(torrent_dir.original_name) or []
        except BrowserBusyError:
            raise
        except Exception as e:
            raise VfsError(f"Could not expand torrent '{torrent_dir.original_name}': {e}") from e

        for f in files:
            name = f.get("name")
            if not name:
                continue
            segments = list(f.get("path", []))
            inner = segments[1:] if segments else []  # segments[0] is the torrent title

            node = torrent_dir
            for seg in inner:
                seg_name = sanitize_fs_name(seg)
                child = node.children.get(seg_name)
                if child is None:
                    child = node._add_child(SubDir(seg_name))
                elif isinstance(child, FileEntry):
                    # Path collision between a file and a folder name; nest anyway.
                    child = node._add_child(SubDir(seg_name))
                node = child

            node._add_child(FileEntry(
                name=name,
                display_name=sanitize_fs_name(name),
                size_str=f.get("size", "Unknown"),
                size_bytes=f.get("size_bytes"),
                selector=f.get("selector"),
                torrent=torrent_dir,
                torrent_id=f.get("torrent_id"),
                file_id=f.get("file_id"),
            ))

        torrent_dir.expanded = True

    def _resolve(self, path):
        """Resolve a vfs path to a node. Expands torrents on demand."""
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        with self._lock:
            if not parts:
                return None  # root
            name = parts[0]
            node = self._torrents.get(name)
            if node is None:
                raise VfsNotFound(f"No torrent named '{name}'")
            for part in parts[1:]:
                if isinstance(node, FileEntry):
                    raise VfsNotADirectory(f"'{part}' is below a file")
                if isinstance(node, TorrentDir) and not node.expanded:
                    self._expand(node)
                nxt = node.children.get(part)
                if nxt is None:
                    raise VfsNotFound(f"Path not found: /{'/'.join(parts)}")
                node = nxt
            if isinstance(node, TorrentDir) and not node.expanded:
                self._expand(node)
            return node

    def _safe_resolve(self, path):
        try:
            return self._resolve(path)
        except VfsError:
            return None

    # --- public API ----------------------------------------------------------

    def stat(self, path):
        """Return {'is_dir': bool, 'size': int} or None when missing."""
        path = str(path)
        if path in ("", "/"):
            return {"is_dir": True, "size": 0}
        node = self._resolve(path)
        if node.is_dir:
            return {"is_dir": True, "size": 0}
        return {"is_dir": False, "size": node.size_bytes}

    def listdir(self, path):
        """Return [(child_display_name, is_dir)] for a directory path."""
        path = str(path)
        with self._lock:
            if path in ("", "/"):
                return [(name, True) for name in self._torrents.keys()]
            node = self._resolve(path)
            if not node.is_dir:
                raise VfsNotADirectory(f"Not a directory: {path}")
            return [(name, child.is_dir) for name, child in node.children.items()]

    def listdir_details(self, path):
        """Return [(child_display_name, is_dir, size_bytes)] for directory entries."""
        path = str(path)
        with self._lock:
            if path in ("", "/"):
                return [(name, True, 0) for name in self._torrents.keys()]
            node = self._resolve(path)
            if not node.is_dir:
                raise VfsNotADirectory(f"Not a directory: {path}")
            return [
                (name, child.is_dir, 0 if child.is_dir else getattr(child, "size_bytes", 0))
                for name, child in node.children.items()
            ]

    def listdir_entries(self, path):
        """Structured listing for the Stream page: one dict per child.

        Each entry carries only already-scraped metadata -- never a URL,
        selector or CDN field -- so rendering a folder can never resolve a
        link (see the plan's invariant 2).
        """
        path = str(path)
        with self._lock:
            if path in ("", "/"):
                return [
                    {
                        "name": name,
                        "is_dir": True,
                        "size": 0,
                        "size_str": getattr(child, "total_size_str", "Unknown"),
                        "kind": "folder",
                        "playable": False,
                    }
                    for name, child in self._torrents.items()
                ]

            node = self._resolve(path)
            if not node.is_dir:
                raise VfsNotADirectory(f"Not a directory: {path}")

            entries = []
            for name, child in node.children.items():
                if child.is_dir:
                    entries.append({
                        "name": name,
                        "is_dir": True,
                        "size": 0,
                        "size_str": "Unknown",
                        "kind": "folder",
                        "playable": False,
                    })
                else:
                    entries.append({
                        "name": name,
                        "is_dir": False,
                        "size": getattr(child, "size_bytes", 0),
                        "size_str": getattr(child, "size_str", "Unknown"),
                        "kind": classify_kind(getattr(child, "name", name)),
                        "playable": is_browser_playable(getattr(child, "name", name)),
                    })
            return entries

    # --- link resolution ------------------------------------------------------

    def resolve_link(self, path, force_refresh=False):
        """Resolve (and cache) the CDN URL for a single file.

        This is the only place a link is generated. Callers invoke it when a
        user opens/acts on a specific file -- never while listing a folder --
        which keeps the "no links resolved upfront" guarantee.
        """
        node = self._resolve(str(path))
        if not isinstance(node, FileEntry):
            raise VfsIsADirectory(f"Not a file: {path}")
        # Serialize per file: a second click waits, then hits the link cache.
        with self._link_lock_for(self._normalize_key(path)):
            return self._resolve_node_link(node, force_refresh)

    def _link_lock_for(self, key):
        with self._link_locks_guard:
            lock = self._link_locks.get(key)
            if lock is None:
                if len(self._link_locks) > 512:
                    self._link_locks.clear()
                lock = threading.Lock()
                self._link_locks[key] = lock
            return lock

    def _resolve_node_link(self, node, force_refresh=False):
        """Cached CDN link for a FileEntry. Reused by open_reader() so the
        mount and the Stream page share one link cache."""
        now = time.time()
        if not force_refresh and node.cdn_url and now < node.cdn_url_expires:
            return node.cdn_url

        session = self._session
        torrent = node.torrent

        # Expanding a torrent and generating its link must be atomic: another
        # caller (the downloader, another stream click) could navigate the
        # page in between and leave the row we want off-screen. The lock is
        # None on the HTTP backend, which is thread-safe and holds no page.
        with browser_op(self._browser_lock, self._browser_op_timeout,
                        "resolve a download link"):
            # The browser session can only hold one expanded torrent at a
            # time, so re-expand when it is showing something else.
            current = getattr(session, "current_torrent_name", None)
            if force_refresh or current != torrent.original_name:
                try:
                    session.fetch_structure(torrent.original_name)
                except Exception as e:
                    raise VfsError(
                        f"Could not expand torrent '{torrent.original_name}': {e}") from e
            try:
                # Exact identifiers when the backend supplied them; the
                # name/size/selector form is the browser backend's contract.
                if node.file_id is not None and node.torrent_id is not None:
                    url = session.generate_download_link(
                        node.name, node.size_str, node.selector,
                        file_id=node.file_id, torrent_id=node.torrent_id)
                else:
                    url = session.generate_download_link(
                        node.name, node.size_str, node.selector)
            except Exception as e:
                raise VfsError(f"Link generation failed for '{node.name}': {e}") from e
            if not url:
                raise VfsError(f"No CDN link returned for '{node.name}'")
        node.cdn_url = url
        node.cdn_url_expires = time.time() + CDN_LINK_TTL_S
        return url

    # --- media classification -------------------------------------------------

    @staticmethod
    def classify_kind(name):
        """'video' | 'image' | 'other' for a filename."""
        return classify_kind(name)

    @staticmethod
    def is_browser_playable(name):
        """True only for containers a browser can decode natively."""
        return is_browser_playable(name)

    # --- playback mode --------------------------------------------------------

    def probe_media(self, path, force=False):
        """Return {'video', 'audio', 'duration'} for a file, or None.

        Costs one ffprobe run, so the result is cached per path. A failure is
        not cached -- a flaky CDN response should be retried, not remembered.
        """
        key = self._normalize_key(path)
        if not force:
            with self._probe_lock:
                if key in self._probes:
                    return self._probes[key]

        if not remux.available():
            return None

        info = None
        try:
            url = self.resolve_link(path)
            info = remux.probe(url)
        except Exception as exc:
            logger.debug("Codec probe failed for %s: %s", key, exc)

        if info is not None:
            with self._probe_lock:
                self._probes[key] = info
        return info

    def playback_plan(self, path):
        """Decide how `path` should be played, and carry the evidence back.

        Returns `{"mode": ..., "reason": ..., "info": ...}`.

        mode:
          'direct'   -- the browser decodes the file as it is, so the range
                        proxy serves it and seeking works normally.
          'remux'    -- a container the browser cannot read, but whose codecs
                        survive a swap to fragmented MP4, so FFmpeg can relay it
                        without re-encoding. No seeking.
          'external' -- neither: the UI offers copy / download / open instead.

        reason -- the *cause*, so the UI can explain itself instead of showing
        one catch-all message. This matters because "the container is odd" and
        "the codec is HEVC" and "ffmpeg is missing" need three different
        answers from the user's point of view:
          'native'      the browser decodes this container as-is
          'remuxable'   a container swap keeps the codecs playable
          'codec'       probed successfully, but the codecs are not browser-safe
          'no-ffmpeg'   ffmpeg/ffprobe are not installed, so we cannot know
          'unprobeable' the probe failed (dead link, or a container ffprobe
                        cannot read)

        info -- the raw probe result when one was taken, else None. It is
        returned rather than re-read from the cache so that the caller and the
        decision can never disagree about what was measured.
        """
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        kind = classify_kind(name)

        if kind != "video" or is_browser_playable(name):
            return {"mode": "direct", "reason": "native", "info": None}
        if not remux.available():
            return {"mode": "external", "reason": "no-ffmpeg", "info": None}

        info = self.probe_media(path)
        if info is None:
            return {"mode": "external", "reason": "unprobeable", "info": None}
        if remux.can_remux(info):
            return {"mode": "remux", "reason": "remuxable", "info": info}
        return {"mode": "external", "reason": "codec", "info": info}

    def media_mode(self, path):
        """'direct' / 'remux' / 'external' -- see `playback_plan` for the why."""
        return self.playback_plan(path)["mode"]

    def playback_info(self, path):
        """Everything the UI needs to pick a player for `path`."""
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        plan = self.playback_plan(path)
        # Naming the codec is what turns a useless "this container cannot play"
        # into "the video track is HEVC" -- the probe already ran, so discarding
        # its result would only hide the real cause from the user.
        info = plan["info"] or {}
        # A re-encode is the one remaining option for a file that cannot play,
        # so the UI needs to know whether it is possible. Only looked up for an
        # 'external' verdict: the common paths must not pay for it, and the
        # answer would be meaningless for a file that already plays.
        encoder = remux.pick_encoder() if plan["mode"] == "external" else None
        return {
            "kind": classify_kind(name),
            "mode": plan["mode"],
            "reason": plan["reason"],
            "duration": info.get("duration"),
            "video_codec": info.get("video"),
            "audio_codec": info.get("audio"),
            "can_transcode": bool(encoder),
            "transcode_encoder": encoder,
        }

    # --- readers --------------------------------------------------------------

    @staticmethod
    def _normalize_key(path):
        return "/" + "/".join(p for p in str(path).replace("\\", "/").split("/") if p)

    def open_reader(self, path, chunk_size=None, cache_budget=None):
        """Open a file for zero-temp streaming via a ChunkedRemoteReader."""
        node = self._resolve(str(path))
        if not isinstance(node, FileEntry):
            raise VfsIsADirectory(f"Not a file: {path}")

        def url_provider(force_refresh=False):
            return self._resolve_node_link(node, force_refresh)

        kwargs = {}
        if chunk_size is not None:
            kwargs["chunk_size"] = chunk_size
        if cache_budget is not None:
            kwargs["cache_budget"] = cache_budget
        return ChunkedRemoteReader(node.size_bytes, url_provider, **kwargs)

    def get_or_open_reader(self, path, chunk_size=None, cache_budget=None):
        """Return a cached reader for `path`, opening one on first use.

        A player issues many range requests for one file; reusing the reader
        keeps its in-RAM chunk cache warm. The cache is bounded and idle
        readers are closed.
        """
        key = self._normalize_key(path)
        now = time.time()
        with self._reader_lock:
            self._evict_idle_readers(now)
            entry = self._readers.get(key)
            if entry is not None:
                entry["last_used"] = now
                return entry["reader"]

            reader = self.open_reader(path, chunk_size=chunk_size, cache_budget=cache_budget)
            self._readers[key] = {"reader": reader, "last_used": now}
            logger.debug("Opened stream reader for %s (%d cached).", key, len(self._readers))

            # Bound the cache by least-recently-used eviction.
            while len(self._readers) > self._reader_cache_max:
                oldest = min(self._readers, key=lambda k: self._readers[k]["last_used"])
                if oldest == key:
                    break
                self._close_reader_locked(oldest, reason="lru")
            return reader

    def _close_reader_locked(self, key, reason="closed"):
        entry = self._readers.pop(key, None)
        if entry is not None:
            logger.debug("Released stream reader for %s (%s).", key, reason)
            try:
                entry["reader"].close()
            except Exception:
                pass

    def _evict_idle_readers(self, now):
        if self._reader_idle_ttl <= 0:
            return
        stale = [k for k, e in self._readers.items()
                 if now - e["last_used"] > self._reader_idle_ttl]
        for key in stale:
            self._close_reader_locked(key, reason="idle")

    def close_reader(self, path):
        with self._reader_lock:
            self._close_reader_locked(self._normalize_key(path))

    def close_all_readers(self):
        with self._reader_lock:
            count = len(self._readers)
            for key in list(self._readers.keys()):
                self._close_reader_locked(key, reason="session reset")
            if count:
                logger.debug("Closed %d stream reader(s).", count)
