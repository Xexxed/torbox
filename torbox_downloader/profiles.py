"""Named Torbox accounts ("profiles"), each backed by a Playwright storage state.

Torbox authenticates through Supabase: the browser keeps a *session object*
(``access_token`` + ``refresh_token``) in ``localStorage`` under
``sb-db-auth-token`` and silently exchanges the refresh token for a new pair
roughly every hour. That is why a signed-in browser stays signed in and a
copied token does not.

So instead of storing a token, each profile stores the whole browser storage
state -- cookies plus ``localStorage`` -- exactly as Playwright exports it::

    {"cookies": [...], "origins": [{"origin": "...", "localStorage": [...]}]}

Loading a profile is ``new_context(storage_state=...)``; saving the *refreshed*
session is ``context.storage_state(path=...)``. Because the browser performs the
refresh itself, this needs no API key and no token parsing -- and because each
profile owns a distinct session, two profiles can never invalidate each other's
refresh token.

Legacy ``token.txt`` (a raw Supabase session dict or JWT) is imported into the
``default`` profile on first run.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"
PROFILES_DIRNAME = "profiles"
REGISTRY_FILENAME = "profiles.json"
LEGACY_TOKEN_FILE = "token.txt"

ORIGIN = "https://torbox.app"
# Both keys are written by the app today; keep them in sync so either the SPA or
# a legacy code path can find the session.
SESSION_STORAGE_KEYS = ("sb-db-auth-token", "torbox_session_token")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileError(RuntimeError):
    """Raised when a profile operation cannot be completed."""


def valid_name(name):
    """Profile names become filenames, so they are restricted to a safe slug."""
    return bool(name) and bool(_NAME_RE.match(str(name)))


def _slugify(name):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name).strip()).strip("-._")
    return slug[:64] or DEFAULT_PROFILE


class ProfileStore:
    """On-disk registry of profiles and their storage-state files.

    Layout (relative to ``base_dir``, which defaults to the working directory
    so this sits alongside ``settings.json``)::

        profiles.json          registry: active profile + per-profile metadata
        profiles/<name>.json   Playwright storage state for that account
    """

    def __init__(self, base_dir=None):
        self.base_dir = os.path.abspath(base_dir or os.getcwd())
        self.dir = os.path.join(self.base_dir, PROFILES_DIRNAME)
        self.registry_path = os.path.join(self.base_dir, REGISTRY_FILENAME)
        self._lock = threading.RLock()
        self._registry = {"version": 1, "active": None, "profiles": {}}
        self.load()

    # --- persistence -------------------------------------------------

    def load(self):
        with self._lock:
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
                    self._registry = {
                        "version": int(data.get("version", 1)),
                        "active": data.get("active"),
                        "profiles": data["profiles"],
                    }
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("Could not read %s: %s", self.registry_path, e)
            # Drop entries whose active profile no longer exists.
            if self._registry["active"] not in self._registry["profiles"]:
                self._registry["active"] = None
            return self._registry

    def save(self):
        with self._lock:
            self._atomic_write(self.registry_path, self._registry)

    @staticmethod
    def _atomic_write(path, payload):
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- CRUD --------------------------------------------------------

    def names(self):
        with self._lock:
            return sorted(self._registry["profiles"])

    def exists(self, name):
        with self._lock:
            return name in self._registry["profiles"]

    def get(self, name):
        with self._lock:
            entry = self._registry["profiles"].get(name)
            return dict(entry) if entry else None

    def add(self, name, email="", dest_dir=""):
        """Register a profile. Returns its (created) registry entry."""
        with self._lock:
            if not valid_name(name):
                raise ProfileError(
                    "Profile names may only contain letters, digits, '.', '_' and '-'."
                )
            entry = self._registry["profiles"].get(name)
            if entry is None:
                entry = {
                    "name": name,
                    "email": email or "",
                    "dest_dir": dest_dir or "",
                    "created_at": time.time(),
                    "last_used": None,
                }
                self._registry["profiles"][name] = entry
            else:
                if email:
                    entry["email"] = email
                if dest_dir:
                    entry["dest_dir"] = dest_dir
            if not self._registry["active"]:
                self._registry["active"] = name
            self.save()
            return dict(entry)

    def remove(self, name):
        """Forget a profile and delete its storage state. Returns True if it existed."""
        with self._lock:
            existed = self._registry["profiles"].pop(name, None) is not None
            if existed:
                if self._registry["active"] == name:
                    remaining = self.names()
                    self._registry["active"] = remaining[0] if remaining else None
                self.save()
        if existed:
            try:
                os.unlink(self.storage_path(name))
            except OSError:
                pass
        return existed

    def active(self):
        with self._lock:
            return self._registry["active"]

    def set_active(self, name):
        with self._lock:
            if name not in self._registry["profiles"]:
                raise ProfileError(f"Unknown profile: {name}")
            self._registry["active"] = name
            entry = self._registry["profiles"][name]
            entry["last_used"] = time.time()
            self.save()
            return dict(entry)

    def set_email(self, name, email):
        with self._lock:
            entry = self._registry["profiles"].get(name)
            if entry and entry.get("email") != email:
                entry["email"] = email
                self.save()

    def set_dest_dir(self, name, dest_dir):
        with self._lock:
            entry = self._registry["profiles"].get(name)
            if entry:
                entry["dest_dir"] = dest_dir or ""
                self.save()

    def list(self):
        """Registry entries with a ``has_login`` flag, safe to send to the UI."""
        with self._lock:
            active = self._registry["active"]
            out = []
            for name in self.names():
                entry = dict(self._registry["profiles"][name])
                entry["has_login"] = self.has_state(name)
                entry["active"] = name == active
                out.append(entry)
            return out

    # --- storage state -----------------------------------------------

    def storage_path(self, name):
        if not valid_name(name):
            raise ProfileError(f"Invalid profile name: {name}")
        return os.path.join(self.dir, f"{name}.json")

    def has_state(self, name):
        return os.path.exists(self.storage_path(name))

    def load_state(self, name):
        """Return the Playwright storage state for ``name``, or None."""
        path = self.storage_path(name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("Could not read storage state %s: %s", path, e)
            return None
        if not isinstance(state, dict) or "origins" not in state and "cookies" not in state:
            return None
        return state

    def save_state(self, name, state):
        """Persist a storage state, refreshing the cached email alongside it."""
        if not isinstance(state, dict):
            return False
        path = self.storage_path(name)
        try:
            self._atomic_write(path, state)
        except Exception as e:
            logger.warning("Could not save storage state %s: %s", path, e)
            return False
        email = extract_email(state)
        if email:
            self.set_email(name, email)
        return True

    # --- legacy migration --------------------------------------------

    def import_legacy_token(self, token_text):
        """Create the ``default`` profile from an old ``token.txt``.

        Returns the profile name, or None when there was nothing to import.
        """
        token_text = (token_text or "").strip()
        if not token_text:
            return None
        state = session_to_storage_state(token_text)
        if state is None:
            return None
        self.add(DEFAULT_PROFILE, email=extract_email(state) or "")
        self.save_state(DEFAULT_PROFILE, state)
        if not self.active():
            self.set_active(DEFAULT_PROFILE)
        return DEFAULT_PROFILE

    def ensure_default(self):
        """Make sure at least one profile is registered, migrating token.txt if present."""
        with self._lock:
            if self._registry["profiles"]:
                return self.active() or self.names()[0]
        legacy = os.path.join(self.base_dir, LEGACY_TOKEN_FILE)
        if os.path.exists(legacy):
            try:
                with open(legacy, "r", encoding="utf-8") as f:
                    imported = self.import_legacy_token(f.read())
                if imported:
                    logger.info("Imported %s into the '%s' profile.", LEGACY_TOKEN_FILE, imported)
            except Exception as e:
                logger.warning("Could not import %s: %s", legacy, e)
        return self.active()


# --- helpers ---------------------------------------------------------


def session_to_storage_state(token_text):
    """Wrap a Supabase session (JSON dict or bare JWT) as a Playwright state."""
    token_text = (token_text or "").strip()
    if not token_text:
        return None
    value = token_text
    if token_text.startswith("{"):
        try:
            parsed = json.loads(token_text)
        except Exception:
            return None
        if not isinstance(parsed, dict) or not parsed.get("access_token"):
            return None
        value = json.dumps(parsed)
    return {
        "cookies": [],
        "origins": [
            {
                "origin": ORIGIN,
                "localStorage": [
                    {"name": key, "value": value} for key in SESSION_STORAGE_KEYS
                ],
            }
        ],
    }


def _iter_session_values(state):
    """Yield candidate session payloads found in a storage state."""
    if not isinstance(state, dict):
        return
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for item in origin.get("localStorage") or []:
            if isinstance(item, dict) and item.get("value"):
                yield item["value"]


def write_session(state, session):
    """Write ``session`` into a storage state's TorBox origin, in place.

    The single definition of *where* the session lives in a storage state,
    shared by ``TokenManager``'s hourly write-back and by the paste importer so
    an imported account and a browser-signed-in one converge on the same file.

    The origin that already holds a session key is preferred over the TorBox
    one, because relocating a working account's session between origins is a
    change nobody asked for; a state with neither gets a fresh TorBox origin.
    Returns the state.
    """
    if not isinstance(state, dict):
        return state
    origins = state.setdefault("origins", [])
    text = json.dumps(session)

    for key in SESSION_STORAGE_KEYS:
        for origin in origins:
            if not isinstance(origin, dict):
                continue
            for item in origin.get("localStorage") or []:
                if isinstance(item, dict) and item.get("name") == key:
                    item["value"] = text

    target = None
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if not isinstance(origin.get("localStorage"), list):
            continue
        if any(isinstance(i, dict) and i.get("name") in SESSION_STORAGE_KEYS
               for i in origin["localStorage"]):
            target = origin
            break
        if origin.get("origin") == ORIGIN and target is None:
            target = origin
    if target is None:
        target = next((o for o in origins
                       if isinstance(o, dict) and "torbox" in str(o.get("origin", ""))), None)
    if target is None:
        target = {"origin": ORIGIN, "localStorage": []}
        origins.append(target)
    entries = target.setdefault("localStorage", [])

    present = {i.get("name") for i in entries if isinstance(i, dict)}
    for key in SESSION_STORAGE_KEYS:
        if key not in present:
            entries.append({"name": key, "value": text})
    return state


def extract_session(state):
    """Pull the Supabase session dict out of a storage state, if present."""
    for value in _iter_session_values(state):
        try:
            parsed = json.loads(value)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("access_token"):
            return parsed
    return None


def extract_email(state):
    session = extract_session(state)
    if not session:
        return ""
    user = session.get("user")
    if isinstance(user, dict):
        return user.get("email") or ""
    return ""


def extract_expiry(state):
    """Return the access-token ``exp`` as a Unix timestamp, or None."""
    session = extract_session(state)
    if not session:
        return None
    exp = session.get("expires_at")
    if exp:
        try:
            return float(exp)
        except (TypeError, ValueError):
            pass
    token = session.get("access_token") or ""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    import base64

    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    try:
        return float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return None
