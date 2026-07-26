import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from torbox_downloader.diskio import (
    SegmentWriter,
    WriteThrottle,
    allocate_file,
    recommended_buffer_bytes,
    recommended_connections,
)


class TestAllocateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_allocates_exact_size(self):
        path = os.path.join(self.dir, "a.bin")
        allocate_file(path, 5 * 1024 * 1024)
        self.assertEqual(os.path.getsize(path), 5 * 1024 * 1024)

    def test_allocation_is_not_sparse(self):
        """The old seek+write trick produced a sparse file; this must not."""
        if not hasattr(os, "posix_fallocate"):
            self.skipTest("posix_fallocate unavailable")
        size = 8 * 1024 * 1024
        path = os.path.join(self.dir, "dense.bin")
        allocate_file(path, size)
        allocated = os.stat(path).st_blocks * 512
        # Allow slack for filesystems that round up, but it must be real space.
        self.assertGreaterEqual(allocated, size * 0.9)

    def test_zero_size(self):
        path = os.path.join(self.dir, "empty.bin")
        allocate_file(path, 0)
        self.assertEqual(os.path.getsize(path), 0)

    def test_creates_parent_dirs(self):
        path = os.path.join(self.dir, "x", "y", "z.bin")
        allocate_file(path, 1024)
        self.assertTrue(os.path.exists(path))


class TestSegmentWriter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "f.bin")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writes_at_offset(self):
        allocate_file(self.path, 1024)
        with SegmentWriter(self.path, 100, buffer_bytes=64 * 1024) as w:
            w.append(b"hello")
        with open(self.path, "rb") as f:
            f.seek(100)
            self.assertEqual(f.read(5), b"hello")

    def test_data_integrity_across_flushes(self):
        payload = os.urandom(3 * 1024 * 1024)
        allocate_file(self.path, len(payload))
        # Small buffer forces many flushes.
        with SegmentWriter(self.path, 0, buffer_bytes=64 * 1024) as w:
            for i in range(0, len(payload), 7919):  # awkward, unaligned chunks
                w.append(payload[i:i + 7919])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_parallel_segments_do_not_corrupt(self):
        import threading
        size = 4 * 1024 * 1024
        payload = os.urandom(size)
        allocate_file(self.path, size)
        nseg = 4
        seg = size // nseg

        def run(i):
            start = i * seg
            end = size if i == nseg - 1 else start + seg
            with SegmentWriter(self.path, start, buffer_bytes=128 * 1024) as w:
                data = payload[start:end]
                for j in range(0, len(data), 9973):
                    w.append(data[j:j + 9973])

        threads = [threading.Thread(target=run, args=(i,)) for i in range(nseg)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_bytes_written_counts_buffered_data(self):
        allocate_file(self.path, 1024)
        with SegmentWriter(self.path, 0, buffer_bytes=1024 * 1024) as w:
            w.append(b"x" * 500)
            self.assertEqual(w.bytes_written, 500)  # still buffered
            w.flush()
            self.assertEqual(w.bytes_written, 500)

    def test_write_syscalls_are_coalesced(self):
        """The whole point: many small appends must become few large writes."""
        size = 4 * 1024 * 1024
        allocate_file(self.path, size)
        real_pwrite = os.pwrite
        calls = []

        def counting_pwrite(fd, data, off):
            calls.append(len(data))
            return real_pwrite(fd, data, off)

        os.pwrite = counting_pwrite
        try:
            with SegmentWriter(self.path, 0, buffer_bytes=1024 * 1024) as w:
                for _ in range(size // (64 * 1024)):  # 64 appends of 64 KB
                    w.append(b"a" * (64 * 1024))
        finally:
            os.pwrite = real_pwrite

        # 64 appends coalesced into ~4 writes of 1 MB.
        self.assertLessEqual(len(calls), 6)
        self.assertGreaterEqual(max(calls), 1024 * 1024)


class TestProfilePolicy(unittest.TestCase):
    def test_hdd_forces_single_connection(self):
        self.assertEqual(recommended_connections("hdd", 5), 1)
        self.assertEqual(recommended_connections("hdd", 1), 1)

    def test_ssd_allows_parallel_but_caps(self):
        self.assertEqual(recommended_connections("ssd", 4), 4)
        self.assertEqual(recommended_connections("ssd", 99), 4)

    def test_hdd_uses_bigger_buffer(self):
        self.assertGreater(recommended_buffer_bytes("hdd"),
                           recommended_buffer_bytes("ssd"))

    def test_buffer_override(self):
        self.assertEqual(recommended_buffer_bytes("ssd", 16), 16 * 1024 * 1024)
        # Invalid overrides fall back to the profile default.
        self.assertEqual(recommended_buffer_bytes("ssd", "nope"),
                         recommended_buffer_bytes("ssd"))


class TestWindowsPathHandling(unittest.TestCase):
    """The GUI accepts paths like 'f:/torbox'; drive parsing must cope."""

    def test_forward_slash_drive_path_yields_letter(self):
        import ntpath
        for p in ("f:/torbox", "F:\\torbox", "f:/torbox/movies"):
            drive = ntpath.splitdrive(p)[0].rstrip(":")
            self.assertEqual(drive.lower(), "f", f"failed for {p}")

    def test_unc_path_has_no_drive_letter(self):
        import ntpath
        drive = ntpath.splitdrive(r"\\server\share\torbox")[0].rstrip(":")
        # Must not be mistaken for a one-letter local drive.
        self.assertNotEqual(len(drive), 1)

    def test_network_profile_uses_large_buffer(self):
        self.assertEqual(recommended_buffer_bytes("network"),
                         recommended_buffer_bytes("hdd"))

    def test_network_profile_keeps_parallelism(self):
        # No physical seek penalty on a share, so don't collapse to 1.
        self.assertGreater(recommended_connections("network", 4), 1)


class TestWriteThrottle(unittest.TestCase):
    def test_limits_concurrency(self):
        import threading
        import time

        throttle = WriteThrottle(2)
        active = []
        peak = [0]
        lock = threading.Lock()

        def worker():
            with throttle:
                with lock:
                    active.append(1)
                    peak[0] = max(peak[0], len(active))
                time.sleep(0.05)
                with lock:
                    active.pop()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(peak[0], 2)

    def test_reconfigure(self):
        throttle = WriteThrottle(2)
        throttle.configure(5)
        self.assertEqual(throttle.limit, 5)


if __name__ == "__main__":
    unittest.main()
