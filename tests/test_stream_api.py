"""API tests for the Stream listing endpoints (Stage 4).

Runs the real GUIRequestHandler over a loopback HTTP server with a fake
AppState, so request parsing, API-key enforcement, path validation and the
"no links in listings" guarantee are all exercised end to end.

Isolation: gui_server's module-level AppState() writes settings.json into the
current directory, so the suite chdirs into a temp dir first and restores it
afterwards. The real app_state is swapped for a fake and restored in tearDown.
"""

import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import requests

from tests.test_mount import FakeSession


class _FakeState:
    """Stand-in for AppState: only what the Stream endpoints touch."""

    def __init__(self, vfs, api_key="test-key"):
        self._vfs = vfs
        self.api_key = api_key
        self.expired_calls = 0
        self.browser_op_lock = threading.Lock()
        self.browser_op_lock_timeout_s = 5.0

    def get_vfs(self):
        return self._vfs

    def handle_token_expired(self):
        self.expired_calls += 1


class _ExpiringVfs:
    def listdir_entries(self, path):
        raise RuntimeError("Your Torbox session token has expired or is invalid.")


class _ServerFixture:
    """Serve GUIRequestHandler on a loopback port with an injected AppState."""

    def __init__(self, state):
        self._gui_server = None
        self.state = state

    def __enter__(self):
        import gui_server
        self._gui_server = gui_server
        self._orig_state = gui_server.app_state
        gui_server.app_state = self.state
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui_server.GUIRequestHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._gui_server.app_state = self._orig_state
        return False

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def headers(self, key="test-key"):
        return {"X-API-Key": key} if key is not None else {}


class TestStreamApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_api_test_")
        os.chdir(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _make_vfs(self):
        from tests.test_mount import make_vfs
        vfs, session = make_vfs()
        return vfs, session

    def test_library_lists_torrents_without_links(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.get(srv.base + "/api/stream/library", headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        names = [e["name"] for e in body["entries"]]
        self.assertIn("Torrent One", names)
        for entry in body["entries"]:
            self.assertNotIn("url", entry)
            self.assertTrue(entry["is_dir"])
            self.assertEqual(entry["kind"], "folder")

    def test_list_returns_placeholders_without_links(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/list",
                              json={"path": "/Torrent One"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        by_name = {e["name"]: e for e in body["entries"]}
        self.assertTrue(by_name["a.mkv"]["is_dir"] is False)
        self.assertTrue(by_name["Sub Dir"]["is_dir"] is True)
        self.assertEqual(by_name["a.mkv"]["kind"], "video")
        self.assertFalse(by_name["a.mkv"]["playable"])
        self.assertEqual(by_name["a.mkv"]["size_str"], "1GB")
        for entry in body["entries"]:
            self.assertNotIn("url", entry)

    def test_list_rejects_traversal(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/list",
                              json={"path": "/Torrent One/../../etc"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["success"])

    def test_list_rejects_empty_path(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/list",
                              json={"path": "   "},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 400)

    def test_requires_api_key(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.get(srv.base + "/api/stream/library", timeout=10)
        self.assertEqual(r.status_code, 401)

    def test_unknown_path_returns_error_body_not_500(self):
        vfs, _session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/list",
                              json={"path": "/No Such Torrent"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertIn("error", body)

    def test_refresh_rescans(self):
        vfs, session = self._make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/refresh",
                              json={}, headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertEqual(r.json()["torrents"], 2)

    def test_token_expiry_logs_out_once(self):
        state = _FakeState(_ExpiringVfs())
        with _ServerFixture(state) as srv:
            r = requests.get(srv.base + "/api/stream/library",
                             headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertIn("expired", body["error"].lower())
        self.assertEqual(state.expired_calls, 1)


if __name__ == "__main__":
    unittest.main()
