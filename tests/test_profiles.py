"""Tests for the profile store: named accounts backed by Playwright storage state.

Covers the registry CRUD, the storage-state round trip, and the migration of a
legacy token.txt. No browser and no network required.
"""

import base64
import json
import os
import shutil
import tempfile
import time
import unittest

from torbox_downloader.profiles import (
    DEFAULT_PROFILE,
    LEGACY_TOKEN_FILE,
    ProfileStore,
    extract_email,
    extract_expiry,
    extract_session,
    session_to_storage_state,
    valid_name,
)


def make_jwt(email="user@example.com", exp=None, offset=3600):
    """Build a fake (unsigned) JWT carrying the claims the app reads."""
    payload = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "email": email,
        "exp": int(exp if exp is not None else time.time() + offset),
        "iss": "https://bejxfytknnkoegtteuzs.supabase.co/auth/v1",
        "role": "authenticated",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def make_session(email="user@example.com", offset=3600):
    return {
        "access_token": make_jwt(email=email, offset=offset),
        "token_type": "bearer",
        "expires_in": 3600,
        "expires_at": int(time.time()) + offset,
        "refresh_token": "abcd1234efgh",
        "user": {"email": email, "id": "11111111-2222-3333-4444-555555555555"},
    }


class TestNames(unittest.TestCase):
    def test_accepts_safe_slugs(self):
        for name in ("default", "main", "acct-2", "work.2026", "a_b"):
            self.assertTrue(valid_name(name), name)

    def test_rejects_path_traversal_and_empty(self):
        # Profile names become filenames, so these must never get through.
        for name in ("", "../evil", "a/b", "..", "/root", "with space"):
            self.assertFalse(valid_name(name), name)


class TestSessionConversion(unittest.TestCase):
    def test_wraps_json_session(self):
        session = make_session()
        state = session_to_storage_state(json.dumps(session))
        self.assertIsNotNone(state)
        self.assertEqual(state["cookies"], [])
        origin = state["origins"][0]
        self.assertEqual(origin["origin"], "https://torbox.app")
        keys = {item["name"] for item in origin["localStorage"]}
        self.assertIn("sb-db-auth-token", keys)
        self.assertIn("torbox_session_token", keys)
        for item in origin["localStorage"]:
            self.assertEqual(json.loads(item["value"])["access_token"], session["access_token"])

    def test_wraps_bare_jwt(self):
        token = make_jwt()
        state = session_to_storage_state(token)
        self.assertIsNotNone(state)
        self.assertEqual(
            state["origins"][0]["localStorage"][0]["value"], token
        )

    def test_rejects_garbage(self):
        self.assertIsNone(session_to_storage_state(""))
        self.assertIsNone(session_to_storage_state(None))
        self.assertIsNone(session_to_storage_state("{not json"))
        self.assertIsNone(session_to_storage_state('{"no_token": 1}'))


class TestExtraction(unittest.TestCase):
    def test_round_trip_through_storage_state(self):
        session = make_session(email="someone@example.com")
        state = session_to_storage_state(json.dumps(session))
        self.assertEqual(extract_session(state)["access_token"], session["access_token"])
        self.assertEqual(extract_email(state), "someone@example.com")

    def test_email_read_from_user_object(self):
        state = session_to_storage_state(json.dumps(make_session(email="x@y.z")))
        self.assertEqual(extract_email(state), "x@y.z")

    def test_expiry_prefers_expires_at(self):
        session = make_session(offset=42)
        state = session_to_storage_state(json.dumps(session))
        self.assertAlmostEqual(extract_expiry(state), session["expires_at"], delta=2)

    def test_expiry_falls_back_to_jwt_claim(self):
        exp = int(time.time()) + 999
        session = make_session()
        session.pop("expires_at")
        session["access_token"] = make_jwt(exp=exp)
        state = session_to_storage_state(json.dumps(session))
        self.assertEqual(extract_expiry(state), float(exp))

    def test_missing_session_returns_none(self):
        self.assertIsNone(extract_session({"cookies": [], "origins": []}))
        self.assertEqual(extract_email({"cookies": [], "origins": []}), "")
        self.assertIsNone(extract_expiry({"cookies": [], "origins": []}))


class ProfileStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="torbox-profiles-")
        self.store = ProfileStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestRegistry(ProfileStoreTestCase):
    def test_add_sets_first_profile_active(self):
        self.store.add("main")
        self.assertEqual(self.store.active(), "main")
        self.assertEqual(self.store.names(), ["main"])

    def test_add_is_idempotent(self):
        self.store.add("main", email="a@b.c")
        self.store.add("main", email="d@e.f")
        self.assertEqual(len(self.store.names()), 1)
        self.assertEqual(self.store.get("main")["email"], "d@e.f")

    def test_per_profile_dest_dir(self):
        self.store.add("main")
        self.store.set_dest_dir("main", "D:\\main")
        self.assertEqual(self.store.get("main")["dest_dir"], "D:\\main")
        # Clearing it falls the account back to the global path.
        self.store.set_dest_dir("main", "")
        self.assertEqual(self.store.get("main")["dest_dir"], "")

    def test_rejects_unsafe_name(self):
        with self.assertRaises(Exception):
            self.store.add("../escape")

    def test_remove_deletes_state_and_reassigns_active(self):
        self.store.add("main")
        self.store.add("backup")
        self.store.save_state("backup", {"cookies": [], "origins": []})
        self.store.set_active("backup")

        self.assertTrue(self.store.remove("backup"))
        self.assertNotIn("backup", self.store.names())
        self.assertFalse(self.store.has_state("backup"))
        # Active fell back to a profile that still exists.
        self.assertEqual(self.store.active(), "main")

    def test_set_active_unknown_profile(self):
        with self.assertRaises(Exception):
            self.store.set_active("nope")

    def test_registry_persists(self):
        self.store.add("main", email="a@b.c")
        reloaded = ProfileStore(self.dir)
        self.assertEqual(reloaded.names(), ["main"])
        self.assertEqual(reloaded.get("main")["email"], "a@b.c")
        self.assertEqual(reloaded.active(), "main")

    def test_active_that_vanished_is_cleared(self):
        self.store.add("main")
        path = os.path.join(self.dir, "profiles.json")
        with open(path, encoding="utf-8") as f:
            registry = json.load(f)
        registry["active"] = "ghost"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(registry, f)
        self.assertIsNone(ProfileStore(self.dir).active())


class TestStorageState(ProfileStoreTestCase):
    def test_save_and_load_round_trip(self):
        self.store.add("main")
        state = session_to_storage_state(json.dumps(make_session(email="round@trip.io")))
        self.assertTrue(self.store.save_state("main", state))
        loaded = self.store.load_state("main")
        self.assertEqual(loaded["origins"][0]["origin"], state["origins"][0]["origin"])
        # Saving also caches the email for display.
        self.assertEqual(self.store.get("main")["email"], "round@trip.io")

    def test_load_missing_state(self):
        self.store.add("main")
        self.assertIsNone(self.store.load_state("main"))
        self.assertFalse(self.store.has_state("main"))

    def test_load_corrupt_state(self):
        self.store.add("main")
        path = self.store.storage_path("main")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertIsNone(self.store.load_state("main"))

    def test_list_flags_active_and_login(self):
        self.store.add("main")
        self.store.add("backup")
        self.store.save_state("backup", {"cookies": [], "origins": []})
        listing = {entry["name"]: entry for entry in self.store.list()}
        self.assertTrue(listing["main"]["active"])
        self.assertFalse(listing["main"]["has_login"])
        self.assertTrue(listing["backup"]["has_login"])


class TestLegacyMigration(ProfileStoreTestCase):
    def test_import_creates_default_profile(self):
        session = make_session(email="legacy@example.com")
        with open(os.path.join(self.dir, LEGACY_TOKEN_FILE), "w", encoding="utf-8") as f:
            f.write(json.dumps(session))

        self.assertEqual(self.store.ensure_default(), DEFAULT_PROFILE)
        self.assertIn(DEFAULT_PROFILE, self.store.names())
        state = self.store.load_state(DEFAULT_PROFILE)
        self.assertEqual(extract_email(state), "legacy@example.com")

    def test_import_ignores_empty_token(self):
        self.assertIsNone(self.store.import_legacy_token(""))
        self.assertEqual(self.store.names(), [])

    def test_ensure_default_without_token(self):
        self.assertIsNone(self.store.ensure_default())
        self.assertEqual(self.store.names(), [])


if __name__ == "__main__":
    unittest.main()
