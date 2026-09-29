"""Add an account by pasting the session JSON instead of signing in.

Signing in through a browser is a one-time cost, but it is a real cost: Chrome
has to be found, Turnstile has to be satisfied over CDP, and a human has to type
a password. This module takes the other route -- hand it the JSON the signed-in
browser is already holding and it builds the same profile file.

**Cookies are not enough.** TorBox authenticates with Supabase, and the session
lives in ``localStorage["sb-db-auth-token"]`` on ``https://torbox.app``, not in a
cookie. A dump of the account's cookies contains Cloudflare and analytics values
and nothing that identifies a user, so a cookie-only export is rejected with an
explanation rather than silently producing a profile that dies in an hour. What
carries the identity -- and, crucially, the ``refresh_token`` -- is that
``localStorage`` value. Paste it and the account refreshes itself forever, via
exactly the same ``TokenManager`` every other profile uses.

Because different tools export different shapes, the parser is deliberately
permissive and accepts, in order:

============================  ==================================================
Playwright storage state      ``{"cookies": [...], "origins": [...]}`` -- a
                              full profile dump, cookies and ``localStorage``.
localStorage map             ``{"sb-db-auth-token": "..."}``, what copying all
                              of a page's local storage gives you.
Supabase session             ``{"access_token": ..., "refresh_token": ...}``
                              -- the value of the ``sb-db-auth-token`` key.
Netscape / cookie list       ``[{"name": ..., "domain": ...}]`` or a
                              cookies.txt body. Parsed, then **rejected**: there
                              is no session in it.
Bare JWT                     A raw ``header.payload.signature`` string. Works,
                              but carries no refresh token, so it cannot renew.
============================  ==================================================

The parsed session is verified against the live GoTrue endpoint before it is
saved, so a stale paste fails immediately with a readable reason instead of
producing an account that breaks on first use. A network failure is *not*
treated as a bad paste: the import goes through and says it could not check.
"""

import json
import logging
import re

from torbox_downloader.profiles import (
    ORIGIN,
    SESSION_STORAGE_KEYS,
    ProfileError,
    session_to_storage_state,
    valid_name,
    write_session,
)

logger = logging.getLogger(__name__)

# How many times a payload may be JSON-decoded before giving up. Two is enough
# for anything a browser or a wrapper produces; the rest is a guard against a
# pathological nesting turning the parser into something unbounded.
_MAX_UNWRAP_DEPTH = 4

# A Netscape cookies.txt line: domain, include-subdomains, path, secure, expiry,
# name, value. Only sniffed to give a cookie-only paste a useful error.
_NETSCAPE_LINE = re.compile(
    r"^\S+\s+(TRUE|FALSE)\s+\S+\s+(TRUE|FALSE)\s+\d+\s+\S+\s+\S+\s*$"
)
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

_COOKIE_ONLY_ERROR = (
    "That is a cookie export, and cookies are not enough. TorBox signs in with "
    "Supabase, which keeps the session in the page's local storage, not in a "
    "cookie -- so this export identifies nobody. Open torbox.app in a signed-in "
    "browser, then DevTools > Application > Local Storage > https://torbox.app, "
    "copy the value of 'sb-db-auth-token', and paste that instead."
)


class SessionImportError(ValueError):
    """The pasted payload is not a usable TorBox session."""


class ImportedSession:
    """A parsed payload, normalised into a Playwright storage state.

    ``storage_state`` is the profile file content. ``source`` is one of
    ``storage_state`` / ``local_storage_map`` / ``session`` / ``jwt``, and
    ``session`` is the Supabase session dict -- or ``{"access_token": "<jwt>"}``
    for a bare JWT, which is why it is never None. ``cookie_count`` counts the
    cookies the paste carried, which is zero for every shape but a full export.
    """

    def __init__(self, storage_state, source, session, cookie_count=0):
        self.storage_state = storage_state
        self.source = source
        self.session = session
        self.cookie_count = cookie_count

    @property
    def has_refresh_token(self):
        return bool(self.session.get("refresh_token"))

    @property
    def email(self):
        # Read from the session already in hand rather than re-parsing the
        # storage state, which holds this same dict as JSON text.
        user = self.session.get("user")
        return user.get("email") or "" if isinstance(user, dict) else ""

    @property
    def access_token(self):
        return self.session.get("access_token", "")

    @property
    def refresh_token(self):
        return self.session.get("refresh_token") or ""

    def summary(self):
        """One line for the UI and the log."""
        what = {
            "storage_state": "a full browser storage-state export",
            "local_storage_map": "the page's local storage",
            "session": "a Supabase session",
            "jwt": "a bare access token",
        }.get(self.source, "a session")
        refresh = "will keep refreshing itself" if self.has_refresh_token else \
                   "has no refresh token, so it expires in about an hour"
        return f"Imported {what} ({self.cookie_count} cookie(s)); {refresh}."


# --- parsing ------------------------------------------------------------


def _unwrap(text):
    """JSON-decode ``text`` repeatedly, so a double-encoded export still lands
    on the object inside. Returns the value, or ``text`` itself if it is not
    JSON at all."""
    value = text
    for _ in range(_MAX_UNWRAP_DEPTH):
        if not isinstance(value, str):
            break
        stripped = value.strip()
        if not stripped or stripped[0] not in "{[\"0123456789-t":
            break
        try:
            value = json.loads(stripped)
        except ValueError:
            break
    return value


def _as_session_dict(value):
    """Return ``value`` if it looks like a Supabase session, else None."""
    return value if isinstance(value, dict) and value.get("access_token") else None


def _maybe_session(value):
    """``_as_session_dict`` on a value that may still be a JSON string.

    Exports disagree about whether a nested session is an object or the text of
    one, so every place that reaches into a nested value has to try both.
    """
    return _as_session_dict(_unwrap(value) if isinstance(value, str) else value)


def _session_from_local_storage(entries):
    """Pull the session out of a ``[{name, value}, ...]`` localStorage list."""
    if not isinstance(entries, list):
        return None
    for item in entries:
        if isinstance(item, dict):
            session = _maybe_session(item.get("value"))
            if session:
                return session
    return None


def _find_deep(value, depth=0):
    """Search a decoded payload for a session dict, wherever it is nested.

    Trace files, extension exports and DevTools snippets all bury the session a
    few levels down; a bounded search beats enumerating every wrapper shape.
    The named keys are tried first so a wrapper's own ``session`` wins over some
    unrelated field that happens to hold an access token.
    """
    session = _as_session_dict(value)
    if session:
        return session
    if depth >= _MAX_UNWRAP_DEPTH:
        return None
    if isinstance(value, dict):
        for key in ("session", "state", "storageState", "storage_state", "data", "value"):
            if key in value:
                found = _find_deep(_maybe_session(value[key]) or _unwrap(value[key]),
                                   depth + 1)
                if found:
                    return found
        for item in value.values():
            found = _find_deep(_unwrap(item), depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_deep(_unwrap(item), depth + 1)
            if found:
                return found
    return None


def _origin_with_session(state, session):
    """Return the storage state with ``session`` written under both keys.

    An imported state may carry none of the TorBox localStorage, or carry it
    under only one of the two names. Both are normalised here so
    ``TokenManager._persist`` finds a key to overwrite and the browser backend
    finds what it expects.

    Unrecognised fields in the source session -- ``user`` above all, which is
    where the account's email address lives -- are carried across, because
    dropping them would leave the profile list nameless.
    """
    state.setdefault("cookies", [])
    payload = {k: v for k, v in session.items() if v is not None}
    payload["access_token"] = session.get("access_token")
    payload["token_type"] = session.get("token_type") or "bearer"
    if not session.get("refresh_token"):
        payload.pop("refresh_token", None)
    if not session.get("expires_at"):
        payload.pop("expires_at", None)
    return write_session(state, payload)


def _looks_like_cookies(value):
    """True for a cookie list: what every cookie export's JSON array looks like.

    Only the array case is tested for. A dict carrying a ``cookies`` list is
    caught earlier, by the storage-state branch, which has a better error for it.
    """
    return any(isinstance(item, dict) and "name" in item and "domain" in item
               for item in value)


def parse_session_payload(text):
    """Turn a pasted payload into a Playwright storage state.

    Raises ``SessionImportError`` with an actionable message for anything that
    is not a usable TorBox session.
    """
    # utf-8-sig is what Windows editors and PowerShell's Out-File produce, and
    # str.strip() does not remove U+FEFF -- without this a perfectly good
    # saved file fails to parse and looks like the user's mistake.
    text = (text or "").strip().lstrip("\ufeff").strip()
    if not text:
        raise SessionImportError("Paste the session JSON first.")

    # A cookies.txt body is not JSON at all, and saying "that is not JSON" when
    # the user exported cookies is the least helpful possible answer.
    first = next((line for line in text.splitlines() if line.strip()
                  and not line.startswith("#")), "")
    if first and _NETSCAPE_LINE.match(first.strip()):
        raise SessionImportError(_COOKIE_ONLY_ERROR)

    value = _unwrap(text)

    if isinstance(value, str):
        stripped = value.strip()
        if _JWT_RE.match(stripped):
            # Usable, but there is no refresh token anywhere in a bare JWT.
            state = _origin_with_session(
                session_to_storage_state(stripped) or {"cookies": [], "origins": []},
                {"access_token": stripped},
            )
            return ImportedSession(state, "jwt", {"access_token": stripped})
        raise SessionImportError(
            "That is not a TorBox session. Expected the JSON value of "
            "'sb-db-auth-token' from torbox.app, or a Playwright storage-state "
            "export."
        )

    if isinstance(value, list):
        if _looks_like_cookies(value):
            raise SessionImportError(_COOKIE_ONLY_ERROR)
        raise SessionImportError("That JSON is a list, but not a list of cookies "
                                 "or a session.")
    if not isinstance(value, dict):
        raise SessionImportError("That JSON is not an object.")

    # A localStorage map: {"sb-db-auth-token": "..."} or a single-origin state.
    for key in SESSION_STORAGE_KEYS:
        if key in value:
            raw = value[key]
            session = _maybe_session(raw)
            if not session:
                raise SessionImportError(
                    f"'{key}' does not contain a Supabase session -- it has no "
                    "'access_token'."
                )
            state = {"cookies": [], "origins": [{"origin": ORIGIN, "localStorage": []}]}
            return ImportedSession(_origin_with_session(state, session),
                                   "local_storage_map", session)

    # A Supabase session on its own -- the common case, and the one that
    # auto-refreshes.
    session = _as_session_dict(value)
    if session:
        state = session_to_storage_state(json.dumps(session)) or {"cookies": [], "origins": []}
        return ImportedSession(_origin_with_session(state, session), "session", session)

    # A full Playwright storage state, cookies included.
    if isinstance(value.get("cookies"), list) or isinstance(value.get("origins"), list):
        state = {
            "cookies": [c for c in value.get("cookies") or [] if isinstance(c, dict)],
            "origins": [o for o in value.get("origins") or [] if isinstance(o, dict)],
        }
        session = None
        for origin in state["origins"]:
            session = _session_from_local_storage(origin.get("localStorage"))
            if session:
                break
        if not session:
            raise SessionImportError(_COOKIE_ONLY_ERROR)
        return ImportedSession(_origin_with_session(state, session), "storage_state",
                               session, len(state["cookies"]))

    # Last resort: a wrapper we do not know, with a session somewhere inside.
    session = _find_deep(value)
    if session:
        return ImportedSession(_origin_with_session({"cookies": [], "origins": []}, session),
                               "session", session)

    raise SessionImportError(
        "No TorBox session found in that JSON. Look for a 'refresh_token' or "
        "'access_token' field."
    )


# --- verification -------------------------------------------------------


def verify_session(imported, http=None):
    """Check the session against GoTrue, refreshing it if it has expired.

    Returns ``(imported, note)`` where ``note`` is a human-readable remark
    about anything that was not a clean pass. A rejected token raises
    ``SessionImportError`` -- that is definitive, and saving it would only
    produce an account that fails later. A network failure is not: the payload
    is structurally sound, so it is saved and the uncertainty is reported.
    """
    from torbox_downloader.api_session import (
        ApiError,
        SessionExpiredError,
        TokenManager,
    )

    # No profile store on purpose: _persist is then a no-op, so a refresh that
    # rotates the refresh token cannot be written somewhere this import will
    # not save. The rotated pair is put back by hand below.
    manager = TokenManager(http=http)
    try:
        manager.set_token(imported.session)
        manager.access_token()
    except SessionExpiredError as e:
        # GoTrue answered, and the answer was no. That is a verdict on the
        # payload, and it must be caught before ApiError, which it subclasses:
        # saving it would only produce an account that breaks on first use.
        message = str(e)
        if not imported.has_refresh_token:
            message += (" This payload carries no refresh token, so even a valid "
                        "one expires in about an hour.")
        raise SessionImportError(f"TorBox rejected that session. {message}")
    except ApiError as e:
        # A transport problem, not a verdict on the token.
        logger.warning("Could not verify the imported session: %s", e)
        return imported, f"Could not reach TorBox to check it ({e}); saved unverified."
    except Exception as e:
        # GoTrue answered with something this code does not model -- a body that
        # is not JSON, an expires_in that is not a number. That is our problem,
        # not the user's paste, so it must not be reported as a rejection: doing
        # so would refuse a good paste on a false verdict.
        logger.exception("Unexpected failure verifying an imported session")
        return imported, f"Could not check it with TorBox ({e}); saved unverified."

    rotated = manager.session()
    # GoTrue answers a refresh with a bare token pair, so anything else the
    # export carried -- `user` above all, which is the only place the address
    # lives -- would be dropped. Merging the other way keeps it.
    rotated = {**imported.session, **rotated}
    if rotated.get("access_token") != imported.access_token:
        logger.info("Refreshed the imported session before saving it.")
    verified = ImportedSession(_origin_with_session(imported.storage_state, rotated),
                               imported.source, rotated, imported.cookie_count)
    return verified, ""


# --- profile import -----------------------------------------------------


def import_session(store, name, text, http=None, verify=True):
    """Parse, verify and save ``text`` as the profile ``name``.

    Returns a summary dict for the UI. Raises ``ProfileError`` for a bad or
    taken name, and ``SessionImportError`` for a payload that cannot be used.
    """
    if not valid_name(name):
        raise ProfileError("Profile names may only contain letters, digits, '.', "
                           "'_' and '-'.")
    if store.exists(name):
        raise ProfileError(f"A profile named '{name}' already exists.")

    imported = parse_session_payload(text)
    note = ""
    if verify:
        imported, note = verify_session(imported, http=http)

    store.add(name)
    if not store.save_state(name, imported.storage_state):
        store.remove(name)
        raise ProfileError(f"Could not write the session for '{name}'.")

    summary = {
        "name": name,
        "email": imported.email,
        "has_refresh_token": imported.has_refresh_token,
        "cookie_count": imported.cookie_count,
        "source": imported.source,
        "message": imported.summary() + (f" {note}" if note else ""),
    }
    logger.info("Imported a session into profile '%s': %s", name, summary["message"])
    return summary
