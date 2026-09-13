"""Tests for the shared VFS provider on AppState.

Covers the Stage 1 contract: one VFS instance shared by the drive mount and
the Stream page, rebuilt when the browser session is replaced, dropped on
logout/token expiry, and left intact by an unmount.

Isolation: the AppState constructor writes settings.json into the current
working directory, so these tests chdir into a temp directory first and
restore the original cwd in tearDown. The browser session is stubbed so no
Playwright process is ever launched.
"""

import os
import shutil
import tempfile
import unittest


class _FakeSession:
    """Minimal stand-in for TorboxBrowserSession (no Playwright)."""

    def __init__(self, name="fake"):
        self.name = name
        self.current_torrent_name = None
        self.scan_calls = 0
        self.link_calls = []

    def scan_torrents(self):
        self.scan_calls += 1
        return [{"name": f"Torrent {self.name}", "size": "1 GB"}]

    def fetch_structure(self, torrent_name):
        self.current_torrent_name = torrent_name
        return [{"name": "movie.mkv", "path": [torrent_name], "size": "1 GB", "selector": "sel"}]

    def generate_download_link(self, name, size, selector):
        self.link_calls.append((name, size, selector))
        return f"https://cdn.example/{name}?s={selector}"

    def stop(self):
        pass


class TestSharedVfs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_vfs_test_")
        os.chdir(cls._tmp)
        # Import after chdir so module-level AppState() writes into the temp dir.
        import gui_server
        cls.gui_server = gui_server

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.session = _FakeSession("a")
        self.state = self.gui_server.AppState()
        # Never let get_vfs() build a real browser session.
        self.state.get_or_create_session = lambda *a, **k: self.session

    def test_get_vfs_is_idempotent(self):
        first = self.state.get_vfs()
        second = self.state.get_vfs()
        self.assertIs(first, second)
        self.assertIs(first._session, self.session)

    def test_get_vfs_rebuilds_when_session_is_replaced(self):
        first = self.state.get_vfs()
        new_session = _FakeSession("b")
        self.state.session = new_session
        self.state.get_or_create_session = lambda *a, **k: new_session
        second = self.state.get_vfs()
        self.assertIsNot(first, second)
        self.assertIs(second._session, new_session)

    def test_drop_vfs_forces_a_fresh_instance(self):
        first = self.state.get_vfs()
        self.state.drop_vfs()
        second = self.state.get_vfs()
        self.assertIsNot(first, second)

    def test_unmount_does_not_clear_the_shared_vfs(self):
        shared = self.state.get_vfs()
        # unmount_sync with nothing mounted is a no-op teardown; it must only
        # clear the manager's own reference.
        self.state.mount_manager.unmount_sync()
        self.assertIs(self.state.get_vfs(), shared)

    def test_get_vfs_raises_without_a_session(self):
        self.state.get_or_create_session = lambda *a, **k: None
        with self.assertRaises(RuntimeError):
            self.state.get_vfs()


if __name__ == "__main__":
    unittest.main()
