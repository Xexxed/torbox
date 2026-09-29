"""Tests for importing an account from a pasted session JSON.

The point of these is the boring half of the feature: a payload has to survive
being turned into the same profile file the browser sign-in writes, and a
cookie export -- which is what people actually reach for first -- has to be
refused with an explanation rather than quietly producing a dead account.

Everything runs against an injected fake transport and a temporary directory.
No network, no browser, and no access to the real ``profiles/`` directory.
"""

import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import requests

from torbox_downloader import profiles as profiles_mod
from torbox_downloader.profiles import ProfileStore, extract_email, extract_session
from torbox_downloader.session_import import (
    SessionImportError,
    import_session,
    parse_session_payload,
    verify_session,
)


def make_jwt(email="user@example.com", exp=None, offset=3600):
    payload = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "email": email,
        "exp": int(exp if exp is not None else time.time() + offset),
        "iss": "https://bejxfytknnkoegtteuzs.supabase.co/auth/v1",
        "role": "authenticated",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def make_session(email="user@example.com", offset=3600, refresh="abcd1234efgh"):
    session = {
        "access_token": make_jwt(email=email, offset=offset),
        "token_type": "bearer",
        "expires_in": 3600,
        "expires_at": int(time.time()) + offset,
        "user": {"email": email, "id": "11111111-2222-3333-4444-555555555555"},
    }
    if refresh:
        session["refresh_token"] = refresh
    return session


def make_storage_state(session, cookies=None):
    return {
        "cookies": cookies if cookies is not None else [],
        "origins": [
            {
                "origin": "https://torbox.app",
                "localStorage": [
                    {"name": "sb-db-auth-token", "value": json.dumps(session)},
                    {"name": "torbox-settings", "value": "{}"},
                ],
            }
        ],
    }


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
    """Answers the GoTrue refresh endpoint, and records what it was sent."""

    def __init__(self, response=None, error=None):
        self.headers = {}
        self.posts = []
        self._response = response
        self._error = error

    def post(self, url, params=None, headers=None, json=None, timeout=None):
        self.posts.append(json)
        if self._error is not None:
            raise self._error
        return self._response


def ok_transport(access=None, refresh="rotated-refresh", expires_in=3600):
    return FakeTransport(FakeResponse(200, {
        "access_token": access or make_jwt(offset=expires_in),
        "refresh_token": refresh,
        "expires_in": expires_in,
    }))


# --- parsing ------------------------------------------------------------


class TestAcceptedPayloads(unittest.TestCase):
    def test_bare_supabase_session(self):
        session = make_session(email="a@b.c")
        imported = parse_session_payload(json.dumps(session))
        self.assertEqual(imported.source, "session")
        self.assertTrue(imported.has_refresh_token)
        self.assertEqual(imported.access_token, session["access_token"])
        self.assertEqual(imported.email, "a@b.c")
        # Both session keys are written, so either reader finds it.
        stored = extract_session(imported.storage_state)
        self.assertEqual(stored["access_token"], session["access_token"])
        names = {i["name"] for i in imported.storage_state["origins"][0]["localStorage"]}
        self.assertEqual(names, set(profiles_mod.SESSION_STORAGE_KEYS))

    def test_local_storage_map(self):
        session = make_session()
        text = json.dumps({"sb-db-auth-token": json.dumps(session), "unrelated": "keep"})
        imported = parse_session_payload(text)
        self.assertEqual(imported.source, "local_storage_map")
        self.assertEqual(imported.access_token, session["access_token"])

    def test_local_storage_map_holding_an_object(self):
        # Some exports unescape the nested value instead of stringifying it.
        session = make_session()
        imported = parse_session_payload(json.dumps({"torbox_session_token": session}))
        self.assertEqual(imported.access_token, session["access_token"])

    def test_full_storage_state_keeps_cookies_and_other_keys(self):
        session = make_session()
        cookie = {"name": "_cfuvid", "domain": ".torbox.app", "value": "x"}
        imported = parse_session_payload(json.dumps(
            make_storage_state(session, [cookie])))
        self.assertEqual(imported.source, "storage_state")
        self.assertEqual(imported.cookie_count, 1)
        self.assertEqual(imported.storage_state["cookies"][0]["name"], "_cfuvid")
        keys = {i["name"] for i in imported.storage_state["origins"][0]["localStorage"]}
        self.assertIn("torbox-settings", keys)

    def test_double_encoded_json(self):
        session = make_session()
        imported = parse_session_payload(json.dumps(json.dumps(session)))
        self.assertEqual(imported.access_token, session["access_token"])

    def test_nested_session_is_found(self):
        session = make_session()
        imported = parse_session_payload(json.dumps(
            {"event": "SIGNED_IN", "session": session}))
        self.assertEqual(imported.access_token, session["access_token"])

    def test_bare_jwt_is_accepted_but_advertised_as_final(self):
        imported = parse_session_payload(make_jwt())
        self.assertEqual(imported.source, "jwt")
        self.assertFalse(imported.has_refresh_token)
        self.assertIn("expires in about an hour", imported.summary())

    def test_user_object_survives_normalisation(self):
        # Without this the profile list would show a bare name: the address
        # only ever lives in the session's `user` object.
        imported = parse_session_payload(json.dumps(make_session(email="keep@me.io")))
        self.assertEqual(extract_email(imported.storage_state), "keep@me.io")

    def test_no_expires_at_invents_nothing(self):
        session = make_session()
        session.pop("expires_at")
        imported = parse_session_payload(json.dumps(session))
        stored = extract_session(imported.storage_state)
        self.assertNotIn("expires_at", stored)

    def test_byte_order_mark_is_tolerated(self):
        # PowerShell's Out-File, Notepad and plenty of editors prepend one, and
        # U+FEFF is not whitespace, so a saved export would otherwise be
        # reported as unreadable.
        imported = parse_session_payload(chr(0xFEFF) + json.dumps(make_session()))
        self.assertEqual(imported.source, "session")

    def test_leading_whitespace_is_tolerated(self):
        self.assertEqual(
            parse_session_payload("\n  \t" + json.dumps(make_session()) + "  \n").source,
            "session")


class TestRejectedPayloads(unittest.TestCase):
    def _assert_cookie_error(self, text):
        with self.assertRaises(SessionImportError) as ctx:
            parse_session_payload(text)
        message = str(ctx.exception)
        self.assertIn("cookies are not enough", message)
        self.assertIn("sb-db-auth-token", message)

    def test_cookie_list_is_refused(self):
        self._assert_cookie_error(json.dumps(
            [{"name": "_cfuvid", "domain": ".torbox.app", "value": "x"}]))

    def test_cookies_only_storage_state_is_refused(self):
        # The shape an account's profile file has if the localStorage never
        # made it into the export: 58 valid cookies, no identity.
        self._assert_cookie_error(json.dumps({
            "cookies": [{"name": "_cfuvid", "domain": ".torbox.app", "value": "x"}],
            "origins": [],
        }))

    def test_netscape_cookies_txt_is_refused(self):
        self._assert_cookie_error(
            "# Netscape HTTP Cookie File\n"
            "torbox.app\tTRUE\t/\tTRUE\t1792098405\t_cfuvid\tabc123\n"
            ".torbox.app\tTRUE\t/\tFALSE\t1792098405\t_vid_t\txyz\n"
        )

    def test_empty_input(self):
        with self.assertRaises(SessionImportError):
            parse_session_payload("   ")
        with self.assertRaises(SessionImportError):
            parse_session_payload(None)

    def test_prose_and_broken_json(self):
        for text in ("hello there", "{not json", "[]", '{"no_token": 1}'):
            with self.assertRaises(SessionImportError, msg=text):
                parse_session_payload(text)

    def test_session_key_without_a_token(self):
        with self.assertRaises(SessionImportError) as ctx:
            parse_session_payload(json.dumps({"sb-db-auth-token": "{}"}))
        self.assertIn("access_token", str(ctx.exception))


# --- verification -------------------------------------------------------


class TestVerification(unittest.TestCase):
    def test_live_token_is_accepted_without_a_network_call(self):
        http = ok_transport()
        imported, note = verify_session(parse_session_payload(
            json.dumps(make_session(email="ok@b.c"))), http=http)
        self.assertEqual(http.posts, [])
        self.assertEqual(note, "")
        self.assertEqual(imported.email, "ok@b.c")

    def test_expired_token_is_refreshed_and_the_new_pair_saved(self):
        # A paste is usually made right after signing in, so this is the
        # exception -- but when it happens the rotation must land in the file,
        # or the refresh token the paste carried is spent and lost.
        http = ok_transport(refresh="rotated")
        expired = make_session(offset=-600, refresh="original")
        imported, note = verify_session(parse_session_payload(json.dumps(expired)), http=http)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.posts[0], {"refresh_token": "original"})
        self.assertEqual(imported.refresh_token, "rotated")
        self.assertGreater(imported.session["expires_at"], time.time())
        self.assertEqual(extract_session(imported.storage_state)["refresh_token"], "rotated")
        self.assertEqual(note, "")

    def test_identity_survives_the_rotation(self):
        http = ok_transport(refresh="rotated")
        imported, _ = verify_session(parse_session_payload(
            json.dumps(make_session(email="me@x.io", offset=-600))), http=http)
        self.assertEqual(imported.email, "me@x.io")

    def test_rejected_refresh_raises(self):
        http = FakeTransport(FakeResponse(400, {"error": "Invalid Refresh Token"}))
        with self.assertRaises(SessionImportError) as ctx:
            verify_session(parse_session_payload(
                json.dumps(make_session(offset=-600))), http=http)
        self.assertIn("rejected", str(ctx.exception))

    def test_rejection_without_a_refresh_token_says_so(self):
        # A bare JWT that has already aged out can never be renewed, and the
        # user deserves to know that before, not an hour later.
        http = FakeTransport(FakeResponse(400, {"error": "bad"}))
        with self.assertRaises(SessionImportError) as ctx:
            verify_session(parse_session_payload(make_jwt(offset=-600)), http=http)
        self.assertIn("no refresh token", str(ctx.exception))

    def test_network_failure_does_not_block_the_import(self):
        # A transport error is not a verdict on the token, and refusing the
        # import because the network blinked would be worse than the risk.
        # An expired session is used so a refresh -- and so the network call --
        # actually happens.
        http = FakeTransport(error=requests.RequestException("no route to host"))
        imported, note = verify_session(parse_session_payload(
            json.dumps(make_session(email="offline@b.c", offset=-600))), http=http)
        self.assertEqual(imported.email, "offline@b.c")
        self.assertIn("Could not reach TorBox", note)

    def test_no_token_manager_writeback_during_verification(self):
        # The manager is built without a profile store, so a rotation that
        # happened here cannot be written to some *other* profile's file.
        http = ok_transport(refresh="rotated")
        store = ProfileStore(tempfile.mkdtemp(prefix="torbox-import-"))
        self.addCleanup(shutil.rmtree, store.base_dir, True)
        store.add("other")
        store.save_state("other", make_storage_state(make_session(refresh="untouched")))
        verify_session(parse_session_payload(
            json.dumps(make_session(offset=-600))), http=http)
        self.assertEqual(extract_session(store.load_state("other"))["refresh_token"],
                         "untouched")


# --- the profile it writes ---------------------------------------------


class ImportStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="torbox-import-")
        self.store = ProfileStore(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_import_writes_a_profile_a_backend_can_load(self):
        result = import_session(self.store, "pasted", json.dumps(make_session(email="p@q.io")),
                                http=ok_transport())
        self.assertEqual(result["name"], "pasted")
        self.assertTrue(result["has_refresh_token"])
        self.assertEqual(self.store.get("pasted")["email"], "p@q.io")
        self.assertIn("pasted", self.store.names())
        # The HTTP backend's own loader accepts it, which is the real test.
        self.assertEqual(extract_session(self.store.load_state("pasted"))["access_token"],
                         make_jwt(email="p@q.io"))

    def test_import_does_not_hijack_the_active_account(self):
        self.store.add("existing")
        self.store.set_active("existing")
        import_session(self.store, "pasted", json.dumps(make_session()), http=ok_transport())
        self.assertEqual(self.store.active(), "existing")

    def test_refuses_an_existing_name(self):
        self.store.add("taken")
        with self.assertRaises(Exception) as ctx:
            import_session(self.store, "taken", json.dumps(make_session()), http=ok_transport())
        self.assertIn("already exists", str(ctx.exception))

    def test_refuses_an_unsafe_name(self):
        with self.assertRaises(Exception):
            import_session(self.store, "../escape", json.dumps(make_session()),
                           http=ok_transport())
        self.assertEqual(self.store.names(), [])

    def test_a_rejected_paste_leaves_nothing_behind(self):
        with self.assertRaises(SessionImportError):
            import_session(self.store, "pasted", json.dumps(
                [{"name": "a", "domain": "torbox.app"}]), http=ok_transport())
        self.assertEqual(self.store.names(), [])
        self.assertFalse(self.store.has_state("pasted"))

    def test_imported_profile_survives_a_token_manager_cycle(self):
        # The end-to-end claim: an imported account refreshes itself. Load it
        # the way the HTTP backend does, force the hourly rotation, and confirm
        # the new pair is on disk and that the cookies survived it.
        cookies = [{"name": "_cfuvid", "domain": ".torbox.app", "value": "x"}]
        import_session(self.store, "pasted",
                       json.dumps(make_storage_state(make_session(refresh="original"), cookies)),
                       http=ok_transport())
        from torbox_downloader.api_session import TokenManager

        manager = TokenManager(profile_store=self.store, profile_name="pasted",
                               http=ok_transport(refresh="rotated"))
        # The pasted token is still live, so expire it to force the exchange.
        manager._state = (manager._state[0], "original", time.time() - 1)
        self.assertTrue(manager.access_token())

        stored = extract_session(self.store.load_state("pasted"))
        self.assertEqual(stored["refresh_token"], "rotated")
        self.assertEqual(self.store.load_state("pasted")["cookies"][0]["name"], "_cfuvid")


class TestImportEndpoint(unittest.TestCase):
    """The add-account modal's second route, over the real request handler."""

    def setUp(self):
        import gui_server

        self.gui_server = gui_server
        self.dir = tempfile.mkdtemp(prefix="torbox-endpoint-")
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        # A fresh AppState in a throwaway directory, so the real profiles/ and
        # settings.json are never touched.
        self.real_state = gui_server.app_state
        gui_server.app_state = gui_server.AppState()
        self.addCleanup(self._restore)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui_server.GUIRequestHandler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _restore(self):
        self.gui_server.app_state = self.real_state
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, path, body):
        # The key is a per-process uuid the page receives in its URL, so the
        # endpoint is exercised through the real auth gate rather than around it.
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-API-Key": self.gui_server.app_state.api_key},
            method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_imports_a_session(self):
        body = {"name": "pasted", "payload": json.dumps(make_session(email="via@http.io"))}
        with mock.patch("torbox_downloader.session_import.verify_session",
                        side_effect=lambda imported, http=None: (imported, "")):
            data = self.post("/api/profiles/import", body)
        self.assertTrue(data["success"], data)
        self.assertEqual(data["email"], "via@http.io")
        self.assertTrue(data["has_refresh_token"])
        self.assertIn("pasted", [p["name"] for p in data["profiles"]])

    def test_reports_the_reason_a_paste_failed(self):
        # The modal shows data['error'] verbatim, so it has to be the
        # explanation, not a stack trace.
        data = self.post("/api/profiles/import", {
            "name": "pasted",
            "payload": json.dumps([{"name": "_cfuvid", "domain": ".torbox.app"}]),
        })
        self.assertFalse(data["success"])
        self.assertIn("sb-db-auth-token", data["error"])

    def test_refuses_a_duplicate_name(self):
        self.gui_server.app_state.profiles.add("taken")
        data = self.post("/api/profiles/import", {
            "name": "taken", "payload": json.dumps(make_session())})
        self.assertFalse(data["success"])
        self.assertIn("already exists", data["error"])

    def test_requires_a_name_and_a_payload(self):
        self.assertIn("name is required",
                      self.post("/api/profiles/import", {"payload": "{}"})["error"])
        self.assertIn("Paste the session JSON first",
                      self.post("/api/profiles/import", {"name": "x"})["error"])
        self.assertIn("Paste the session JSON first",
                      self.post("/api/profiles/import",
                                {"name": "x", "payload": "   "})["error"])


if __name__ == "__main__":
    unittest.main()
