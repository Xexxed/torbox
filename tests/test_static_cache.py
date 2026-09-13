"""Tests for the static-file cache and its TORBOX_GUI_NOCACHE bypass (Stage 7).

The cache exists so gui/*.js and gui/*.css are read from disk once. The bypass
exists so frontend work does not need a server restart. Both behaviours are
verified against the real handler over a loopback server, including the
acceptance criterion that an edited file is picked up on reload when the flag
is set -- and is *not* picked up when it is unset.

Isolation: the process environment and gui_server._static_cache are snapshotted
in setUp and restored (with an assertion) in tearDown. The edit tests write a
scratch probe file inside gui/ and remove it in a finally block; no tracked
file is ever touched.
"""

import os
import shutil
import tempfile
import unittest

import requests

from tests.test_stream_api import _FakeState, _ServerFixture


class TestStaticCacheBypass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_cwd = os.getcwd()
        cls._tmp = tempfile.mkdtemp(prefix="torbox_cache_test_")
        os.chdir(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._orig_cwd)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        import gui_server
        self._gui_server = gui_server
        self._env_before = os.environ.get("TORBOX_GUI_NOCACHE")
        self._cache_before = dict(gui_server._static_cache)
        gui_server._static_cache.clear()
        os.environ.pop("TORBOX_GUI_NOCACHE", None)

    def tearDown(self):
        self._gui_server._static_cache.clear()
        self._gui_server._static_cache.update(self._cache_before)
        if self._env_before is None:
            os.environ.pop("TORBOX_GUI_NOCACHE", None)
        else:
            os.environ["TORBOX_GUI_NOCACHE"] = self._env_before
        # The isolation rules require proving the environment was restored.
        self.assertEqual(os.environ.get("TORBOX_GUI_NOCACHE"), self._env_before)

    # -- the helper ---------------------------------------------------------

    def test_helper_reflects_the_env_var(self):
        self.assertTrue(self._gui_server._static_cache_enabled())
        os.environ["TORBOX_GUI_NOCACHE"] = "1"
        self.assertFalse(self._gui_server._static_cache_enabled())
        os.environ["TORBOX_GUI_NOCACHE"] = ""
        self.assertTrue(self._gui_server._static_cache_enabled())

    # -- default behaviour is unchanged -------------------------------------

    def test_static_files_are_cached_by_default(self):
        with _ServerFixture(_FakeState(None)) as srv:
            first = requests.get(srv.base + "/index.html", timeout=10)
            second = requests.get(srv.base + "/index.html", timeout=10)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertTrue(
            any(p.endswith("index.html") for p in self._gui_server._static_cache),
            "the static cache should have been populated",
        )

    def test_an_edit_is_not_picked_up_without_the_flag(self):
        probe = self._probe_path()
        try:
            self._write(probe, "// first\n")
            with _ServerFixture(_FakeState(None)) as srv:
                first = requests.get(srv.base + "/_nocache_probe.js", timeout=10)
                self._write(probe, "// second\n")
                second = requests.get(srv.base + "/_nocache_probe.js", timeout=10)
        finally:
            self._remove(probe)

        self.assertIn(b"first", first.content)
        self.assertIn(b"first", second.content,
                      "without the flag the cached copy should still be served")

    # -- the bypass ---------------------------------------------------------

    def test_nocache_serves_without_populating_the_cache(self):
        os.environ["TORBOX_GUI_NOCACHE"] = "1"
        with _ServerFixture(_FakeState(None)) as srv:
            r = requests.get(srv.base + "/index.html", timeout=10)

        self.assertEqual(r.status_code, 200)
        self.assertIn(b"stream-player", r.content)
        self.assertEqual(self._gui_server._static_cache, {},
                         "the bypass must not write to the cache")

    def test_nocache_picks_up_an_edit_without_a_restart(self):
        probe = self._probe_path()
        try:
            self._write(probe, "// first\n")
            os.environ["TORBOX_GUI_NOCACHE"] = "1"
            with _ServerFixture(_FakeState(None)) as srv:
                first = requests.get(srv.base + "/_nocache_probe.js", timeout=10)
                self._write(probe, "// second\n")
                second = requests.get(srv.base + "/_nocache_probe.js", timeout=10)
        finally:
            self._remove(probe)

        self.assertIn(b"first", first.content)
        self.assertIn(b"second", second.content,
                      "with the flag an edited file must be re-read on reload")

    # -- MIME types ---------------------------------------------------------

    def test_stream_assets_are_served_with_the_right_mime_types(self):
        with _ServerFixture(_FakeState(None)) as srv:
            js = requests.get(srv.base + "/stream.js", timeout=10)
            css = requests.get(srv.base + "/style.css", timeout=10)
            html = requests.get(srv.base + "/", timeout=10)

        self.assertEqual(js.status_code, 200)
        self.assertTrue(js.headers["Content-Type"].startswith("application/javascript"))
        self.assertTrue(css.headers["Content-Type"].startswith("text/css"))
        self.assertTrue(html.headers["Content-Type"].startswith("text/html"))

    # -- helpers ------------------------------------------------------------

    def _probe_path(self):
        return os.path.join(os.path.dirname(self._gui_server.__file__),
                            "gui", "_nocache_probe.js")

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def _remove(path):
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
