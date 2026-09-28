"""Tests for the download pipeline's link resolution.

Focuses on the two backend behaviours that changed: a torrent's links are
resolved in one parallel batch on the HTTP backend, and the exact
(torrent_id, file_id) pair is always what reaches the session. No network and
no real downloads: the session and the file writer are both faked.
"""

import os
import shutil
import tempfile
import unittest

from main import _is_http_session, download_files_pipeline


class RecordingSession:
    """Stands in for TorboxApiSession."""

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.batched = []
        self.singles = []
        self.prefetch_calls = 0
        self.lock_held_during = []

    def prefetch_links(self, refs, cancel_event=None, workers=None):
        self.prefetch_calls += 1
        out = {}
        for torrent_id, file_id, key in refs:
            self.batched.append((torrent_id, file_id))
            if file_id not in self.fail_ids:
                out[key] = f"https://cdn.test/{file_id}"
        return out

    def generate_download_link(self, name, size=None, selector=None,
                               file_id=None, torrent_id=None):
        self.singles.append((name, size, selector, file_id, torrent_id))
        return f"https://cdn.test/{file_id}"


class BrowserStyleSession(RecordingSession):
    """Stands in for TorboxBrowserSession: no batch API, positional only."""

    prefetch_links = None

    def __init__(self):
        super().__init__()
        del self.batched


def make_files(root, count=4, with_ids=True):
    files = []
    for i in range(count):
        entry = {
            "name": f"file{i}.mkv",
            "path": ["T", "Sub"],
            "size": "1.00 MB",
            "size_bytes": 1048576,
            "selector": f"sel{i}",
            "local_path": os.path.join(root, "T", "Sub", f"file{i}.mkv"),
        }
        if with_ids:
            entry["torrent_id"] = 7
            entry["file_id"] = i
        files.append(entry)
    return files


def run(session, files, tmp, **kwargs):
    """Drive the pipeline with the network-facing download neutralised.

    Files start absent so link resolution is genuinely exercised; the stub
    writes each one on "download" so the consumer path still completes.
    """
    import torbox_downloader.downloader as dl

    # download_files_pipeline imports download_file inside the function body,
    # so the patch has to land on the defining module.
    original = dl.download_file
    downloaded = []

    def fake_download(url, local_path, expected_size_str=None, **kw):
        downloaded.append(url)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as fh:
            fh.write(b"\0" * 1048576)
        return True

    dl.download_file = fake_download
    try:
        result = download_files_pipeline(
            session=session,
            dest_dir=tmp,
            files_with_local_paths=files,
            max_parallel_files=2,
            browser_lock=kwargs.get("browser_lock"),
            cancel_event=kwargs.get("cancel_event"),
            disk_profile="ssd",
        )
    finally:
        dl.download_file = original
    return result, downloaded


class TestBackendDetection(unittest.TestCase):
    def test_http_session_is_detected_by_its_batch_api(self):
        self.assertTrue(_is_http_session(RecordingSession()))

    def test_browser_session_is_not(self):
        self.assertFalse(_is_http_session(BrowserStyleSession()))


class TestLinkPrefetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="torbox-pipeline-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_http_backend_batches_every_link(self):
        session = RecordingSession()
        run(session, make_files(self.tmp), self.tmp)
        self.assertEqual(session.prefetch_calls, 1, "one batch, not one call per file")
        self.assertEqual(sorted(f for _, f in session.batched), [0, 1, 2, 3])
        self.assertEqual(session.singles, [], "nothing should need a per-file call")

    def test_the_batch_carries_exact_torrent_and_file_ids(self):
        session = RecordingSession()
        run(session, make_files(self.tmp), self.tmp)
        self.assertTrue(all(tid == 7 for tid, _ in session.batched))
        self.assertNotIn(None, [fid for _, fid in session.batched])

    def test_a_failed_file_is_retried_individually(self):
        session = RecordingSession(fail_ids={2})
        run(session, make_files(self.tmp), self.tmp)
        # 2 was dropped by the batch, then retried on its own.
        self.assertEqual([s[3] for s in session.singles], [2])
        self.assertEqual(session.singles[0][4], 7, "the retry must still carry torrent_id")

    def test_browser_backend_uses_one_call_per_file(self):
        session = BrowserStyleSession()
        run(session, make_files(self.tmp, with_ids=False), self.tmp)
        self.assertEqual(len(session.singles), 4)
        for name, size, selector, file_id, torrent_id in session.singles:
            self.assertIsNone(file_id)
            self.assertIsNone(torrent_id)
            self.assertTrue(selector.startswith("sel"))

    def test_cancellation_stops_before_any_link_is_requested(self):
        class Cancelled:
            def is_set(self):
                return True

        session = RecordingSession()
        run(session, make_files(self.tmp), self.tmp, cancel_event=Cancelled())
        self.assertEqual(session.batched, [])
        self.assertEqual(session.singles, [])

    def test_already_complete_files_are_skipped_without_a_link(self):
        session = RecordingSession()
        files = make_files(self.tmp)
        for f in files:
            os.makedirs(os.path.dirname(f["local_path"]), exist_ok=True)
            with open(f["local_path"], "wb") as fh:
                fh.write(b"\0" * 1048576)
        run(session, files, self.tmp)
        self.assertEqual(session.batched, [], "a finished file needs no CDN link")
        self.assertEqual(session.prefetch_calls, 0)

if __name__ == "__main__":
    unittest.main()
