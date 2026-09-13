"""Tests for lazy link resolution and the shared reader cache (Stage 2).

Covers TorboxVfs.resolve_link() caching/refresh/re-expansion, the bounded
get_or_open_reader() cache, and the media classification helpers.

Reuses the FakeSession / make_vfs / RangeServerFixture fixtures from
tests/test_mount.py so the two suites stay in sync.
"""

import time
import unittest

from torbox_downloader.mount.vfs import (
    TorboxVfs,
    classify_kind,
    is_browser_playable,
)
from tests.test_mount import (
    RangeServerFixture,
    make_streaming_vfs,
    make_vfs,
)


class TestResolveLink(unittest.TestCase):
    def test_resolve_link_is_cached(self):
        vfs, session = make_vfs()
        first = vfs.resolve_link("/Torrent One/a.mkv")
        second = vfs.resolve_link("/Torrent One/a.mkv")
        self.assertEqual(first, second)
        # Only one browser link generation for two resolves.
        self.assertEqual(len(session.generated), 1)

    def test_force_refresh_bypasses_the_cache(self):
        vfs, session = make_vfs()
        vfs.resolve_link("/Torrent One/a.mkv")
        vfs.resolve_link("/Torrent One/a.mkv", force_refresh=True)
        self.assertEqual(len(session.generated), 2)

    def test_same_torrent_does_not_reexpand(self):
        vfs, session = make_vfs()
        vfs.resolve_link("/Torrent One/a.mkv")
        self.assertEqual(session.fetch_calls, ["Torrent One"])
        # Second file in the same torrent: already expanded, no re-expansion.
        vfs.resolve_link("/Torrent One/Sub Dir/b.mkv")
        self.assertEqual(session.fetch_calls, ["Torrent One"])

    def test_reexpands_when_current_torrent_differs(self):
        vfs, session = make_vfs()
        vfs.resolve_link("/Torrent One/a.mkv")
        session.current_torrent_name = "Something Else"
        vfs.resolve_link("/Torrent One/a.mkv", force_refresh=True)
        self.assertEqual(session.fetch_calls, ["Torrent One", "Torrent One"])

    def test_resolve_link_rejects_directories(self):
        vfs, _session = make_vfs()
        with self.assertRaises(Exception):
            vfs.resolve_link("/Torrent One")

    def test_resolve_link_uses_original_names(self):
        vfs, session = make_vfs()
        vfs.resolve_link("/Torrent One/Sub Dir/b.mkv")
        # Original (unsanitized) name + selector, not the display name.
        self.assertIn(("b.mkv", "2GB", "s2"), session.generated)


class TestReaderCache(unittest.TestCase):
    def test_same_path_reuses_one_reader(self):
        vfs, _session = make_vfs()
        r1 = vfs.get_or_open_reader("/Torrent One/a.mkv")
        r2 = vfs.get_or_open_reader("/Torrent One/a.mkv")
        self.assertIs(r1, r2)

    def test_idle_reader_is_evicted_after_ttl(self):
        from tests.test_mount import FakeSession
        session = FakeSession(
            torrents=[{"name": "T", "size": "1GB"}],
            structures={"T": [{"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "s"}]},
        )
        vfs = TorboxVfs(session, reader_cache_max=2, reader_idle_ttl=0.05)
        vfs.scan()
        r1 = vfs.get_or_open_reader("/T/a.mkv")
        time.sleep(0.12)
        r2 = vfs.get_or_open_reader("/T/a.mkv")
        self.assertIsNot(r1, r2)

    def test_cache_is_bounded_by_lru(self):
        from tests.test_mount import FakeSession
        session = FakeSession(
            torrents=[{"name": "T", "size": "1GB"}],
            structures={"T": [
                {"name": "a.mkv", "path": ["T"], "size": "1GB", "selector": "s"},
                {"name": "b.mkv", "path": ["T"], "size": "1GB", "selector": "s"},
                {"name": "c.mkv", "path": ["T"], "size": "1GB", "selector": "s"},
            ]},
        )
        vfs = TorboxVfs(session, reader_cache_max=2, reader_idle_ttl=3600)
        vfs.scan()
        ra = vfs.get_or_open_reader("/T/a.mkv")
        vfs.get_or_open_reader("/T/b.mkv")
        vfs.get_or_open_reader("/T/c.mkv")   # evicts a
        self.assertNotIn("/T/a.mkv", vfs._readers)
        self.assertIn("/T/b.mkv", vfs._readers)
        self.assertIn("/T/c.mkv", vfs._readers)

    def test_close_all_readers_empties_the_cache(self):
        vfs, _session = make_vfs()
        vfs.get_or_open_reader("/Torrent One/a.mkv")
        self.assertTrue(vfs._readers)
        vfs.close_all_readers()
        self.assertFalse(vfs._readers)

    def test_refresh_closes_readers(self):
        vfs, _session = make_vfs()
        vfs.get_or_open_reader("/Torrent One/a.mkv")
        vfs.refresh()
        self.assertFalse(vfs._readers)

    def test_cached_reader_still_streams(self):
        payload = b"0123456789abcdef"
        with RangeServerFixture(payload) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(payload))
            reader = vfs.get_or_open_reader("/Torrent One/Sub Dir/b.mkv")
            self.assertEqual(reader.read_range(0, 16), payload)


class TestClassification(unittest.TestCase):
    def test_classify_kind(self):
        cases = {
            "movie.mkv": "video",
            "clip.MP4": "video",
            "show.S01E01.m4v": "video",
            "song.webm": "video",
            "reel.mov": "video",
            "old.avi": "video",
            "stream.ts": "video",
            "photo.jpg": "image",
            "pic.JPEG": "image",
            "art.png": "image",
            "anim.gif": "image",
            "modern.webp": "image",
            "scan.tiff": "image",
            "apple.heic": "image",
            "notes.txt": "other",
            "archive.zip": "other",
            "noextension": "other",
            "": "other",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify_kind(name), expected)

    def test_is_browser_playable(self):
        for name in ("a.mp4", "a.m4v", "a.webm", "a.mov", "a.ogg", "A.MP4"):
            with self.subTest(name=name):
                self.assertTrue(is_browser_playable(name))
        for name in ("a.mkv", "a.avi", "a.ts", "a.m2ts", "a.wmv", "a.flv", "a.jpg", "a"):
            with self.subTest(name=name):
                self.assertFalse(is_browser_playable(name))

    def test_static_methods_delegate(self):
        self.assertEqual(TorboxVfs.classify_kind("x.mp4"), "video")
        self.assertTrue(TorboxVfs.is_browser_playable("x.mp4"))
        self.assertFalse(TorboxVfs.is_browser_playable("x.mkv"))


if __name__ == "__main__":
    unittest.main()
