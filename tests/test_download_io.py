"""
End-to-end download tests against a real local HTTP server.

These verify that the downloader still produces byte-exact files (single
threaded, segmented, resumed) and that the write pattern reaching the disk is
actually coalesced rather than a stream of small scattered writes.
"""

import os
import sys
import shutil
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from torbox_downloader import downloader as dl

PAYLOAD_SIZE = 6 * 1024 * 1024
PAYLOAD = os.urandom(PAYLOAD_SIZE)


class RangeHandler(BaseHTTPRequestHandler):
    supports_ranges = True

    def log_message(self, *a):
        pass

    def _range(self):
        rng = self.headers.get("Range")
        if not rng or not self.supports_ranges:
            return None
        try:
            spec = rng.split("=", 1)[1]
            start_s, end_s = spec.split("-", 1)
            start = int(start_s)
            end = int(end_s) if end_s else PAYLOAD_SIZE - 1
            return start, min(end, PAYLOAD_SIZE - 1)
        except Exception:
            return None

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(PAYLOAD_SIZE))
        if self.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        rng = self._range()
        if rng:
            start, end = rng
            body = PAYLOAD[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{PAYLOAD_SIZE}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if self.supports_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class BaseServerTest(unittest.TestCase):
    supports_ranges = True

    @classmethod
    def setUpClass(cls):
        handler = type("H", (RangeHandler,), {"supports_ranges": cls.supports_ranges})
        cls.server = HTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.url = f"http://127.0.0.1:{self.port}/file.bin"
        self.out = os.path.join(self.dir, "file.bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def assertPayload(self, path):
        self.assertTrue(os.path.exists(path), "output file missing")
        with open(path, "rb") as f:
            got = f.read()
        self.assertEqual(len(got), PAYLOAD_SIZE)
        self.assertEqual(hashlib.sha256(got).hexdigest(),
                         hashlib.sha256(PAYLOAD).hexdigest())


class TestSingleThreaded(BaseServerTest):
    def test_download_is_byte_exact(self):
        ok = dl.download_file(self.url, self.out, parallel=False, disk_profile="ssd")
        self.assertTrue(ok)
        self.assertPayload(self.out)
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_hdd_profile_download_is_byte_exact(self):
        ok = dl.download_file(self.url, self.out, disk_profile="hdd")
        self.assertTrue(ok)
        self.assertPayload(self.out)

    def test_resume_from_partial_file(self):
        # Simulate an interrupted download: half the bytes already on disk.
        part = self.out + ".part"
        half = PAYLOAD_SIZE // 2
        with open(part, "wb") as f:
            f.write(PAYLOAD[:half])
        ok = dl.download_file(self.url, self.out, parallel=False, disk_profile="ssd")
        self.assertTrue(ok)
        self.assertPayload(self.out)

    def test_skips_already_complete_file(self):
        with open(self.out, "wb") as f:
            f.write(PAYLOAD)
        calls = []
        ok = dl.download_file(self.url, self.out, expected_size_str="6MB",
                              progress_callback=lambda *a: calls.append(a))
        self.assertTrue(ok)
        self.assertPayload(self.out)


class TestSegmented(BaseServerTest):
    def test_segmented_download_is_byte_exact(self):
        ok = self._segmented()
        self.assertTrue(ok)
        self.assertPayload(self.out)

    def _segmented(self, threads=4):
        return _run_segmented(self.url, self.out, threads)

    def test_segments_cover_whole_file(self):
        ok = self._segmented(threads=3)
        self.assertTrue(ok)
        self.assertPayload(self.out)

    def test_no_leftover_markers(self):
        self._segmented()
        leftovers = [f for f in os.listdir(self.dir) if ".part" in f]
        self.assertEqual(leftovers, [])


class TestNoRangeSupport(BaseServerTest):
    supports_ranges = False

    def test_falls_back_to_single_stream(self):
        ok = dl.download_file(self.url, self.out, disk_profile="ssd")
        self.assertTrue(ok)
        self.assertPayload(self.out)


class TestWritePattern(BaseServerTest):
    def test_write_sizes_are_large(self):
        """Regression guard: the old code wrote 512 KB at a time."""
        real_pwrite = getattr(os, "pwrite", None)
        sizes = []

        if real_pwrite is not None:
            def counting(fd, data, off):
                sizes.append(len(data))
                return real_pwrite(fd, data, off)
            os.pwrite = counting
        else:
            def counting(fd, data, off):
                sizes.append(len(data))
                os.lseek(fd, off, os.SEEK_SET)
                return os.write(fd, data)
            os.pwrite = counting

        try:
            ok = dl.download_file(self.url, self.out, disk_profile="hdd")
        finally:
            if real_pwrite is not None:
                os.pwrite = real_pwrite
            else:
                if hasattr(os, "pwrite"):
                    delattr(os, "pwrite")

        self.assertTrue(ok)
        self.assertPayload(self.out)
        # A 6 MB file under the 32 MB HDD buffer should land in very few writes.
        self.assertLessEqual(len(sizes), 4,
                             f"expected coalesced writes, got {len(sizes)}: {sizes}")


def _run_segmented(url, out, threads):
    """
    Drive the segmented multi-connection path.

    ``download_file`` only chooses that branch for files >= 1 GB, which is
    impractical to transfer in a unit test, so we rebind the size gate constant
    to 1 MB in a copy of the function.
    """
    import types
    import torbox_downloader.downloader as d

    original = d.download_file
    gate = 1024 * 1024 * 1024
    code = original.__code__
    new_consts = tuple((1024 * 1024) if c == gate else c for c in code.co_consts)
    patched = types.FunctionType(
        code.replace(co_consts=new_consts), original.__globals__,
        original.__name__, original.__defaults__, original.__closure__,
    )
    patched.__kwdefaults__ = original.__kwdefaults__
    return patched(url, out, parallel=True, num_threads=threads,
                   disk_profile="ssd")


if __name__ == "__main__":
    unittest.main()
