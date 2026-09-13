"""API tests for the Stream resolve endpoint and content proxy (Stage 6).

The content proxy is exercised against a real local range server standing in
for the CDN, so range parsing, 206/416 responses, HEAD probing and the
"HEAD never resolves a link" guarantee are all covered end to end.
"""

import unittest

import requests

from torbox_downloader.mount.vfs import TorboxVfs
from tests.test_mount import FakeSession, RangeServerFixture, make_streaming_vfs, make_vfs
from tests.test_stream_api import _FakeState, _ServerFixture

PAYLOAD = b"0123456789abcdef"      # 16 bytes


def _vfs_with(names, cdn_base="http://cdn.test"):
    files = [{"name": n, "path": ["T"], "size": "1GB", "selector": "s"} for n in names]
    session = FakeSession(torrents=[{"name": "T", "size": "1GB"}], structures={"T": files},
                          cdn_base=cdn_base)
    vfs = TorboxVfs(session)
    vfs.scan()
    return vfs, session


class TestResolveEndpoint(unittest.TestCase):
    def test_resolve_returns_url_and_metadata(self):
        vfs, session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/resolve",
                              json={"path": "/Torrent One/Sub Dir/b.mkv"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["url"].startswith("http"))
        self.assertEqual(body["kind"], "video")
        self.assertFalse(body["playable"])          # mkv is not browser-playable
        self.assertIn("expires_at", body)
        self.assertEqual(len(session.generated), 1)

    def test_resolve_flags_mp4_as_playable(self):
        vfs, _session = _vfs_with(["movie.mp4"])
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/resolve",
                              json={"path": "/T/movie.mp4"},
                              headers=srv.headers(), timeout=10)
        body = r.json()
        self.assertTrue(body["playable"])
        self.assertEqual(body["kind"], "video")

    def test_resolve_rejects_traversal(self):
        vfs, _session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/resolve",
                              json={"path": "/Torrent One/../secret"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 400)

    def test_resolve_directory_returns_error_body(self):
        vfs, _session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.post(srv.base + "/api/stream/resolve",
                              json={"path": "/Torrent One"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["success"])


class TestContentProxy(unittest.TestCase):
    def _serve(self, payload):
        return RangeServerFixture(payload)

    def test_full_body(self):
        with self._serve(PAYLOAD) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(PAYLOAD))
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/Torrent One/Sub Dir/b.mkv"},
                                 headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PAYLOAD)
        self.assertEqual(r.headers.get("Accept-Ranges"), "bytes")

    def test_range_request_returns_206(self):
        with self._serve(PAYLOAD) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(PAYLOAD))
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/Torrent One/Sub Dir/b.mkv"},
                                 headers=dict(srv.headers(), Range="bytes=0-3"),
                                 timeout=10)
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, PAYLOAD[:4])
        self.assertEqual(r.headers.get("Content-Range"), f"bytes 0-3/{len(PAYLOAD)}")

    def test_unsatisfiable_range_returns_416(self):
        with self._serve(PAYLOAD) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(PAYLOAD))
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/Torrent One/Sub Dir/b.mkv"},
                                 headers=dict(srv.headers(), Range="bytes=999-1000"),
                                 timeout=10)
        self.assertEqual(r.status_code, 416)
        self.assertEqual(r.headers.get("Content-Range"), f"bytes */{len(PAYLOAD)}")

    def test_head_reports_length_without_resolving(self):
        vfs, session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.head(srv.base + "/api/stream/content",
                              params={"path": "/Torrent One/Sub Dir/b.mkv"},
                              headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Content-Length"), str(2 * 1024**3))
        self.assertEqual(r.content, b"")
        # Probing metadata must not generate a CDN link.
        self.assertEqual(session.generated, [])

    def test_download_sets_content_disposition(self):
        with self._serve(PAYLOAD) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(PAYLOAD))
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/Torrent One/Sub Dir/b.mkv", "download": "1"},
                                 headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        self.assertIn("b.mkv", r.headers.get("Content-Disposition", ""))

    def test_directory_returns_400(self):
        vfs, _session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.get(srv.base + "/api/stream/content",
                             params={"path": "/Torrent One"},
                             headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 400)

    def test_missing_file_returns_404(self):
        vfs, _session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.get(srv.base + "/api/stream/content",
                             params={"path": "/Torrent One/nope.mkv"},
                             headers=srv.headers(), timeout=10)
        self.assertEqual(r.status_code, 404)

    def test_content_requires_api_key(self):
        vfs, _session = make_vfs()
        with _ServerFixture(_FakeState(vfs)) as srv:
            r = requests.get(srv.base + "/api/stream/content",
                             params={"path": "/Torrent One/Sub Dir/b.mkv"}, timeout=10)
        self.assertEqual(r.status_code, 401)

    def test_content_accepts_api_key_in_query(self):
        # A <video> element cannot set headers, so the key may come via query.
        with self._serve(PAYLOAD) as remote:
            base = remote.url.split("/file.bin")[0]
            vfs, _session = make_streaming_vfs(base, len(PAYLOAD))
            with _ServerFixture(_FakeState(vfs)) as srv:
                r = requests.get(srv.base + "/api/stream/content",
                                 params={"path": "/Torrent One/Sub Dir/b.mkv",
                                         "api_key": "test-key"},
                                 timeout=10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PAYLOAD)


if __name__ == "__main__":
    unittest.main()
