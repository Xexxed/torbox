"""Tests for opening a stream in VLC.

Two layers:

1. Detection and launching -- where VLC is found, and what happens when it is
   not. No VLC and no network, so these always run.
2. The endpoint -- that it resolves the link from the path itself, refuses to
   be told what to open, and reports a missing player instead of failing
   silently.

Isolation: AppState() writes settings.json into the current directory, so the
suite chdirs into a temp dir.
"""

import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import requests

from torbox_downloader import vlc
from torbox_downloader.mount.vfs import TorboxVfs
from tests.test_mount import FakeSession
from tests.test_stream_api import _FakeState, _ServerFixture


def _make_vfs(name="movie.mkv", cdn_base="http://cdn.test"):
    files = [{"name": name, "path": ["T"], "size": "1GB", "selector": "s"}]
    session = FakeSession(torrents=[{"name": "T", "size": "1GB"}],
                          structures={"T": files},
                          cdn_base=cdn_base)
    vfs = TorboxVfs(session)
    vfs.scan()
    return vfs, session


class TestDetection(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("TORBOX_VLC_PATH", None)
        self._which = vlc.shutil.which
        self._candidates = vlc._install_candidates
        self._tmp = tempfile.mkdtemp(prefix="torbox_vlc_test_")
        self.fake = os.path.join(self._tmp, "vlc")
        with open(self.fake, "w") as f:
            f.write("")

    def tearDown(self):
        vlc.shutil.which = self._which
        vlc._install_candidates = self._candidates
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop("TORBOX_VLC_PATH", None)
        if self._env is not None:
            os.environ["TORBOX_VLC_PATH"] = self._env

    def test_path_is_preferred(self):
        vlc.shutil.which = lambda name: self.fake
        self.assertEqual(vlc.vlc_path(), self.fake)
        self.assertTrue(vlc.available())

    def test_install_locations_are_the_fallback(self):
        # The Windows installer does not put VLC on PATH, so this is the
        # normal case rather than an exotic one.
        vlc.shutil.which = lambda name: None
        vlc._install_candidates = lambda: [self.fake]
        self.assertEqual(vlc.vlc_path(), self.fake)

    def test_nothing_found_means_not_available(self):
        vlc.shutil.which = lambda name: None
        vlc._install_candidates = lambda: [os.path.join(self._tmp, "nope")]
        self.assertIsNone(vlc.vlc_path())
        self.assertFalse(vlc.available())

    def test_the_override_wins_over_everything(self):
        os.environ["TORBOX_VLC_PATH"] = self.fake
        vlc.shutil.which = lambda name: "/somewhere/else/vlc"
        vlc._install_candidates = lambda: ["/another/vlc"]
        self.assertEqual(vlc.vlc_path(), self.fake)

    def test_a_broken_override_does_not_fall_back(self):
        # A typo in the override should be visible, not quietly ignored in
        # favour of some other binary the user did not mean.
        os.environ["TORBOX_VLC_PATH"] = os.path.join(self._tmp, "typo.exe")
        vlc.shutil.which = lambda name: self.fake
        vlc._install_candidates = lambda: [self.fake]
        self.assertIsNone(vlc.vlc_path())

    def test_a_blank_override_is_ignored(self):
        os.environ["TORBOX_VLC_PATH"] = "   "
        vlc.shutil.which = lambda name: self.fake
        self.assertEqual(vlc.vlc_path(), self.fake)


class TestOpenUrl(unittest.TestCase):
    def setUp(self):
        self._path = vlc.vlc_path
        self._popen = vlc.subprocess.Popen
        self.launched = []
        vlc.vlc_path = lambda: "/usr/bin/vlc"
        vlc.subprocess.Popen = lambda cmd, **kw: self.launched.append(cmd)

    def tearDown(self):
        vlc.vlc_path = self._path
        vlc.subprocess.Popen = self._popen

    def test_launches_vlc_with_the_url_untouched(self):
        vlc.open_url("https://cdn.test/a.mkv?token=abc")
        self.assertEqual(
            self.launched,
            [["/usr/bin/vlc", "https://cdn.test/a.mkv?token=abc"]],
            "the query string carries the auth token and must be passed through",
        )

    def test_raises_when_vlc_is_missing(self):
        vlc.vlc_path = lambda: None
        with self.assertRaises(vlc.VlcError):
            vlc.open_url("https://cdn.test/a.mkv")
        self.assertEqual(self.launched, [])

    def test_refuses_a_local_file(self):
        # Without this the endpoint would be a way to make the user's own
        # player open arbitrary local files.
        for bad in ("file:///C:/windows/win.ini", r"\\server\share\a.mkv",
                    "C:/movies/a.mkv", "/etc/passwd"):
            with self.subTest(url=bad):
                with self.assertRaises(vlc.VlcError):
                    vlc.open_url(bad)
        self.assertEqual(self.launched, [])

    def test_refuses_an_argument_shaped_like_a_flag(self):
        # A bare argument beginning with a dash would be read by VLC as one of
        # its own options rather than as a URL.
        with self.assertRaises(vlc.VlcError):
            vlc.open_url("--video-filter=invert")
        self.assertEqual(self.launched, [])

    def test_refuses_an_empty_url(self):
        with self.assertRaises(vlc.VlcError):
            vlc.open_url("")
        self.assertEqual(self.launched, [])

    def test_the_auth_token_never_reaches_the_log(self):
        with self.assertLogs("torbox_downloader.vlc", level="INFO") as logs:
            vlc.open_url("https://cdn.test/a.mkv?token=supersecret")
        joined = "\n".join(logs.output)
        self.assertNotIn("supersecret", joined)
        self.assertIn("https://cdn.test/a.mkv", joined)

    def test_returns_the_executable_used(self):
        self.assertEqual(vlc.open_url("https://cdn.test/a.mkv"), "/usr/bin/vlc")

    def test_a_failed_spawn_is_reported_as_a_vlc_error(self):
        def boom(cmd, **kw):
            raise OSError("not executable")

        vlc.subprocess.Popen = boom
        with self.assertRaises(vlc.VlcError):
            vlc.open_url("https://cdn.test/a.mkv")


class TestVlcEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_vlc_api_test_")
        os.chdir(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _post(self, srv, payload):
        return requests.post(srv.base + "/api/stream/vlc", json=payload,
                             headers=srv.headers(), timeout=10)

    def test_launches_vlc_with_the_resolved_link(self):
        vfs, _session = _make_vfs()
        with mock.patch.object(vlc, "open_url") as opened:
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = self._post(srv, {"path": "/T/movie.mkv"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        opened.assert_called_once_with("http://cdn.test/movie.mkv")

    def test_a_url_in_the_request_is_ignored(self):
        """The endpoint must resolve the link itself, never take one.

        Accepting a URL would make this a way to open the user's media player
        on any address a caller named -- a local file, a UNC share, anything.
        """
        vfs, _session = _make_vfs()
        with mock.patch.object(vlc, "open_url") as opened:
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = self._post(srv, {
                    "path": "/T/movie.mkv",
                    "url": "file:///C:/windows/win.ini",
                })
        self.assertEqual(r.status_code, 200)
        opened.assert_called_once_with("http://cdn.test/movie.mkv")

    def test_a_path_only_request_cannot_smuggle_a_url(self):
        vfs, _session = _make_vfs()
        with mock.patch.object(vlc, "open_url") as opened:
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = self._post(srv, {"path": "http://evil.test/a.mkv"})
        # A URL is not a path, so this is either rejected or treated as a
        # (missing) file -- never launched.
        self.assertFalse(opened.called, "a URL must never reach the launcher")
        self.assertIn(r.status_code, (200, 400))

    def test_reports_a_missing_player(self):
        vfs, _session = _make_vfs()
        with mock.patch.object(vlc, "open_url",
                               side_effect=vlc.VlcError("VLC was not found.")):
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = self._post(srv, {"path": "/T/movie.mkv"})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.json()["success"])
        self.assertIn("VLC", r.json()["error"])

    def test_rejects_traversal(self):
        vfs, _session = _make_vfs()
        with mock.patch.object(vlc, "open_url") as opened:
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = self._post(srv, {"path": "/T/../secret"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(opened.called)

    def test_refuses_to_launch_without_a_valid_key(self):
        """Launching a local program is exactly what auth is for here.

        Both the missing and the wrong key are checked. Neither used to be
        testable over the wire: the 401 was written before the request body was
        read, so the connection was reset mid-response and a strict client saw
        a broken server rather than a refusal.
        """
        for label, headers in (("no key", {}),
                               ("wrong key", {"X-API-Key": "not-the-key"})):
            with self.subTest(case=label):
                vfs, _session = _make_vfs()
                with mock.patch.object(vlc, "open_url") as opened:
                    with _ServerFixture(_FakeState(vfs)) as srv:
                        r = requests.post(srv.base + "/api/stream/vlc",
                                          json={"path": "/T/movie.mkv"},
                                          headers=headers, timeout=10)
                self.assertEqual(r.status_code, 401)
                self.assertFalse(opened.called)

    def test_resolving_happens_through_the_vfs(self):
        """The link must come from the library, not from anywhere else."""
        vfs, session = _make_vfs()
        with mock.patch.object(vlc, "open_url") as opened:
            with _ServerFixture(_FakeState(vfs)) as srv:
                self._post(srv, {"path": "/T/movie.mkv"})
        self.assertEqual(len(session.generated), 1,
                         "exactly one link should be generated for the file")
        self.assertTrue(opened.called)


if __name__ == "__main__":
    unittest.main()
