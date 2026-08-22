import unittest
from torbox_downloader.browser_session import (
    TorboxBrowserSession,
    SCAN_MAX_ROUNDS,
    SCAN_STALE_ROUNDS,
)


def _card_html(index):
    """A single torrent card exactly like the ones on the Torbox dashboard."""
    return f"""
    <div class="flex items-center justify-between p-4 border rounded-lg bg-gray-800">
        <div class="flex-1 min-w-0">
            <p class="font-semibold text-gray-200 truncate">Torrent Card {index}</p>
            <div class="flex gap-4 mt-1 text-xs text-gray-400">
                <span>Total Size: <span class="font-semibold">{index}0GB</span></span>
            </div>
        </div>
        <div class="flex items-center gap-2">
            <button title="Download files" class="p-2"><svg class="w-5 h-5"></svg></button>
        </div>
    </div>
    """


class _FakeMouse:
    def __init__(self, page):
        self._page = page

    def move(self, x, y):
        pass

    def wheel(self, dx, dy):
        self._page.on_scroll()


class _FakeLazyDashboardPage:
    """
    Simulates the Torbox dashboard's infinite scroll: right after load only
    ``initial_cards`` torrent cards exist in the DOM. Every scroll event
    appends another ``batch`` cards until ``total_cards`` are rendered.
    """

    def __init__(self, total_cards, batch=5, initial_cards=5):
        self.url = "https://torbox.app/download"
        self.mouse = _FakeMouse(self)
        self.total = total_cards
        self.batch = batch
        self.visible = min(initial_cards, total_cards)
        self.scroll_calls = 0

    def on_scroll(self):
        self.scroll_calls += 1
        self.visible = min(self.total, self.visible + self.batch)

    def content(self):
        cards = "".join(_card_html(i) for i in range(self.visible))
        return f"<html><body><div class='dashboard'>{cards}</div></body></html>"

    def evaluate(self, script):
        return None

    def wait_for_timeout(self, ms):
        return None


class _FakeVirtualizedPage:
    """
    Simulates a virtualised (windowed) list: the DOM only ever holds
    ``window_size`` cards; scrolling slides the window forward and removes
    off-screen cards from the DOM.
    """

    def __init__(self, total_cards, window_size=6, batch=3):
        self.url = "https://torbox.app/download"
        self.mouse = _FakeMouse(self)
        self.total = total_cards
        self.window_size = window_size
        self.batch = batch
        self.offset = 0
        self.scroll_calls = 0

    def on_scroll(self):
        self.scroll_calls += 1
        max_offset = max(0, self.total - self.window_size)
        self.offset = min(max_offset, self.offset + self.batch)

    def content(self):
        end = min(self.total, self.offset + self.window_size)
        cards = "".join(_card_html(i) for i in range(self.offset, end))
        return f"<html><body><div class='dashboard'>{cards}</div></body></html>"

    def evaluate(self, script):
        return None

    def wait_for_timeout(self, ms):
        return None


def _session_with_page(page):
    session = TorboxBrowserSession("fake-token")
    session.page = page
    return session


class TestCollectDashboardTorrents(unittest.TestCase):
    def _card_names(self, torrents):
        return [t["name"] for t in torrents]

    def test_collects_all_lazy_loaded_torrents(self):
        # Only the first 5 cards exist in the DOM at load time; the rest are
        # fetched as the page scrolls. This reproduces the "only 5 torrents"
        # bug: the old code took a single DOM snapshot without scrolling.
        page = _FakeLazyDashboardPage(total_cards=23, batch=5, initial_cards=5)
        session = _session_with_page(page)

        torrents = session._collect_dashboard_torrents()

        self.assertEqual(len(torrents), 23)
        names = self._card_names(torrents)
        self.assertEqual(names[0], "Torrent Card 0")
        self.assertIn("Torrent Card 22", names)
        # Each torrent keeps its parsed size
        sizes = {t["name"]: t["size"] for t in torrents}
        self.assertEqual(sizes["Torrent Card 22"], "220GB")

    def test_single_snapshot_returns_only_initial_cards(self):
        page = _FakeLazyDashboardPage(total_cards=23, batch=5, initial_cards=5)
        session = _session_with_page(page)

        from torbox_downloader.detector import TorboxDetector
        single_snapshot = TorboxDetector(page.content()).parse_torrents()

        self.assertEqual(len(single_snapshot), 5)

    def test_stops_scrolling_when_no_new_cards_appear(self):
        page = _FakeLazyDashboardPage(total_cards=5, batch=5, initial_cards=5)
        session = _session_with_page(page)

        torrents = session._collect_dashboard_torrents()

        self.assertEqual(len(torrents), 5)
        # Must stop after SCAN_STALE_ROUNDS idle rounds instead of exhausting
        # the hard cap of SCAN_MAX_ROUNDS.
        self.assertLessEqual(page.scroll_calls, SCAN_MAX_ROUNDS - SCAN_STALE_ROUNDS)

    def test_handles_virtualized_windowed_list(self):
        # A virtualised list removes off-screen cards, so each snapshot only
        # contains a sliding window of torrents. Merging by name must still
        # recover every unique torrent.
        page = _FakeVirtualizedPage(total_cards=20, window_size=6, batch=3)
        session = _session_with_page(page)

        torrents = session._collect_dashboard_torrents()

        names = set(self._card_names(torrents))
        expected = {f"Torrent Card {i}" for i in range(20)}
        self.assertEqual(names, expected)

    def test_no_duplicate_torrents_in_result(self):
        page = _FakeLazyDashboardPage(total_cards=12, batch=5, initial_cards=5)
        session = _session_with_page(page)

        torrents = session._collect_dashboard_torrents()
        names = self._card_names(torrents)

        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
