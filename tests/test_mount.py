"""Tests for the virtual drive mount feature.

Covers the VFS tree model, the zero-temp ranged streaming reader, the
seekable file adapter, drive-letter picking, and (when the optional
dependencies are installed) the WebDAV provider end-to-end and the WinFsp
operations layer. Tests that need winfspy/wsgidav skip gracefully when
those packages are absent.
"""

import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

try:
    from cheroot import wsgi as _cheroot_wsgi

    HAVE_CHEROOT = True
except ImportError:
    HAVE_CHEROOT = False

try:
    import wsgidav  # noqa: F401

    HAVE_WSGIDAV = True
except ImportError:
    HAVE_WSGIDAV = False

try:
    from winfspy import (
        FILE_ATTRIBUTE,
        NTStatusEndOfFile,
        NTStatusMediaWriteProtected,
        NTStatusObjectNameNotFound,
    )

    from torbox_downloader.mount.winfsp_fs import TorboxFsOperations

    HAVE_WINFSPY = True
except ImportError:
    HAVE_WINFSPY = False

from torbox_downloader.mount.manager import find_free_port, pick_free_drive_letter
from torbox_downloader.mount.streaming import LazyRemoteFile, LinkExpiredError
from torbox_downloader.mount.vfs import TorboxVfs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSession:
    """Mimics the parts of a TorBox session the VFS talks to.

    Works for both backends: `generate_download_link` accepts the optional
    file_id/torrent_id keyword that the HTTP backend prefers, and records
    which form was used so tests can assert the VFS passes exact identifiers
    when it has them.
    """

    def __init__(self, torrents=None, structures=None, cdn_base="http://cdn.test"):
        self.torrents = torrents or []
        self.structures = structures or {}
        self.cdn_base = cdn_base.rstrip("/")
        self.current_torrent_name = None
        self.fetch_calls = []
        self.generated = []

    def scan_torrents(self):
        return list(self.torrents)

    def fetch_structure(self, name):
        self.fetch_calls.append(name)
        self.current_torrent_name = name
        return list(self.structures.get(name, []))

    def generate_download_link(self, name, size, selector, file_id=None, torrent_id=None):
        from urllib.parse import quote

        self.generated.append((name, size, selector, file_id, torrent_id))
        return f"{self.cdn_base}/{quote(name or '')}"

    def viewing_torrent_name(self):
        return {"torrent": self.current_torrent_name, "on_download_view": True}


def make_vfs(cdn_base="http://cdn.test"):
    session = FakeSession(
        torrents=[
            {"name": "Torrent One", "size": "3GB"},
            {"name": "Bad/Torrent:Name?", "size": "1GB"},
        ],
        structures={
            "Torrent One": [
                {"name": "a.mkv", "path": ["Torrent One"], "size": "1GB", "selector": "s1"},
                {
                    "name": "b.mkv",
                    "path": ["Torrent One", "Sub Dir"],
                    "size": "2GB",
                    "selector": "s2",
                },
            ],
            "Bad/Torrent:Name?": [
                {"name": "x <1>.mkv", "path": ["Bad/Torrent:Name?"], "size": "1GB", "selector": "sx"},
            ],
        },
        cdn_base=cdn_base,
    )
    vfs = TorboxVfs(session)
    vfs.scan()
    return vfs, session


def make_streaming_vfs(cdn_base, payload_len):
    """VFS whose file sizes exactly match a small local fixture blob, so the
    reader can stream the whole file from the test HTTP server."""
    size_str = f"{payload_len}B"
    session = FakeSession(
        torrents=[
            {"name": "Torrent One", "size": size_str},
            {"name": "Bad/Torrent:Name?", "size": size_str},
        ],
        structures={
            "Torrent One": [
                {"name": "a.mkv", "path": ["Torrent One"], "size": size_str, "selector": "s1"},
                {
                    "name": "b.mkv",
                    "path": ["Torrent One", "Sub Dir"],
                    "size": size_str,
                    "selector": "s2",
                },
            ],
            "Bad/Torrent:Name?": [
                {"name": "x <1>.mkv", "path": ["Bad/Torrent:Name?"], "size": size_str, "selector": "sx"},
            ],
        },
        cdn_base=cdn_base,
    )
    vfs = TorboxVfs(session)
    vfs.scan()
    return vfs, session


class _RangeHandler(BaseHTTPRequestHandler):
    """Serves a fixture blob with HTTP range support; simulates CDN quirks."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        server = self.server
        data = server.data

        if getattr(server, "reject_once", False) and not getattr(server, "_rejected", False):
            server._rejected = True
            body = b""
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return

        server.hit_count += 1
        match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            if start >= len(data) or start > end:
                body = b""
                self.send_response(416)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return
            body = data[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RangeServerFixture:
    """Background HTTP server exposing one fixture blob with Range support."""

    def __init__(self, data):
        self.data = data
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.httpd.data = data
        self.httpd.hit_count = 0
        self.httpd.reject_once = False
        self.httpd._rejected = False
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/file.bin"

    @property
    def hit_count(self):
        return self.httpd.hit_count

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# VFS model
# ---------------------------------------------------------------------------


class TestVfs(unittest.TestCase):
    def test_root_listing_and_sanitization(self):
        vfs, _session = make_vfs()
        entries = dict(vfs.listdir("/"))
        self.assertIn("Torrent One", entries)
        # Windows-invalid characters are replaced and trailing punctuation trimmed.
        self.assertIn("Bad_Torrent_Name_", entries)
        self.assertTrue(all(is_dir for is_dir in entries.values()))

    def test_stat_sizes_parsed_to_bytes(self):
        vfs, _session = make_vfs()
        st = vfs.stat("/Torrent One/Sub Dir/b.mkv")
        self.assertFalse(st["is_dir"])
        self.assertEqual(st["size"], 2 * 1024**3)

    def test_nested_subdirectories(self):
        vfs, _session = make_vfs()
        listing = dict(vfs.listdir("/Torrent One"))
        self.assertEqual(listing, {"a.mkv": False, "Sub Dir": True})
        self.assertEqual(dict(vfs.listdir("/Torrent One/Sub Dir")), {"b.mkv": False})

    def test_missing_paths_raise(self):
        vfs, _session = make_vfs()
        with self.assertRaises(Exception):
            vfs.stat("/Nope/file.mkv")
        with self.assertRaises(Exception):
            vfs.listdir("/Torrent One/Missing")

    def test_lazy_expansion_runs_once_per_torrent(self):
        vfs, session = make_vfs()
        for _ in range(3):
            vfs.listdir("/Torrent One")
            vfs.stat("/Torrent One/a.mkv")
            vfs.listdir("/Bad_Torrent_Name_")
            vfs.stat("/Bad_Torrent_Name_/x _1_.mkv")
        self.assertEqual(sorted(session.fetch_calls), ["Bad/Torrent:Name?", "Torrent One"])

    def test_open_reader_streams_via_original_names(self):
        payload = b"0123456789abcdef"
        with RangeServerFixture(payload) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, session = make_streaming_vfs(base, len(payload))
            reader = vfs.open_reader("/Torrent One/Sub Dir/b.mkv")
            self.assertEqual(reader.size, 16)
            data = reader.read_range(0, 16)
            self.assertEqual(data, payload)
            # Link generation used the original (unsanitized) names + selector.
            self.assertIn(("b.mkv", "16B", "s2", None, None), session.generated)

    def test_total_bytes_aggregated(self):
        vfs, _session = make_vfs()
        self.assertEqual(vfs.total_bytes, 4 * 1024**3)


# ---------------------------------------------------------------------------
# ChunkedRemoteReader / LazyRemoteFile
# ---------------------------------------------------------------------------


class TestChunkedRemoteReader(unittest.TestCase):
    def test_random_access_matches_fixture(self):
        payload = bytes(range(256)) * 4096  # 1 MiB deterministic blob
        with RangeServerFixture(payload) as remote:
            reader = _make_reader(remote.url, len(payload))
            self.assertEqual(reader.read_range(0, 10), payload[:10])
            self.assertEqual(reader.read_range(500000, 100), payload[500000:500100])
            self.assertEqual(reader.read_range(len(payload) - 5, 100), payload[-5:])
            self.assertEqual(reader.read_range(0, 0), b"")
            self.assertEqual(reader.read_range(len(payload), 5), b"")

    def test_chunks_are_cached_across_reads(self):
        payload = b"x" * (64 * 1024)  # small chunk size -> several chunks
        with RangeServerFixture(payload) as remote:
            reader = _make_reader(remote.url, len(payload), chunk_size=16384)
            reader.read_range(0, 100)
            first_hits = remote.hit_count
            self.assertGreaterEqual(first_hits, 1)
            reader.read_range(0, 100)  # same chunk again -> served from cache
            self.assertEqual(remote.hit_count, first_hits)

    def test_expired_link_is_regenerated_once(self):
        payload = b"payload-bytes"
        with RangeServerFixture(payload) as remote:
            remote.httpd.reject_once = True
            refreshes = []

            def url_provider(force_refresh=False):
                refreshes.append(force_refresh)
                return remote.url

            reader = _make_reader(remote.url, len(payload), url_provider=url_provider)
            self.assertEqual(reader.read_range(0, len(payload)), payload)
            self.assertEqual(refreshes, [False, True])

    def test_double_expiry_surfaces_ioerror(self):
        from torbox_downloader.mount.streaming import ChunkedRemoteReader

        def fetch_403(url, start, end):
            raise LinkExpiredError("expired")

        reader = ChunkedRemoteReader(13, lambda force_refresh: "http://unused/")
        reader._fetch_range = fetch_403  # simulate permanent CDN expiry
        with self.assertRaises(IOError):
            reader.read_range(0, 4)

    def test_lazy_remote_file_seek_read_tell(self):
        payload = bytes(range(256)) * 64

        class _FakeReader:
            def __init__(self, blob):
                self.blob = blob
                self.size = len(blob)

            def read_range(self, offset, length):
                return self.blob[offset : offset + length]

        handle = LazyRemoteFile(_FakeReader(payload))
        self.assertTrue(handle.seekable())
        self.assertFalse(handle.writable())
        self.assertEqual(handle.read(4), payload[:4])
        self.assertEqual(handle.tell(), 4)
        self.assertEqual(handle.seek(-2, 1), 2)
        self.assertEqual(handle.read(3), payload[2:5])
        self.assertEqual(handle.seek(-1, 2), len(payload) - 1)
        self.assertEqual(handle.read(10), payload[-1:])


def _make_reader(url, size, url_provider=None, **kwargs):
    from torbox_downloader.mount.streaming import ChunkedRemoteReader

    if url_provider is None:

        def url_provider(force_refresh=False):
            return url

    return ChunkedRemoteReader(size, url_provider, **kwargs)


# ---------------------------------------------------------------------------
# Drive letter picking
# ---------------------------------------------------------------------------


class TestDriveLetterPicking(unittest.TestCase):
    def test_prefers_z_when_empty(self):
        self.assertEqual(pick_free_drive_letter("", used=set()), "Z:")

    def test_skips_used_letters(self):
        self.assertEqual(pick_free_drive_letter("", used={"Z", "Y"}), "X:")

    def test_honours_preference_when_free(self):
        self.assertEqual(pick_free_drive_letter("T", used=set()), "T:")
        self.assertEqual(pick_free_drive_letter("T:", used={"T"}), "Z:")

    def test_returns_none_when_all_taken(self):
        all_letters = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.assertIsNone(pick_free_drive_letter("", used=all_letters))

    def test_find_free_port_binds_locally(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)


# ---------------------------------------------------------------------------
# WebDAV provider (end-to-end against a real WsgiDAV/cheroot server)
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_WSGIDAV and HAVE_CHEROOT, "wsgidav/cheroot not installed")
class TestDavProviderE2E(unittest.TestCase):
    PAYLOAD = b"dav-content:" * 32

    @classmethod
    def setUpClass(cls):
        cls.remote = RangeServerFixture(cls.PAYLOAD)
        cls.remote.__enter__()
        base = cls.remote.url.split("/file.bin")[0]

        cls.vfs, _session = make_streaming_vfs(base, len(cls.PAYLOAD))
        # Point every generated link at the local fixture blob regardless of name.
        original_session = cls.vfs._session

        def fixed_link(name, size, selector, file_id=None, torrent_id=None):
            original_session.generated.append((name, size, selector, file_id, torrent_id))
            return cls.remote.url

        original_session.generate_download_link = fixed_link

        from torbox_downloader.mount.dav_provider import create_dav_server

        port = find_free_port()
        cls.dav = create_dav_server(cls.vfs, "127.0.0.1", port)
        cls.thread = threading.Thread(target=cls.dav.start, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{port}"
        deadline = 15.0
        while deadline > 0:
            try:
                resp = requests.request("OPTIONS", cls.base_url, timeout=2)
                if resp.status_code < 500:
                    break
            except Exception:
                pass
            deadline -= 0.25
            threading.Event().wait(0.25)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.dav.stop()
        except Exception:
            pass
        cls.remote.__exit__()

    def test_propfind_root_lists_torrents(self):
        resp = requests.request("PROPFIND", self.base_url + "/", headers={"Depth": "1"}, timeout=10)
        self.assertEqual(resp.status_code, 207)
        self.assertIn("Torrent%20One", resp.text)

    def test_head_reports_content_length(self):
        resp = requests.head(f"{self.base_url}/Torrent%20One/Sub%20Dir/b.mkv", timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(int(resp.headers.get("Content-Length", "-1")), len(self.PAYLOAD))

    def test_get_streams_full_file_without_temp(self):
        resp = requests.get(f"{self.base_url}/Torrent%20One/Sub%20Dir/b.mkv", timeout=30)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, self.PAYLOAD)

    def test_get_honors_range_requests(self):
        resp = requests.get(
            f"{self.base_url}/Torrent%20One/Sub%20Dir/b.mkv",
            headers={"Range": f"bytes=2-7"},
            timeout=30,
        )
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.content, self.PAYLOAD[2:8])


# ---------------------------------------------------------------------------
# WinFsp operations layer (pure method calls; no driver mount required)
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_WINFSPY, "winfspy not installed")
class TestWinfspOperations(unittest.TestCase):
    def setUp(self):
        self.payload = b"abcdef"
        self.remote = RangeServerFixture(self.payload)
        self.remote.__enter__()
        base = self.remote.url.split("/file.bin")[0]
        self.vfs, _session = make_streaming_vfs(base, len(self.payload))
        session = self.vfs._session

        def fixed_link(name, size, selector, file_id=None, torrent_id=None):
            return self.remote.url

        session.generate_download_link = fixed_link
        self.ops = TorboxFsOperations(self.vfs)

    def tearDown(self):
        self.remote.__exit__()

    def test_volume_info(self):
        info = self.ops.get_volume_info()
        self.assertEqual(info["volume_label"], "Torbox")
        self.assertEqual(info["total_size"], 2 * len(self.payload))
        self.assertEqual(info["free_size"], 0)

    def test_security_by_name_hit_and_miss(self):
        attrs, _handle, _size = self.ops.get_security_by_name("\\Torrent One")
        self.assertTrue(attrs & FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY)
        with self.assertRaises(NTStatusObjectNameNotFound):
            self.ops.get_security_by_name("\\Missing Torrent")

    def test_open_and_read_file(self):
        ctx = self.ops.open("\\Torrent One\\Sub Dir\\b.mkv", 0, 0)
        data = self.ops.read(ctx, 0, 4)
        self.assertEqual(data, self.payload[:4])
        self.ops.close(ctx)

    def test_read_past_eof_raises(self):
        ctx = self.ops.open("\\Torrent One\\Sub Dir\\b.mkv", 0, 0)
        with self.assertRaises(NTStatusEndOfFile):
            self.ops.read(ctx, len(self.payload), 4)
        self.ops.close(ctx)

    def test_read_directory_lists_children(self):
        ctx = self.ops.open("\\Torrent One", 0, 0)
        entries = self.ops.read_directory(ctx, None)
        names = [e["file_name"] for e in entries]
        self.assertIn("Sub Dir", names)
        self.assertIn("a.mkv", names)
        marker_entries = self.ops.read_directory(ctx, "a.mkv")
        marker_names = [e["file_name"] for e in marker_entries]
        self.assertNotIn("a.mkv", marker_names)
        self.assertIn("Sub Dir", marker_names)

    def test_writes_are_rejected(self):
        ctx = self.ops.open("\\Torrent One\\Sub Dir\\b.mkv", 0, 0)
        with self.assertRaises(NTStatusMediaWriteProtected):
            self.ops.write(ctx, b"", 0, False, False)
        self.ops.close(ctx)


if __name__ == "__main__":
    unittest.main()
