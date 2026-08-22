"""Zero-temp streaming core for the virtual drive.

`ChunkedRemoteReader` provides random-access reads over a CDN URL that
supports HTTP range requests. Fetched chunks live in a small bounded
in-RAM LRU cache; nothing is ever written to disk.

`LazyRemoteFile` adapts a reader into a seekable, read-only file-object so
WebDAV servers (and anything else expecting file semantics) can stream the
bytes without materializing them.
"""

import io
import logging
import threading
from collections import OrderedDict

import requests

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024      # 4 MiB per CDN range fetch
DEFAULT_CACHE_BUDGET = 64 * 1024 * 1024   # keep ~64 MiB of chunks per reader


class LinkExpiredError(Exception):
    """The CDN URL was rejected (expired/revoked); caller must regenerate."""


class ChunkedRemoteReader:
    """Random-access reader over one remote file, chunk-cached in RAM."""

    def __init__(self, size_bytes, url_provider,
                 chunk_size=DEFAULT_CHUNK_SIZE, cache_budget=DEFAULT_CACHE_BUDGET):
        """
        size_bytes:    total file size in bytes (from the scraped listing).
        url_provider:  callable(force_refresh: bool) -> str. Called with False
                       for the initial (cached-ok) link and with True when the
                       current link was rejected and must be regenerated.
                       May raise any exception to signal permanent failure.
        """
        self._size = max(0, int(size_bytes or 0))
        self._url_provider = url_provider
        self._chunk_size = max(1, int(chunk_size))
        self._cache_budget = max(self._chunk_size, int(cache_budget))
        self._cache = OrderedDict()   # chunk_index -> bytes
        self._cache_bytes = 0
        self._lock = threading.RLock()
        self._closed = False

    @property
    def size(self):
        return self._size

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            self._cache_bytes = 0

    # --- internals ---------------------------------------------------------

    def _fetch_range(self, url, start, end):
        """Fetch [start, end] inclusive. Raises LinkExpiredError on auth-ish
        rejections so the caller can regenerate the link."""
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=(10, 120))
        except requests.RequestException as e:
            raise IOError(f"CDN request failed: {e}") from e
        try:
            if resp.status_code in (401, 403, 410):
                raise LinkExpiredError(f"CDN link rejected (HTTP {resp.status_code})")
            if resp.status_code == 416:
                return b""
            if resp.status_code == 200:
                content = resp.content
                return content[start:end + 1]
            if resp.status_code != 206:
                raise IOError(f"CDN did not honor range request (HTTP {resp.status_code})")

            # Learn exact file size from Content-Range header if available: bytes start-end/total
            cr = resp.headers.get("Content-Range", "")
            if "/" in cr:
                try:
                    total_str = cr.split("/")[-1].strip()
                    if total_str.isdigit():
                        real_total = int(total_str)
                        if real_total > 0 and real_total != self._size:
                            self._size = real_total
                except Exception:
                    pass

            expected = end - start + 1
            raw = resp.raw
            parts = []
            got = 0
            while got < expected:
                piece = raw.read(min(65536, expected - got))
                if not piece:
                    break
                parts.append(piece)
                got += len(piece)

            if got < expected:
                # Reached EOF sooner than estimated file size
                self._size = min(self._size, start + got)
            return b"".join(parts)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _get_chunk(self, index):
        with self._lock:
            if self._closed:
                raise IOError("Reader is closed")
            cached = self._cache.get(index)
            if cached is not None:
                self._cache.move_to_end(index)
                return cached

        start = index * self._chunk_size
        if start >= self._size and self._size > 0:
            return b""
        end = min(start + self._chunk_size, self._size) - 1 if self._size > 0 else start + self._chunk_size - 1

        refreshed = False
        while True:
            url = self._url_provider(force_refresh=refreshed)
            try:
                data = self._fetch_range(url, start, end)
                break
            except LinkExpiredError:
                if refreshed:
                    raise IOError("CDN link expired and regeneration failed")
                logger.info("CDN link expired mid-stream; regenerating...")
                refreshed = True

        with self._lock:
            self._cache[index] = data
            self._cache_bytes += len(data)
            while self._cache_bytes > self._cache_budget and len(self._cache) > 1:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)
        return data

    # --- public ------------------------------------------------------------

    def read_range(self, offset, length):
        """Read up to `length` bytes starting at `offset`; clamps to EOF."""
        offset = max(0, int(offset))
        length = int(length)
        if length <= 0 or (self._size > 0 and offset >= self._size):
            return b""
        end = min(offset + length, self._size) if self._size > 0 else offset + length

        parts = []
        pos = offset
        while pos < end:
            index = pos // self._chunk_size
            chunk = self._get_chunk(index)
            if not chunk:
                break
            chunk_start = index * self._chunk_size
            lo = pos - chunk_start
            if lo >= len(chunk):
                break
            hi = min(end - chunk_start, len(chunk))
            parts.append(chunk[lo:hi])
            pos = chunk_start + hi
            if len(chunk) < self._chunk_size:
                # Reached EOF
                break
        return b"".join(parts)


class LazyRemoteFile(io.RawIOBase):
    """Seekable, read-only file-object backed by a ChunkedRemoteReader."""

    def __init__(self, reader):
        super().__init__()
        self._reader = reader
        self._pos = 0
        self._lock = threading.Lock()

    def readable(self):
        return True

    def seekable(self):
        return True

    def writable(self):
        return False

    def read(self, n=-1):
        with self._lock:
            if n is None or n < 0:
                n = self._reader.size - self._pos
            n = max(0, min(int(n), 1024 * 1024))
            data = self._reader.read_range(self._pos, n)
            self._pos += len(data)
        return data

    def seek(self, offset, whence=0):
        with self._lock:
            size = self._reader.size
            if whence == 0:
                self._pos = offset
            elif whence == 1:
                self._pos += offset
            elif whence == 2:
                self._pos = size + offset
            else:
                raise ValueError(f"Invalid whence: {whence!r}")
            self._pos = max(0, min(self._pos, size))
        return self._pos

    def tell(self):
        with self._lock:
            return self._pos
