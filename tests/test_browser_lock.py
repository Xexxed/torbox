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

    def generate_download_link(self, name, size, selector):
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.05)   # widen the window for overlap to show up
            return super().generate_download_link(name, size, selector)
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


if __name__ == "__main__":
    unittest.main()
