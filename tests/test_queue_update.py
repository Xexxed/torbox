"""Tests for the queue refresh that a rescan triggers.

A rescan replaces the queue wholesale, which was safe while the scan always
listed every torrent. Now that undownloadable torrents are filtered out, a
torrent whose TorBox cache expires mid-transfer would drop its row while its
download kept running -- orphaning the name->index map the progress updates go
through. These tests pin the carry-over behaviour that prevents that.
"""

import unittest

import gui_server
from gui_server import AppState


def torrent(name, **kw):
    entry = {"name": name, "size": "1.00 GB"}
    entry.update(kw)
    return entry


class QueueUpdateTestCase(unittest.TestCase):
    def setUp(self):
        # AppState.__init__ touches the real settings.json, the real profile
        # store and the drive-letter manager, so build the object without it.
        self.state = AppState.__new__(AppState)
        self.state.lock = __import__("threading").Lock()
        self.state.queue = []
        self.state._queue_index = {}
        self.state.active_downloads = {}


class TestRescanPreservesLiveDownloads(QueueUpdateTestCase):
    def test_a_running_download_survives_a_rescan(self):
        self.state.update_queue([torrent("A"), torrent("B")])
        self.state.update_file_progress("B", "downloading_file", 500, 1000, 2048)

        # B has dropped off the server: its cache expired mid-transfer.
        self.state.update_queue([torrent("A")])

        names = [e["name"] for e in self.state.queue]
        self.assertIn("B", names, "an in-flight torrent must not vanish from the queue")
        self.assertEqual(self.state._queue_index["B"], names.index("B"))

    def test_the_carried_row_keeps_its_live_progress(self):
        self.state.update_queue([torrent("A"), torrent("B")])
        self.state.update_file_progress("B", "downloading_file", 500, 1000, 2048)
        self.state.update_queue([torrent("A")])

        carried = next(e for e in self.state.queue if e["name"] == "B")
        self.assertEqual(carried["progress"], 50)
        self.assertEqual(carried["status"], "downloading_file")
        self.assertEqual(carried["speed"], "2.0 KB/s")

    def test_progress_updates_reach_the_carried_row(self):
        # The bug this guards: a stale index would write to the wrong row.
        self.state.update_queue([torrent("A"), torrent("B")])
        self.state.update_file_progress("B", "downloading_file", 100, 1000, 1024)
        self.state.update_queue([torrent("A")])
        self.state.update_file_progress("B", "downloading_file", 900, 1000, 1024)

        carried = next(e for e in self.state.queue if e["name"] == "B")
        self.assertEqual(carried["progress"], 90)

    def test_a_rescan_does_not_reset_a_download_still_listed(self):
        self.state.update_queue([torrent("A", progress=10)])
        self.state.update_file_progress("A", "downloading_file", 500, 1000, 2048)
        self.state.update_queue([torrent("A", progress=10)])

        row = next(e for e in self.state.queue if e["name"] == "A")
        self.assertEqual(row["progress"], 50, "a poll must not rewind an active download")

    def test_an_idle_row_still_takes_the_new_progress(self):
        self.state.update_queue([torrent("A", progress=10)])
        self.state.update_queue([torrent("A", progress=60)])
        row = next(e for e in self.state.queue if e["name"] == "A")
        self.assertEqual(row["progress"], 60)
        self.assertEqual(row["status"], "queued")

    def test_a_finished_download_is_dropped_when_it_disappears(self):
        self.state.update_queue([torrent("A"), torrent("B")])
        self.state.update_file_progress("B", "completed_file", 1000, 1000, 0)
        self.state.update_queue([torrent("A")])
        self.assertNotIn("B", [e["name"] for e in self.state.queue])

    def test_a_failed_download_is_dropped_when_it_disappears(self):
        self.state.update_queue([torrent("A"), torrent("B")])
        self.state.update_file_progress("B", "failed_file", 0, 1000, 0)
        self.state.update_queue([torrent("A")])
        self.assertNotIn("B", [e["name"] for e in self.state.queue])

    def test_an_empty_scan_still_clears_idle_rows(self):
        self.state.update_queue([torrent("A")])
        self.state.update_queue([])
        self.assertEqual(self.state.queue, [])
        self.assertEqual(self.state._queue_index, {})

    def test_an_empty_scan_keeps_a_running_download(self):
        self.state.update_queue([torrent("A")])
        self.state.update_file_progress("A", "downloading_file", 250, 1000, 1024)
        self.state.update_queue([])
        self.assertEqual([e["name"] for e in self.state.queue], ["A"])

    def test_the_index_stays_consistent_after_every_rescan(self):
        self.state.update_queue([torrent("A"), torrent("B"), torrent("C")])
        self.state.update_file_progress("B", "downloading_file", 100, 1000, 1024)
        self.state.update_queue([torrent("A"), torrent("C")])
        self.state.update_queue([torrent("C")])
        for idx, entry in enumerate(self.state.queue):
            self.assertEqual(self.state._queue_index[entry["name"]], idx)

    def test_server_progress_is_carried_through_to_the_row(self):
        self.state.update_queue([torrent("A", progress=42, eta=90,
                                         download_state="cached",
                                         download_speed=2 * 1024 * 1024)])
        row = self.state.queue[0]
        self.assertEqual(row["progress"], 42)
        self.assertEqual(row["eta"], 90)
        self.assertEqual(row["download_state"], "cached")
        self.assertEqual(row["speed"], "2.0 MB/s")

    def test_a_row_without_server_progress_keeps_the_old_defaults(self):
        # The browser backend and file dicts carry no progress at all.
        self.state.update_queue([{"name": "A", "size": "1GB"}])
        row = self.state.queue[0]
        self.assertEqual(row["progress"], 0)
        self.assertEqual(row["speed"], "0 KB/s")
        self.assertIsNone(row["eta"])


class TestInFlightStatusSet(unittest.TestCase):
    def test_only_running_statuses_are_protected(self):
        self.assertEqual(
            gui_server._IN_FLIGHT_STATUSES, {"downloading", "downloading_file"})

    def test_terminal_statuses_are_not_protected(self):
        for status in ("queued", "completed", "failed", "completed_file", "failed_file"):
            self.assertNotIn(status, gui_server._IN_FLIGHT_STATUSES)


class TestScanTriggerDoesNotDeadlock(unittest.TestCase):
    """`trigger_torrent_scan()` re-acquires `app_state.lock`.

    `app_state.lock` is a plain `threading.Lock`, so calling the trigger from
    inside a `with app_state.lock:` block -- as the settings handler once did --
    hangs the request forever. These tests pin the constraint.
    """

    def setUp(self):
        self.real_state = gui_server.app_state
        self.real_scan = gui_server.scan_torrents_background
        fake = AppState.__new__(AppState)
        fake.lock = __import__("threading").Lock()
        fake._scan_in_flight = False
        gui_server.app_state = fake
        # Hold the in-flight flag long enough for the dedup to be observable;
        # a no-op stub would clear it before the next thread looks.
        gui_server.scan_torrents_background = lambda: __import__("time").sleep(0.3)
        self.fake = fake

    def tearDown(self):
        gui_server.app_state = self.real_state
        gui_server.scan_torrents_background = self.real_scan

    def test_the_state_lock_is_not_reentrant(self):
        # The reason the ordering matters: this must stay a plain Lock.
        self.assertFalse(isinstance(self.fake.lock, type(__import__("threading").RLock())))

    def test_triggering_a_scan_completes_without_holding_the_lock(self):
        import threading
        done = []
        t = threading.Thread(target=lambda: (gui_server.trigger_torrent_scan(), done.append(1)))
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "trigger_torrent_scan() must not block")
        self.assertEqual(done, [1])

    def test_a_second_trigger_is_deduped_rather_than_queued(self):
        import threading
        results = []
        threads = [threading.Thread(target=lambda: results.append(gui_server.trigger_torrent_scan()))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(1 for r in results if r), 1,
                         "exactly one scan should be started")


if __name__ == "__main__":
    unittest.main()
