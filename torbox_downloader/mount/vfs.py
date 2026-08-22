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

from torbox_downloader.downloader import parse_size_to_bytes
from torbox_downloader.mount.streaming import ChunkedRemoteReader

logger = logging.getLogger(__name__)

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    __slots__ = ("name", "display_name", "size_str", "size_bytes", "selector", "torrent", "cdn_url", "cdn_url_expires")

    def __init__(self, name, display_name, size_str, selector, torrent):
        self.name = name                    # original scraped filename
        self.display_name = display_name    # sanitized, unique among siblings
        self.size_str = size_str or "Unknown"
        self.size_bytes = parse_size_to_bytes(self.size_str) or 0
        self.selector = selector
        self.torrent = torrent              # owning TorrentDir
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

    def __init__(self, session):
        self._session = session
        self._lock = threading.RLock()
        self._torrents = {}                 # display name -> TorrentDir
        self.total_bytes = 0
        self.file_count = 0

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
        with self._lock:
            self._torrents = {}
        return self.scan()

    # --- internals ----------------------------------------------------------

    def _expand(self, torrent_dir):
        """Scrape and build the subtree for one torrent. Caller holds lock."""
        try:
            files = self._session.fetch_structure(torrent_dir.original_name) or []
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
                selector=f.get("selector"),
                torrent=torrent_dir,
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

    def open_reader(self, path, chunk_size=None, cache_budget=None):
        """Open a file for zero-temp streaming via a ChunkedRemoteReader."""
        node = self._resolve(str(path))
        if not isinstance(node, FileEntry):
            raise VfsIsADirectory(f"Not a file: {path}")

        session = self._session
        torrent = node.torrent

        def url_provider(force_refresh=False):
            now = time.time()
            if not force_refresh and node.cdn_url and now < node.cdn_url_expires:
                return node.cdn_url
            current = getattr(session, "current_torrent_name", None)
            if force_refresh or current != torrent.original_name:
                try:
                    session.fetch_structure(torrent.original_name)
                except Exception as e:
                    raise VfsError(
                        f"Could not expand torrent '{torrent.original_name}': {e}") from e
            try:
                url = session.generate_download_link(node.name, node.size_str, node.selector)
            except Exception as e:
                raise VfsError(f"Link generation failed for '{node.name}': {e}") from e
            if not url:
                raise VfsError(f"No CDN link returned for '{node.name}'")
            node.cdn_url = url
            node.cdn_url_expires = time.time() + 1800  # cache 30 mins
            return url

        kwargs = {}
        if chunk_size is not None:
            kwargs["chunk_size"] = chunk_size
        if cache_budget is not None:
            kwargs["cache_budget"] = cache_budget
        return ChunkedRemoteReader(node.size_bytes, url_provider, **kwargs)
