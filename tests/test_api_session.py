"""Tests for the browser-free TorBox HTTP backend.

Everything here runs against an injected fake transport and a temporary
directory. No network, no browser, and no access to the real ``profiles/``
directory or ``settings.json``.
"""

import base64
import json
import shutil
import tempfile
import threading
import time
import unittest

from torbox_downloader import api_session
from torbox_downloader.api_session import (
    ANON_KEY_SEED,
    RateLimitedError,
    SessionExpiredError,
    TorboxApiSession,
    TokenManager,
    derive_anon_key,
    format_size,
    jwt_expiry,
)
from torbox_downloader.profiles import ProfileStore

FAKE_ANON = "fake-anon-key"


def make_jwt(exp, ref="testref"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'iss': 'supabase', 'ref': ref, 'exp': exp})}.sig"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeTransport:
    """Records every call and answers from a path -> handler table."""

    def __init__(self):
        self.headers = {}
        self.calls = []
        self.handlers = {}

    def route(self, needle, handler):
        self.handlers[needle] = handler

    def _record(self, method, url, params, headers):
        self.calls.append({"method": method, "url": url,
                           "params": params or {}, "headers": headers or {}})

    def request(self, method, url, params=None, headers=None, timeout=None):
        self._record(method, url, params, headers)
        for needle, handler in self.handlers.items():
            if needle in url:
                return handler(url, params, headers, method)
        return FakeResponse(404, {"success": False, "detail": f"no route for {url}"})

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self._record("POST", url, params, headers)
        for needle, handler in self.handlers.items():
            if needle in url:
                return handler(url, json, headers, "POST")
        return FakeResponse(404, {"message": "no route"})

    def close(self):
        pass

    def calls_to(self, needle):
        return [c for c in self.calls if needle in c["url"]]


TORRENT = {
    "id": 42,
    "name": "Example Pack",
    "size": 3221225472,
    "progress": 0.5,
    "eta": 120,
    "download_state": "cached",
    "download_finished": False,
    "download_speed": 1048576,
    "files": [
        {"id": 1, "name": "readme.txt", "short_name": "readme.txt", "size": 100},
        {"id": 2, "name": "Extras/clip.mkv", "short_name": "clip.mkv", "size": 2048},
        {"id": 3, "name": "Extras/Sub/deep.srt", "short_name": "deep.srt", "size": 4096},
        {"id": 4, "name": "\u00e9\u00e0 \u30d5\u30a1\u30a4\u30eb.bin", "size": 8192},
    ],
}

# download_state -> (files, can requestdl resolve it), measured on the live API.
STATE_MATRIX = {
    "cached": 218,
    "completed": 83,
    "expired": 1,        # requestdl answers 500 DATABASE_ERROR
    "incomplete": 22,    # requestdl answers 500 DATABASE_ERROR
    "stalled (no seeds)": 0,
}


def ok(data):
    return FakeResponse(200, {"success": True, "error": None, "detail": "ok", "data": data})


def live_session(expires_in=3600.0):
    return {
        "access_token": make_jwt(time.time() + expires_in),
        "refresh_token": "refresh-abc",
        "expires_at": time.time() + expires_in,
        "token_type": "bearer",
    }


def raw(payload, status=200):
    """GoTrue answers bare JSON, not the API's success/data envelope."""
    return FakeResponse(status, payload)


class ApiSessionTestCase(unittest.TestCase):
    """Shared fixture: a fake transport wired to mylist and requestdl."""

    def setUp(self):
        self.http = FakeTransport()
        self._orig_anon = api_session.resolve_anon_key
        self._orig_derive = api_session.derive_anon_key
        api_session.resolve_anon_key = lambda: FAKE_ANON
        api_session.derive_anon_key = lambda: None
        self.http.route("/auth/v1/token", lambda *a: raw(
            {"access_token": make_jwt(time.time() + 3600),
             "refresh_token": "rotated", "expires_in": 3600}))

    def tearDown(self):
        api_session.resolve_anon_key = self._orig_anon
        api_session.derive_anon_key = self._orig_derive

    def make_session(self, **kwargs):
        params = {"limit": 1000}

        def mylist(url, q, headers, method):
            if q and q.get("id"):
                return ok(dict(TORRENT))
            return ok([dict(TORRENT)])

        self.http.route("/torrents/mylist", mylist)
        self.http.route("/torrents/requestdl",
                        lambda url, q, h, m: ok("https://cdn.example/dld/abc"))
        return TorboxApiSession(token=live_session(), http=self.http, **kwargs)

    def serve_states(self):
        """Answer mylist with one torrent per state in STATE_MATRIX."""
        rows = [dict(TORRENT, id=i, name=f"T-{state}", download_state=state,
                     files=[{"id": i * 10 + 1, "name": "a.mkv", "short_name": "a.mkv",
                             "size": 10}])
                for i, state in enumerate(STATE_MATRIX)]

        def mylist(url, q, headers, method):
            if q and q.get("id"):
                return ok(dict(TORRENT))
            return ok(rows)

        self.http.route("/torrents/mylist", mylist)


class TestHelpers(unittest.TestCase):
    def test_format_size_matches_downloader_units(self):
        # downloader.parse_size_to_bytes only understands GB/MB/KB/B.
        self.assertEqual(format_size(3221225472), "3.00 GB")
        self.assertEqual(format_size(1048576), "1.00 MB")
        self.assertEqual(format_size(2048), "2.00 KB")
        self.assertEqual(format_size(900), "900 B")
        self.assertEqual(format_size(0), "0 B")
        self.assertTrue(format_size(2 * 1024 ** 4).endswith("GB"))

    def test_format_size_round_trips_through_parse_size_to_bytes(self):
        # Two decimals of GB is ~10 MB of granularity, so the round trip is
        # close rather than exact. Precision-critical callers use size_bytes.
        from torbox_downloader.downloader import parse_size_to_bytes
        for n in (999, 4096, 1048576, 3221225472, 153737711355):
            parsed = parse_size_to_bytes(format_size(n))
            self.assertIsNotNone(parsed, f"{n} formatted to an unparseable string")
            self.assertLess(abs(parsed - n) / n, 0.001,
                            f"{n} -> {format_size(n)} -> {parsed} lost too much")

    def test_jwt_expiry_reads_the_exp_claim(self):
        self.assertEqual(jwt_expiry(make_jwt(1234.0)), 1234.0)
        self.assertIsNone(jwt_expiry("not-a-jwt"))
        self.assertIsNone(jwt_expiry(None))


class TestScanAndStructure(ApiSessionTestCase):
    def test_scan_returns_progress_fields_and_sorts_by_id_desc(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: ok([
            dict(TORRENT, id=7, name="B"),
            dict(TORRENT, id=99, name="A"),
        ]))
        torrents = session.scan_torrents()
        self.assertEqual([t["name"] for t in torrents], ["A", "B"])
        first = torrents[0]
        self.assertEqual(first["progress"], 50)
        self.assertEqual(first["eta"], 120)
        self.assertEqual(first["download_state"], "cached")
        self.assertEqual(first["download_speed"], 1048576)
        self.assertEqual(first["size"], "3.00 GB")
        self.assertEqual(first["size_bytes"], 3221225472)
        self.assertEqual(first["file_count"], 4)

    def test_scan_clamps_progress_into_0_100(self):
        session = self.make_session()
        self.http.route("/torrents/mylist",
                        lambda *a: ok([dict(TORRENT, progress=1.4)]))
        self.assertEqual(session.scan_torrents()[0]["progress"], 100)

    def test_structure_paths_are_rooted_at_the_torrent_name(self):
        session = self.make_session()
        session.scan_torrents()
        files = session.fetch_structure("Example Pack")
        by_id = {f["file_id"]: f for f in files}

        self.assertEqual(by_id[1]["path"], ["Example Pack"])
        self.assertEqual(by_id[2]["path"], ["Example Pack", "Extras"])
        self.assertEqual(by_id[3]["path"], ["Example Pack", "Extras", "Sub"])
        for f in files:
            self.assertEqual(f["path"][0], "Example Pack", "path[0] must be the torrent name")

    def test_structure_entries_carry_exact_identifiers(self):
        session = self.make_session()
        session.scan_torrents()
        entry = session.fetch_structure("Example Pack")[1]
        self.assertEqual(entry["torrent_id"], 42)
        self.assertEqual(entry["file_id"], 2)
        self.assertIsNone(entry["selector"])
        self.assertEqual(entry["name"], "clip.mkv")
        self.assertEqual(entry["size"], "2.00 KB")
        self.assertEqual(entry["size_bytes"], 2048)

    def test_structure_falls_back_to_the_last_segment_when_short_name_is_absent(self):
        payload = dict(TORRENT, files=[{"id": 9, "name": "a/b/only.mkv", "size": 10}])
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda url, q, h, m: (
            ok(payload) if (q and q.get("id")) else ok([payload])))
        session.scan_torrents()
        entry = session.fetch_structure("Example Pack")[0]
        self.assertEqual(entry["name"], "only.mkv")
        self.assertEqual(entry["path"], ["Example Pack", "a", "b"])

    def test_structure_is_cached_per_torrent(self):
        session = self.make_session()
        session.scan_torrents()
        session.fetch_structure("Example Pack")
        before = len(self.http.calls_to("mylist"))
        session.fetch_structure("Example Pack")
        self.assertEqual(len(self.http.calls_to("mylist")), before)

    def test_structure_keeps_a_file_whose_id_is_zero(self):
        # TorBox really does ship entries with id 0. Testing that id for
        # truthiness silently dropped a 1.79 GB file from the tree.
        payload = dict(TORRENT, files=[
            {"id": 0, "name": "zero.mkv", "short_name": "zero.mkv", "size": 1797840157},
            {"id": 5, "name": "other.mkv", "short_name": "other.mkv", "size": 10},
        ])
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda url, q, h, m: (
            ok(payload) if (q and q.get("id")) else ok([payload])))
        session.scan_torrents()
        files = session.fetch_structure("Example Pack")
        self.assertEqual(len(files), 2, "a file with id 0 must not be dropped")
        self.assertEqual(files[0]["file_id"], 0)
        # And it must resolve to a link rather than a whole-torrent request.
        session.generate_download_link("zero.mkv", "1.67 GB")
        self.assertEqual(self.http.calls_to("requestdl")[0]["params"]["file_id"], 0)

    def test_structure_handles_a_list_response_for_a_single_id(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda url, q, h, m: (
            ok([dict(TORRENT)]) if (q and q.get("id")) else ok([dict(TORRENT)])))
        session.scan_torrents()
        self.assertEqual(len(session.fetch_structure("Example Pack")), 4)

    def test_unknown_torrent_raises(self):
        session = self.make_session()
        session.scan_torrents()
        with self.assertRaises(api_session.ApiError):
            session.fetch_structure("nope")

    def test_get_page_content_refuses_rather_than_lying(self):
        session = self.make_session()
        with self.assertRaises(api_session.ApiError):
            session.get_page_content()

    def test_viewing_torrent_name_tracks_the_expanded_torrent(self):
        session = self.make_session()
        session.scan_torrents()
        self.assertIsNone(session.viewing_torrent_name()["torrent"])
        session.fetch_structure("Example Pack")
        view = session.viewing_torrent_name()
        self.assertEqual(view["torrent"], "Example Pack")
        # No page exists here, so the cached structure is always live.
        self.assertTrue(view["on_download_view"])


class TestDownloadableFilter(ApiSessionTestCase):
    """Only torrents whose files can actually be fetched are listed.

    Measured on the live API: requestdl returns HTTP 500 DATABASE_ERROR for
    expired and incomplete torrents, and a stalled torrent has no files at all.
    """

    def test_only_cached_and_completed_are_listed(self):
        session = self.make_session()
        self.serve_states()
        names = {t["name"] for t in session.scan_torrents()}
        self.assertEqual(names, {"T-cached", "T-completed"})

    def test_hidden_states_are_reported_with_counts(self):
        session = self.make_session()
        self.serve_states()
        session.scan_torrents()
        total, states = session.hidden_summary()
        self.assertEqual(total, 3)
        self.assertEqual(states["expired"], 1)
        self.assertEqual(states["incomplete"], 1)
        self.assertEqual(states["stalled (no seeds)"], 1)

    def test_show_uncached_lists_everything(self):
        session = self.make_session(show_uncached=True)
        self.serve_states()
        names = {t["name"] for t in session.scan_torrents()}
        self.assertEqual(len(names), len(STATE_MATRIX))
        self.assertEqual(session.hidden_summary()[0], 0)

    def test_the_flag_can_be_toggled_on_a_live_session(self):
        session = self.make_session()
        self.serve_states()
        self.assertEqual(len(session.scan_torrents()), 2)
        session.show_uncached = True
        self.assertEqual(len(session.scan_torrents()), len(STATE_MATRIX))

    def test_a_missing_state_is_hidden_and_counted(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: ok([
            dict(TORRENT, name="NoState", download_state=None)]))
        self.assertEqual(session.scan_torrents(), [])
        total, states = session.hidden_summary()
        self.assertEqual(total, 1)
        self.assertEqual(states, {"unknown": 1})

    def test_an_unfamiliar_state_is_logged_loudly(self):
        # A rename must not silently empty the library with no explanation.
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: ok([
            dict(TORRENT, name="Weird", download_state="some_new_state")]))
        with self.assertLogs("torbox_downloader.api_session", level="WARNING") as logs:
            session.scan_torrents()
        self.assertTrue(any("some_new_state" in line for line in logs.output))

    def test_hiding_the_whole_library_is_logged_as_an_error(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: ok([
            dict(TORRENT, name="A", download_state="expired"),
            dict(TORRENT, name="B", download_state="incomplete")]))
        with self.assertLogs("torbox_downloader.api_session", level="ERROR") as logs:
            self.assertEqual(session.scan_torrents(), [])
        self.assertTrue(any("renamed download_state" in line for line in logs.output))

    def test_a_known_unavailable_state_is_not_treated_as_unknown(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: ok([
            dict(TORRENT, name="A", download_state="expired")]))
        session.scan_torrents()
        # No warning about an unrecognised state, and no "library emptied" error.
        self.assertEqual(session.hidden_summary(), (1, {"expired": 1}))

    def test_a_hidden_torrent_cannot_be_expanded(self):
        session = self.make_session()
        self.serve_states()
        session.scan_torrents()
        with self.assertRaises(api_session.ApiError):
            session.fetch_structure("T-expired")

    def test_a_visible_torrent_still_expands(self):
        session = self.make_session()
        self.serve_states()
        session.scan_torrents()
        self.assertEqual(len(session.fetch_structure("T-cached")), 1)


class TestLinkGeneration(ApiSessionTestCase):
    def test_requestdl_receives_torrent_and_file_ids(self):
        session = self.make_session()
        url = session.generate_download_link(None, file_id=2, torrent_id=42)
        self.assertEqual(url, "https://cdn.example/dld/abc")
        call = self.http.calls_to("requestdl")[0]
        self.assertEqual(call["params"]["torrent_id"], 42)
        self.assertEqual(call["params"]["file_id"], 2)
        self.assertIn("token", call["params"])

    def test_compat_lookup_resolves_by_name_and_size(self):
        session = self.make_session()
        session.scan_torrents()
        session.fetch_structure("Example Pack")
        session.generate_download_link("clip.mkv", "2.00 KB")
        self.assertEqual(self.http.calls_to("requestdl")[0]["params"]["file_id"], 2)

    def test_compat_lookup_rejects_an_unknown_file(self):
        session = self.make_session()
        session.scan_torrents()
        session.fetch_structure("Example Pack")
        with self.assertRaises(api_session.ApiError):
            session.generate_download_link("nope.mkv", "1.00 KB")

    def test_prefetch_resolves_many_files_concurrently(self):
        session = self.make_session()
        refs = [(42, i, i) for i in (1, 2, 3, 4)]
        result = session.prefetch_links(refs)
        self.assertEqual(sorted(result), [1, 2, 3, 4])
        self.assertEqual(len(self.http.calls_to("requestdl")), 4)

    def test_prefetch_stops_when_cancelled(self):
        session = self.make_session()
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(session.prefetch_links([(42, 1, 1)], cancel_event=cancelled), {})
        self.assertEqual(len(self.http.calls_to("requestdl")), 0)

    def test_prefetch_keeps_the_batch_when_one_file_fails(self):
        session = self.make_session()

        def requestdl(url, q, h, m):
            if q.get("file_id") == 3:
                return FakeResponse(200, {"success": False, "detail": "gone"})
            return ok("https://cdn.example/dld/abc")

        self.http.route("/torrents/requestdl", requestdl)
        result = session.prefetch_links([(42, i, i) for i in (1, 2, 3, 4)])
        self.assertEqual(sorted(result), [1, 2, 4])

    def test_rate_limit_surfaces_as_its_own_error(self):
        session = self.make_session()
        self.http.route("/torrents/requestdl", lambda *a: FakeResponse(429, {}))
        with self.assertRaises(RateLimitedError):
            session.generate_download_link(None, file_id=1, torrent_id=42)

    def test_server_error_surfaces_as_an_api_error(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: FakeResponse(503, {}))
        with self.assertRaises(api_session.ApiError):
            session.scan_torrents()

    def test_server_error_keeps_the_servers_own_explanation(self):
        # A 500 from requestdl is usually "not cached yet", and the body says
        # so. An opaque status code would be unactionable.
        session = self.make_session()
        self.http.route("/torrents/requestdl", lambda *a: FakeResponse(500, {
            "success": False, "error": "DATABASE_ERROR",
            "detail": "There was an error processing your request."}))
        with self.assertRaises(api_session.ApiError) as ctx:
            session.generate_download_link(None, file_id=1, torrent_id=42)
        message = str(ctx.exception)
        self.assertIn("500", message)
        self.assertIn("DATABASE_ERROR", message)
        self.assertIn("error processing your request", message)

    def test_a_failure_names_the_browser_escape_hatch_once(self):
        session = self.make_session()
        self.http.route("/torrents/mylist", lambda *a: FakeResponse(500, {"detail": "boom"}))
        with self.assertLogs("torbox_downloader.api_session", level="ERROR") as logs:
            for _ in range(3):
                with self.assertRaises(api_session.ApiError):
                    session.scan_torrents()
        hints = [line for line in logs.output if "fetch_backend" in line]
        self.assertEqual(len(hints), 1, "the escape hatch must be logged once, not per call")

    def test_unsuccessful_body_reports_the_detail(self):
        session = self.make_session()
        self.http.route("/torrents/requestdl", lambda *a: FakeResponse(
            200, {"success": False, "detail": "file not found"}))
        with self.assertRaises(api_session.ApiError) as ctx:
            session.generate_download_link(None, file_id=1, torrent_id=42)
        self.assertIn("file not found", str(ctx.exception))


class TestTokenLifecycle(ApiSessionTestCase):
    def test_valid_token_is_reused_without_refreshing(self):
        http = FakeTransport()
        http.route("/auth/v1/token", lambda *a: ok({"access_token": "refreshed"}))
        manager = TokenManager(http=http, token=live_session())
        first = manager.access_token()
        self.assertEqual(manager.access_token(), first)
        self.assertEqual(len(http.calls_to("/auth/v1/token")), 0)

    def test_expired_token_is_refreshed_and_persisted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = ProfileStore(base_dir=tmp)
        store.add("acct")
        state = store.load_state("acct") or {
            "cookies": [], "origins": [{"origin": "https://torbox.app", "localStorage": []}]}
        state["cookies"] = [{"name": "keepme", "value": "yes", "domain": ".torbox.app"}]
        state["origins"][0]["localStorage"] = [
            {"name": "sb-db-auth-token", "value": json.dumps(live_session(-60))},
            {"name": "torbox_session_token", "value": json.dumps(live_session(-60))},
            {"name": "unrelated", "value": "keep-me-too"},
        ]
        store.save_state("acct", state)

        http = FakeTransport()

        def refresh(url, body, headers, method):
            return FakeResponse(200, {"access_token": make_jwt(time.time() + 3600),
                                      "refresh_token": "rotated", "expires_in": 3600})

        http.route("/auth/v1/token", refresh)
        manager = TokenManager(http=http, profile_store=store, profile_name="acct")
        self.assertTrue(manager.access_token())

        self.assertEqual(len(http.calls_to("/auth/v1/token")), 1)
        saved = store.load_state("acct")
        self.assertEqual(saved["cookies"][0]["name"], "keepme", "cookies must survive")
        names = {e["name"]: e["value"] for e in saved["origins"][0]["localStorage"]}
        self.assertEqual(names["unrelated"], "keep-me-too", "unrelated keys must survive")
        for key in ("sb-db-auth-token", "torbox_session_token"):
            self.assertIn("rotated", names[key], f"{key} must hold the rotated token")

    def test_concurrent_callers_collapse_onto_one_refresh(self):
        http = FakeTransport()
        counter = {"n": 0}
        lock = threading.Lock()

        def refresh(url, body, headers, method):
            with lock:
                counter["n"] += 1
            time.sleep(0.05)
            return FakeResponse(200, {"access_token": make_jwt(time.time() + 3600),
                                      "refresh_token": "rotated", "expires_in": 3600})

        http.route("/auth/v1/token", refresh)
        manager = TokenManager(http=http, token=live_session(-60))
        results = []
        threads = [threading.Thread(target=lambda: results.append(manager.access_token()))
                   for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(counter["n"], 1, "20 threads must trigger exactly one refresh")
        self.assertEqual(len(set(results)), 1, "all threads must see the same token")

    def test_bare_jwt_without_a_refresh_token_cannot_be_renewed(self):
        http = FakeTransport()
        manager = TokenManager(http=http, token=make_jwt(time.time() - 10))
        with self.assertRaises(SessionExpiredError):
            manager.access_token()

    def test_rejected_refresh_raises_the_login_sentinel(self):
        http = FakeTransport()
        http.route("/auth/v1/token", lambda *a: FakeResponse(400, {"message": "bad"}))
        manager = TokenManager(http=http, token=live_session(-60))
        with self.assertRaises(SessionExpiredError) as ctx:
            manager.access_token()
        self.assertIn(api_session.SESSION_EXPIRED_SENTINEL, str(ctx.exception))

    def test_401_retries_once_then_raises(self):
        http = FakeTransport()
        http.route("/torrents/mylist", lambda *a: FakeResponse(401, {"detail": "expired"}))
        http.route("/auth/v1/token", lambda *a: raw(
            {"access_token": make_jwt(time.time() + 3600), "expires_in": 3600}))
        session = TorboxApiSession(token=live_session(-60), http=http)
        with self.assertRaises(SessionExpiredError) as ctx:
            session.scan_torrents()
        self.assertIn(api_session.SESSION_EXPIRED_SENTINEL, str(ctx.exception))
        self.assertEqual(len(http.calls_to("/torrents/mylist")), 2,
                         "the request must be retried exactly once, not looped")

    def test_update_token_resets_caches(self):
        session = self.make_session()
        session.scan_torrents()
        session.fetch_structure("Example Pack")
        session.update_token(live_session())
        self.http.calls.clear()
        session.scan_torrents()
        self.assertTrue(self.http.calls_to("mylist"), "a rescan must refetch")


class TestAnonKey(unittest.TestCase):
    def setUp(self):
        self._orig = dict(api_session._anon_cache)
        self._orig_get = api_session.requests.get

    def tearDown(self):
        api_session._anon_cache.update(self._orig)
        api_session.requests.get = self._orig_get

    def test_resolve_returns_the_seed_without_touching_the_network(self):
        api_session._anon_cache["key"] = ANON_KEY_SEED
        api_session._anon_cache["source"] = "seed"
        self.assertEqual(api_session.resolve_anon_key(), ANON_KEY_SEED)
        self.assertEqual(api_session.anon_key_source(), "seed")

    def test_derive_failure_keeps_the_cached_key(self):
        api_session._anon_cache["key"] = ANON_KEY_SEED
        api_session._anon_cache["source"] = "seed"

        def offline(*a, **k):
            raise OSError("network disabled in tests")

        api_session.requests.get = offline
        self.assertIsNone(derive_anon_key())
        self.assertEqual(api_session.resolve_anon_key(), ANON_KEY_SEED)
        self.assertEqual(api_session.anon_key_source(), "seed")

    def test_derive_reads_a_rotated_key_from_the_bundle(self):
        class R:
            def __init__(self, text):
                self.text = text

        pages = {
            "https://torbox.app": R('<script type="module" src="/assets/index-abc123.js">'),
            "https://torbox.app/assets/index-abc123.js": R('VITE_SUPABASE_KEY:"rotated-key"'),
        }
        api_session.requests.get = lambda url, **k: pages[url]
        api_session._anon_cache["key"] = ANON_KEY_SEED
        api_session._anon_cache["source"] = "seed"
        self.assertEqual(derive_anon_key(), "rotated-key")
        self.assertEqual(api_session.resolve_anon_key(), "rotated-key")
        self.assertEqual(api_session.anon_key_source(), "live")


if __name__ == "__main__":
    unittest.main()
