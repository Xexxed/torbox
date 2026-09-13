"""Virtual drive orchestration.

Chooses the best available backend (native WinFsp drive, falling back to a
localhost WebDAV share mapped via ``net use``), owns the background threads
and teardown paths, and exposes a small status snapshot for the GUI API.
"""

import ctypes
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from http import client as http_client

logger = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_MOUNTING = "mounting"
STATUS_MOUNTED = "mounted"
STATUS_UNMOUNTING = "unmounting"
STATUS_ERROR = "error"

_WINFSP_START_TIMEOUT_S = 20.0


def pick_free_drive_letter(preferred="", used=None):
    """Pick a free drive letter like 'T:'.

    preferred: single-letter preference ('' = none).
    used:      explicit set of used letters (for tests); when omitted the
               real Windows drive bitmap is queried.
    """
    pref = (preferred or "").strip().upper().rstrip(":")
    if used is None:
        if sys.platform != "win32":
            return None
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        except Exception:
            bitmask = 0
        used = {chr(65 + index) for index in range(26) if bitmask & (1 << index)}
    order = []
    if len(pref) == 1 and pref.isalpha():
        order.append(pref)
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        if letter not in order:
            order.append(letter)
    for letter in order:
        if letter not in used:
            return letter + ":"
    return None


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DriveMountManager:
    """State machine owning the lifetime of one mounted virtual drive."""

    def __init__(self):
        self._lock = threading.RLock()
        self._status = STATUS_IDLE
        self._backend = None          # "winfsp" | "webdav"
        self._drive_letter = None     # e.g. "T:"
        self._webdav_url = ""
        self._error = ""
        self._hint = ""
        self._vfs = None
        self._winfsp_handle = None
        self._winfsp_thread = None
        self._dav_server = None
        self._dav_thread = None
        self._net_use_letter = None
        self._stop_event = threading.Event()
        self._preferred_letter = ""

    # --- configuration -------------------------------------------------------

    def set_preferred_letter(self, letter):
        """Persisted preference; '' means auto-pick."""
        with self._lock:
            self._preferred_letter = (letter or "").strip().upper().rstrip(":")

    # --- status ---------------------------------------------------------------

    @property
    def status(self):
        with self._lock:
            return self._status

    def status_snapshot(self):
        with self._lock:
            return {
                "status": self._status,
                "backend": self._backend,
                "drive_letter": self._drive_letter,
                "path": f"{self._drive_letter}\\" if self._drive_letter else "",
                "url": self._webdav_url,
                "error": self._error,
                "hint": self._hint,
            }

    # --- mount ------------------------------------------------------------------

    def mount_async(self, app_state):
        with self._lock:
            if self._status in (STATUS_MOUNTING, STATUS_MOUNTED, STATUS_UNMOUNTING):
                return False
            self._status = STATUS_MOUNTING
            self._error = ""
            self._hint = ""
        threading.Thread(
            target=self._mount_worker,
            args=(app_state,),
            daemon=True,
            name="TorboxMountThread",
        ).start()
        return True

    def _log(self, app_state, message):
        if app_state is None:
            logger.info(message)
            return
        try:
            app_state.add_log(message)
        except Exception:
            logger.info(message)

    def _mount_worker(self, app_state):
        letter = None
        try:
            if not getattr(app_state, "token", None):
                raise RuntimeError("Save a session token before mounting the drive.")

            # Borrow the shared VFS (owned by AppState) so the drive and the
            # Stream page reuse one tree and one CDN-link cache. Unmounting
            # clears only this manager's reference, never the shared instance.
            vfs = app_state.get_vfs()
            torrents = vfs.scan() or []
            try:
                app_state.update_queue(torrents)
            except Exception:
                pass
            self._log(app_state, f"[{time.strftime('%H:%M:%S')}] INFO: Virtual drive scan found {len(torrents)} cached torrents.")

            letter = pick_free_drive_letter(self._preferred_letter)
            if not letter:
                raise RuntimeError("No free drive letter is available.")

            winfsp_error = None
            backend = None
            try:
                self._start_winfsp(vfs, letter)
                backend = "winfsp"
            except Exception as exc:
                winfsp_error = exc
                logger.info(f"WinFsp mount unavailable ({exc}); trying WebDAV fallback...")
                self._teardown_winfsp()
            if backend is None:
                self._start_webdav(vfs, letter)
                backend = "webdav"

            # A concurrent unmount may have flipped our status meanwhile.
            proceed = False
            with self._lock:
                if self._status == STATUS_MOUNTING:
                    self._backend = backend
                    self._drive_letter = letter
                    self._vfs = vfs
                    self._status = STATUS_MOUNTED
                    if backend == "webdav":
                        from torbox_downloader.mount.winfsp_fs import HAVE_WINFSPY

                        self._hint = (
                            ""
                            if HAVE_WINFSPY
                            else "Using WebDAV mapping. Install WinFsp (https://winfsp.dev/rel/) "
                                 "and 'pip install winfspy' for a faster native drive."
                        )
                    proceed = True
            if proceed:
                self._log(app_state, f"[{time.strftime('%H:%M:%S')}] INFO: TorBox mounted as {letter} via {backend}. Open it straight from Explorer.")
            else:
                self._teardown_net_use()
                self._teardown_winfsp()
                self._teardown_dav_server()
        except Exception as exc:
            self._teardown_net_use()
            self._teardown_winfsp()
            self._teardown_dav_server()
            aborted = False
            with self._lock:
                if self._status == STATUS_MOUNTING:
                    self._status = STATUS_ERROR
                    self._error = str(exc)
                    aborted = True
            if aborted:
                self._log(app_state, f"[{time.strftime('%H:%M:%S')}] ERROR: Drive mount failed: {exc}")

    def _start_winfsp(self, vfs, letter):
        from torbox_downloader.mount.winfsp_fs import HAVE_WINFSPY, WinfspMountHandle

        if not HAVE_WINFSPY:
            raise RuntimeError("winfspy package is not installed")

        handle = WinfspMountHandle(vfs, letter)
        started = threading.Event()
        failures = []

        def run():
            try:
                handle.start()
            except BaseException as exc:  # surfaced below
                failures.append(exc)
                return
            started.set()
            self._stop_event.wait()
            handle.stop()

        self._stop_event.clear()
        with self._lock:
            self._winfsp_handle = handle
        thread = threading.Thread(target=run, daemon=True, name="TorboxWinFspThread")
        with self._lock:
            self._winfsp_thread = thread
        thread.start()
        if not started.wait(_WINFSP_START_TIMEOUT_S):
            if failures:
                raise RuntimeError(f"WinFsp mount failed: {failures[0]}")
            raise RuntimeError(f"WinFsp mount timed out after {_WINFSP_START_TIMEOUT_S:.0f}s")

    def _start_webdav(self, vfs, letter):
        from torbox_downloader.mount.dav_provider import HAVE_WSGIDAV, create_dav_server

        if not HAVE_WSGIDAV:
            raise RuntimeError(
                "Neither WinFsp nor WsgiDAV is available. Install one: "
                "'pip install winfspy' (plus the WinFsp driver) or 'pip install wsgidav cheroot'."
            )

        port = find_free_port()
        server = create_dav_server(vfs, "127.0.0.1", port)
        with self._lock:
            self._dav_server = server
            self._webdav_url = f"http://127.0.0.1:{port}/"
        thread = threading.Thread(target=server.start, daemon=True, name="TorboxDavThread")
        with self._lock:
            self._dav_thread = thread
        thread.start()
        self._wait_dav_ready(port)

        url = f"http://127.0.0.1:{port}/"
        map_cmd = ["net", "use", f"{letter}:", url, "/persistent:no"]
        result = subprocess.run(map_cmd, capture_output=True, text=True, timeout=60)
        mapped = result.returncode == 0 and self._letter_exists(letter)
        if not mapped:
            # The WebClient service may be stopped; starting it needs admin.
            subprocess.run(["net", "start", "WebClient"], capture_output=True, text=True, timeout=90)
            retry = subprocess.run(map_cmd, capture_output=True, text=True, timeout=60)
            mapped = retry.returncode == 0 and self._letter_exists(letter)
            if not mapped:
                detail = ((retry.stdout or "") + (retry.stderr or "")).strip() or \
                         ((result.stdout or "") + (result.stderr or "")).strip()
                self._teardown_dav_server()
                raise RuntimeError(
                    "Could not map the WebDAV drive via 'net use': "
                    f"{detail[:300]} | If this mentions the WebClient service, run once as "
                    "Administrator: 'sc config WebClient start= demand' then 'net start WebClient'. "
                    "Installing WinFsp avoids this entirely."
                )
        with self._lock:
            self._net_use_letter = letter
        self._webdav_url = url

    @staticmethod
    def _wait_dav_ready(port, timeout=15.0):
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                conn = http_client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("OPTIONS", "/")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status < 500:
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
        raise RuntimeError(f"Local WebDAV server did not become ready: {last_error}")

    @staticmethod
    def _letter_exists(letter):
        return os.path.exists(f"{letter}:\\")

    # --- unmount ------------------------------------------------------------

    def unmount_async(self, app_state=None):
        with self._lock:
            if self._status not in (STATUS_MOUNTED, STATUS_ERROR):
                return False
            self._status = STATUS_UNMOUNTING
        threading.Thread(
            target=self._unmount_worker,
            args=(app_state,),
            daemon=True,
            name="TorboxUnmountThread",
        ).start()
        return True

    def _unmount_worker(self, app_state):
        letter = None
        try:
            self._teardown_net_use()
            self._teardown_winfsp()
            self._teardown_dav_server()
        finally:
            with self._lock:
                letter = self._drive_letter
                self._vfs = None
                self._drive_letter = None
                self._webdav_url = ""
                self._error = ""
                self._hint = ""
                self._status = STATUS_IDLE
        self._log(app_state, f"[{time.strftime('%H:%M:%S')}] INFO: Virtual drive {letter or ''} unmounted.")

    def unmount_sync(self):
        """Blocking unmount used at app exit; safe from any state."""
        self._teardown_net_use()
        self._teardown_winfsp()
        self._teardown_dav_server()
        with self._lock:
            self._vfs = None
            self._drive_letter = None
            self._webdav_url = ""
            self._status = STATUS_IDLE

    # --- teardown pieces ------------------------------------------------------

    def _teardown_net_use(self):
        with self._lock:
            letter = self._net_use_letter
            self._net_use_letter = None
        if letter:
            try:
                subprocess.run(
                    ["net", "use", f"{letter}:", "/delete", "/y"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception as exc:
                logger.warning(f"net use {letter}: /delete failed: {exc}")

    def _teardown_winfsp(self):
        with self._lock:
            thread = self._winfsp_thread
            self._winfsp_handle = None
            self._winfsp_thread = None
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)

    def _teardown_dav_server(self):
        with self._lock:
            server = self._dav_server
            self._dav_server = None
            self._dav_thread = None
        if server is not None:
            try:
                server.stop()
            except Exception as exc:
                logger.warning(f"WebDAV server stop failed: {exc}")
