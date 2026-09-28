"""Tests for the per-operation browser lock (Stage 3).

Verifies that browser-mutating operations serialize against each other, that
the lock is a no-op when not supplied (CLI paths), and that a contended lock
fails fast with BrowserBusyError instead of hanging.
"""

import threading
import time
import unittest

from torbox_downloader.browser_lock import BrowserBusyError, browser_op
from torbox_downloader.mount.vfs import TorboxVfs
from tests.test_mount import FakeSession


def _session():
    return FakeSession(
        torrents=[{"name": "T", "size": "2GB"}],
        structures={"T": [
            {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "sa"},
            {"name": "b.mkv", "path": ["T"], "size": "1GB", "selector": "sb"},
        ]},
    )


class _ConcurrencySession(FakeSession):
    """FakeSession that records how many link generations ran at once."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def generate_download_link(self, name, size, selector, file_id=None, torrent_id=None):
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.05)   # widen the window for overlap to show up
            return super().generate_download_link(name, size, selector, file_id, torrent_id)
        finally:
            with self._guard:
                self._active -= 1


class TestBrowserOpContextManager(unittest.TestCase):
    def test_noop_without_a_lock(self):
        ran = []
        with browser_op(None, timeout=0.01, what="test"):
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_mutual_exclusion(self):
        lock = threading.Lock()
        active = 0
        max_active = 0
        guard = threading.Lock()

        def worker():
            nonlocal active, max_active
            for _ in range(5):
                with browser_op(lock, timeout=5, what="test"):
                    with guard:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.005)
                    with guard:
                        active -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(max_active, 1)

    def test_timeout_raises_browser_busy(self):
        lock = threading.Lock()
        lock.acquire()
        try:
            with self.assertRaises(BrowserBusyError):
                with browser_op(lock, timeout=0.05, what="test"):
                    pass
        finally:
            lock.release()


class TestVfsUsesInjectedLock(unittest.TestCase):
    def test_resolve_link_waits_for_the_shared_lock(self):
        session = _session()
        lock = threading.Lock()
        vfs = TorboxVfs(session, browser_lock=lock, browser_op_timeout=0.05)
        vfs.scan()

        # Hold the lock, then a resolve from another thread must time out.
        lock.acquire()
        try:
            result = {}

            def run():
                try:
                    vfs.resolve_link("/T/a.mkv")
                    result["error"] = None
                except BrowserBusyError as e:
                    result["error"] = e

            t = threading.Thread(target=run)
            t.start()
            t.join(timeout=5)
            self.assertIsInstance(result.get("error"), BrowserBusyError)
        finally:
            lock.release()

    def test_concurrent_resolves_do_not_overlap(self):
        session = _ConcurrencySession(
            torrents=[{"name": "T", "size": "2GB"}],
            structures={"T": [
                {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "sa"},
                {"name": "b.mkv", "path": ["T"], "size": "1GB", "selector": "sb"},
            ]},
        )
        lock = threading.Lock()
        vfs = TorboxVfs(session, browser_lock=lock, browser_op_timeout=5)
        vfs.scan()

        threads = [
            threading.Thread(target=vfs.resolve_link, args=(path,))
            for path in ("/T/a.mkv", "/T/b.mkv")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(session.max_active, 1)


class TestVfsWithoutLock(unittest.TestCase):
    """The HTTP backend holds no page, so it passes no lock at all.

    This is the mechanism that removes the serialization: with browser_lock
    None, `browser_op` is the no-op it documents itself as, so concurrent
    resolves actually overlap instead of queueing.
    """

    def test_resolve_does_not_block_on_a_held_lock(self):
        session = _ConcurrencySession(
            torrents=[{"name": "T", "size": "2GB"}],
            structures={"T": [
                {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "sa"},
                {"name": "b.mkv", "path": ["T"], "size": "1GB", "selector": "sb"},
            ]},
        )
        lock = threading.Lock()
        vfs = TorboxVfs(session, browser_lock=None, browser_op_timeout=0.05)
        vfs.scan()

        # Hold a lock the VFS was never given: it must not matter.
        lock.acquire()
        try:
            threads = [
                threading.Thread(target=vfs.resolve_link, args=(path,))
                for path in ("/T/a.mkv", "/T/b.mkv")
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertFalse(any(t.is_alive() for t in threads),
                             "resolves must not block when no lock is supplied")
            self.assertGreater(session.max_active, 1,
                               "resolves should overlap without a lock")
        finally:
            lock.release()

    def test_a_none_lock_never_raises_browser_busy(self):
        vfs = TorboxVfs(FakeSession(), browser_lock=None, browser_op_timeout=0.0)
        vfs.scan()
        with browser_op(vfs._browser_lock, vfs._browser_op_timeout, "test"):
            pass  # must not raise


class TestFileIdPlumbing(unittest.TestCase):
    """The VFS must address files exactly when the backend supplies ids."""

    def _vfs(self):
        session = FakeSession(
            torrents=[{"name": "T", "size": "2GB"}],
            structures={"T": [
                {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "sa",
                 "size_bytes": 1073741824, "torrent_id": 7, "file_id": 0},
                {"name": "b.mkv", "path": ["T"], "size": "1GB", "selector": "sb",
                 "size_bytes": 1073741824, "torrent_id": 7, "file_id": 2},
            ]},
        )
        return session, TorboxVfs(session)

    def test_resolve_passes_exact_ids(self):
        session, vfs = self._vfs()
        vfs.scan()
        vfs.resolve_link("/T/b.mkv")
        # recorded as (name, size, selector, file_id, torrent_id)
        self.assertEqual(session.generated[-1], ("b.mkv", "1GB", "sb", 2, 7))

    def test_a_file_id_of_zero_is_still_passed_through(self):
        session, vfs = self._vfs()
        vfs.scan()
        vfs.resolve_link("/T/a.mkv")
        self.assertEqual(session.generated[-1], ("a.mkv", "1GB", "sa", 0, 7))

    def test_size_bytes_prefers_the_exact_count(self):
        _session, vfs = self._vfs()
        vfs.scan()
        node = vfs._resolve("/T/b.mkv")
        self.assertEqual(node.size_bytes, 1073741824)

    def test_browser_style_entries_still_fall_back_to_name_and_size(self):
        session = FakeSession(
            torrents=[{"name": "T", "size": "2GB"}],
            structures={"T": [
                {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "sa"},
            ]},
        )
        vfs = TorboxVfs(session)
        vfs.scan()
        vfs.resolve_link("/T/a.mkv")
        self.assertEqual(session.generated[-1], ("a.mkv", "1GB", "sa", None, None))


if __name__ == "__main__":
    unittest.main()
