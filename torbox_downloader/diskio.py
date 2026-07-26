"""
Disk I/O helpers focused on reducing HDD load during downloads.

The download hot path used to be the single biggest source of disk pressure:
several threads writing 512 KB chunks at widely separated offsets of the same
file. On a spinning disk that pattern is close to a worst case - every switch
between segments costs a physical head seek, so the drive reports ~100% "active
time" while delivering only a fraction of its sequential throughput.

This module provides the primitives used to fix that:

* ``detect_disk_type``  - is the destination on a rotational disk or an SSD?
* ``allocate_file``     - real (non-sparse) preallocation to avoid fragmentation.
* ``SegmentWriter``     - coalesces many small chunks into few large writes.
* ``WriteThrottle``     - caps how many write streams touch a disk at once.
* ``drop_cache_range``  - hints the OS to evict already-flushed pages.
"""

import os
import sys
import errno
import logging
import threading

logger = logging.getLogger(__name__)

# Default coalescing buffer per write stream. Chunks are accumulated in RAM and
# handed to the OS as one large sequential write, which is what turns a seek
# storm into a handful of ordered writes.
DEFAULT_BUFFER_BYTES = 8 * 1024 * 1024      # 8 MB on SSD/unknown
HDD_BUFFER_BYTES = 32 * 1024 * 1024         # 32 MB on rotational disks

# Network read granularity. Larger than the old 512 KB so fewer, bigger appends
# hit the coalescing buffer.
DEFAULT_CHUNK_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Disk type detection
# ---------------------------------------------------------------------------

_disk_type_cache = {}
_disk_type_lock = threading.Lock()


def _detect_disk_type_linux(path):
    """Read /sys/block/<dev>/queue/rotational for the device backing ``path``."""
    try:
        st = os.stat(path)
        major, minor = os.major(st.st_dev), os.minor(st.st_dev)
        dev_link = "/sys/dev/block/%d:%d" % (major, minor)
        real = os.path.realpath(dev_link)

        # Walk up from the partition to its parent disk (sda1 -> sda).
        candidates = [real, os.path.dirname(real)]
        for cand in candidates:
            rot_file = os.path.join(cand, "queue", "rotational")
            if os.path.exists(rot_file):
                with open(rot_file, "r") as f:
                    return "hdd" if f.read().strip() == "1" else "ssd"
    except Exception as e:  # pragma: no cover - platform specific
        logger.debug("Linux disk detection failed for %s: %s", path, e)
    return "unknown"


def _detect_disk_type_windows(path):
    """Ask Windows (MSFT_PhysicalDisk) whether the volume is on an HDD."""
    try:
        import subprocess

        drive = os.path.splitdrive(os.path.abspath(path))[0].rstrip(":")
        if not drive:
            return "unknown"

        # MediaType: 3 = HDD, 4 = SSD. Partition -> Disk -> PhysicalDisk.
        ps = (
            "$ErrorActionPreference='Stop';"
            "$p = Get-Partition -DriveLetter %s;"
            "$d = Get-PhysicalDisk | Where-Object DeviceId -eq $p.DiskNumber;"
            "$d.MediaType" % drive
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=12,
            creationflags=creationflags,
        ).stdout.strip().lower()

        if "hdd" in out:
            return "hdd"
        if "ssd" in out:
            return "ssd"
    except Exception as e:  # pragma: no cover - platform specific
        logger.debug("Windows disk detection failed for %s: %s", path, e)
    return "unknown"


def detect_disk_type(path):
    """
    Return ``"hdd"``, ``"ssd"`` or ``"unknown"`` for the volume holding ``path``.

    The result is cached per resolved mount point; detection can shell out to
    PowerShell on Windows and must never run inside the download loop.
    """
    probe = os.path.abspath(path or ".")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not probe or not os.path.exists(probe):
        probe = os.path.expanduser("~")

    key = os.path.splitdrive(probe)[0] or probe
    with _disk_type_lock:
        if key in _disk_type_cache:
            return _disk_type_cache[key]

    if sys.platform.startswith("win"):
        result = _detect_disk_type_windows(probe)
    elif sys.platform.startswith("linux"):
        result = _detect_disk_type_linux(probe)
    else:
        # macOS: internal storage has been flash-only for many years.
        result = "ssd"

    with _disk_type_lock:
        _disk_type_cache[key] = result
    logger.info("Detected storage type for %s: %s", key, result)
    return result


def resolve_disk_profile(dest_dir, configured="auto"):
    """Map the user setting (``auto``/``hdd``/``ssd``) to a concrete profile."""
    configured = (configured or "auto").lower()
    if configured in ("hdd", "ssd"):
        return configured
    detected = detect_disk_type(dest_dir)
    # Treat "unknown" conservatively as HDD-ish is too slow for SSD users, so
    # fall back to the balanced SSD profile but with coalesced writes on.
    return detected if detected in ("hdd", "ssd") else "ssd"


def recommended_connections(profile, requested):
    """
    Clamp the number of parallel byte-range connections for a single file.

    On a rotational disk every extra connection adds another write cursor and
    therefore another seek target, so we deliberately serialise to one stream.
    """
    requested = max(1, int(requested or 1))
    if profile == "hdd":
        return 1
    return min(requested, 4)


def recommended_buffer_bytes(profile, override_mb=None):
    if override_mb:
        try:
            mb = float(override_mb)
            if mb > 0:
                return int(mb * 1024 * 1024)
        except (TypeError, ValueError):
            pass
    return HDD_BUFFER_BYTES if profile == "hdd" else DEFAULT_BUFFER_BYTES


# ---------------------------------------------------------------------------
# Preallocation
# ---------------------------------------------------------------------------

def allocate_file(path, size):
    """
    Create ``path`` with ``size`` bytes of *really allocated* space.

    The previous implementation did ``seek(size - 1); write(b"\\x00")`` which on
    ext4/XFS/APFS produces a sparse file. Filling a sparse file from several
    offsets at once makes the allocator interleave extents from each writer, so
    the finished file ends up heavily fragmented - every later read of that file
    then costs extra seeks too.

    ``posix_fallocate`` reserves contiguous space up front. On Windows,
    ``truncate`` maps to ``SetEndOfFile``, which allocates clusters without
    marking the file sparse, so it is already the right primitive there.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "wb") as f:
        if size <= 0:
            return True
        fd = f.fileno()
        fallocate = getattr(os, "posix_fallocate", None)
        if fallocate is not None:
            try:
                fallocate(fd, 0, size)
                return True
            except OSError as e:
                # ENOSYS/EOPNOTSUPP: filesystem has no fallocate (e.g. some
                # network mounts). ENOSPC must propagate as a real failure.
                if e.errno == errno.ENOSPC:
                    raise
                if e.errno not in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
                    logger.debug("posix_fallocate failed on %s: %s", path, e)
        f.truncate(size)
    return True


# ---------------------------------------------------------------------------
# Cache hints
# ---------------------------------------------------------------------------

def drop_cache_range(fd, offset, length):
    """
    Tell the kernel the given already-written range is not needed again.

    Without this, a multi-gigabyte download fills the page cache with dirty and
    then clean pages; the kernel eventually flushes them in bursts, which is
    part of why disk utilisation spikes to 100% in waves. A no-op on Windows.
    """
    fadvise = getattr(os, "posix_fadvise", None)
    if fadvise is None or length <= 0:
        return
    try:
        fadvise(fd, offset, length, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Write throttling
# ---------------------------------------------------------------------------

class WriteThrottle:
    """
    Bounds the number of write streams that may touch the disk concurrently.

    ``max_parallel_files`` (up to 5) multiplied by per-file connections (up to
    5) previously allowed 25 independent write cursors on one drive. This
    process-wide gate keeps the real number of interleaved streams sane no
    matter how the two settings are combined.
    """

    def __init__(self, limit=4):
        self._limit = max(1, int(limit))
        self._sem = threading.BoundedSemaphore(self._limit)

    @property
    def limit(self):
        return self._limit

    def configure(self, limit):
        limit = max(1, int(limit))
        if limit == self._limit:
            return
        self._limit = limit
        self._sem = threading.BoundedSemaphore(limit)

    def acquire(self, timeout=None):
        return self._sem.acquire(timeout=timeout) if timeout else self._sem.acquire()

    def release(self):
        try:
            self._sem.release()
        except ValueError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# One gate for the whole process; sized on first use from the disk profile.
write_throttle = WriteThrottle(4)


def configure_write_throttle(profile, max_parallel_files=1):
    """Pick a sensible global stream limit for the detected disk profile."""
    if profile == "hdd":
        limit = 1 if max_parallel_files <= 1 else 2
    else:
        limit = max(2, min(int(max_parallel_files or 1) * 2, 8))
    write_throttle.configure(limit)
    return write_throttle.limit


# ---------------------------------------------------------------------------
# Buffered segment writer
# ---------------------------------------------------------------------------

class SegmentWriter:
    """
    Buffered writer that flushes large, aligned blocks at a fixed file offset.

    Callers ``append`` network chunks; the writer holds them in memory until
    ``buffer_bytes`` is reached and then issues a single ``pwrite``/``write``.
    Turning 512 KB writes into 8-32 MB writes cuts the number of seeks (and of
    write syscalls) by one to two orders of magnitude on the same download.
    """

    __slots__ = ("_path", "_offset", "_buffer_bytes", "_parts", "_pending",
                 "_fh", "_fd", "_use_pwrite", "_written", "_flush_start",
                 "_drop_cache")

    def __init__(self, path, offset, buffer_bytes=DEFAULT_BUFFER_BYTES,
                 drop_cache=False):
        self._path = path
        self._offset = int(offset)
        self._buffer_bytes = max(64 * 1024, int(buffer_bytes))
        self._parts = []
        self._pending = 0
        self._written = 0
        self._flush_start = int(offset)
        self._drop_cache = drop_cache
        # Unbuffered handle: SegmentWriter already does the buffering, a second
        # layer of Python-side buffering would only add a memcpy.
        self._fh = open(path, "r+b", buffering=0)
        self._fd = self._fh.fileno()
        self._use_pwrite = hasattr(os, "pwrite")
        if not self._use_pwrite:
            self._fh.seek(self._offset)

    @property
    def bytes_written(self):
        """Bytes handed to this writer, including data still buffered."""
        return self._written + self._pending

    def append(self, data):
        if not data:
            return
        self._parts.append(data)
        self._pending += len(data)
        if self._pending >= self._buffer_bytes:
            self.flush()

    def flush(self):
        if not self._pending:
            return
        blob = b"".join(self._parts) if len(self._parts) > 1 else self._parts[0]
        self._parts = []
        pending, self._pending = self._pending, 0

        pos = self._offset + self._written
        if self._use_pwrite:
            view = memoryview(blob)
            done = 0
            while done < pending:
                done += os.pwrite(self._fd, view[done:], pos + done)
        else:
            self._fh.seek(pos)
            self._fh.write(blob)
        self._written += pending

        if self._drop_cache:
            drop_cache_range(self._fd, self._flush_start, pos + pending - self._flush_start)
            self._flush_start = pos + pending

    def close(self):
        try:
            self.flush()
        finally:
            try:
                self._fh.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
