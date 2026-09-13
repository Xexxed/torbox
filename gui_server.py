import os
import sys
import json
import time
import shutil
import threading
import webbrowser
import uuid
import logging
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

# Size of each block written to the client by the Stream content proxy.
STREAM_BLOCK_BYTES = 1024 * 1024


def _fmt_speed(bps: float) -> str:
    """Format bytes-per-second as a human-readable string. Centralised here to
    avoid copy-pasting the same if/elif chain in multiple AppState methods."""
    if bps > 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    if bps > 1024:
        return f"{bps / 1024:.1f} KB/s"
    if bps > 0:
        return f"{int(bps)} B/s"
    return "0 KB/s"


def _clean_vfs_path(raw):
    """Validate and normalize a client-supplied virtual-filesystem path.

    Returns a '/'-rooted path. Raises ValueError for anything that is not a
    plain absolute path (empty, NUL bytes, or traversal segments).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A non-empty 'path' is required.")
    if "\x00" in raw:
        raise ValueError("Invalid path.")
    segments = [s for s in raw.strip().replace("\\", "/").split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise ValueError("Path traversal is not allowed.")
    return "/" + "/".join(segments)


# In-memory cache for static GUI files. Files never change at runtime so we
# read each one once and serve from RAM on every subsequent request.
_static_cache: dict = {}


def extract_email_from_token(token):
    """
    Decodes the user's email from the JWT payload in localStorage token
    without network calls.
    """
    if not token:
        return ""
    try:
        access_token = token.strip()
        if access_token.startswith('{') and access_token.endswith('}'):
            try:
                data = json.loads(access_token)
                access_token = data.get("access_token", access_token)
            except Exception:
                pass
        
        parts = access_token.split('.')
        if len(parts) == 3:
            import base64
            payload_b64 = parts[1]
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            payload = json.loads(payload_bytes.decode('utf-8'))
            return payload.get('email', '')
    except Exception:
        pass
    return ""

def send_os_notification(title, message):
    """
    Sends a non-blocking Windows OS balloon alert using native PowerShell.
    """
    if sys.platform != "win32":
        return
    ps_script = f"""
    [void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
    $toast = New-Object System.Windows.Forms.NotifyIcon
    $toast.Icon = [System.Drawing.SystemIcons]::Information
    $toast.BalloonTipTitle = '{title.replace("'", "''")}'
    $toast.BalloonTipText = '{message.replace("'", "''")}'
    $toast.Visible = $true
    $toast.ShowBalloonTip(10000)
    """
    try:
        import subprocess
        subprocess.Popen(["powershell", "-Command", ps_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# Global App State
class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        # One lock guards every browser-mutating operation (structure scrapes
        # and link generation). It is held per operation, never for a whole
        # download, so the Stream page and the download pipeline cannot corrupt
        # each other's SPA state while still interleaving between files.
        # `fetch_lock` is kept as an alias for existing call sites.
        self.browser_op_lock = threading.Lock()
        self.fetch_lock = self.browser_op_lock
        self.browser_op_lock_timeout_s = 60.0
        self.token = ""
        self.email = ""
        self.dest_dir = "downloads"
        self.delay = 5
        self.browser_type = "chromium"
        self.headless = True
        self.max_parallel_files = 1
        self.parallel_connections = True
        self.disk_profile = "auto"   # auto | hdd | ssd
        self.write_buffer_mb = 0     # 0 = pick from disk profile
        self.watch_mode = False
        self.watch_interval = 60
        
        self.status = "idle"  # idle, scanning, downloading, finished, error
        self.error_message = ""
        self.queue = []       # list of file dicts: {name, path, size, status, progress, speed}
        self.logs = []        # list of console log lines
        self.abort_flag = False
        self.cancel_event = threading.Event()
        self.active_downloads = {}  # dict: {filename: {downloaded, total, speed, status, progress}}
        self.file_to_torrent_map = {}  # dict: {filename: {torrent_name, relative_path}}
        
        self.session = None
        self.session_lock = threading.Lock()
        self.api_key = str(uuid.uuid4())
        # Shared virtual filesystem (TorboxVfs). Owned here so the drive mount
        # and the Stream page reuse one tree and one CDN-link cache instead of
        # each building their own. Lazily created by get_vfs().
        self._vfs = None
        self._vfs_lock = threading.RLock()
        # O(1) name → queue-index mapping; rebuilt whenever update_queue() runs.
        self._queue_index: dict = {}
        # TTL cache for shutil.disk_usage() to avoid a filesystem syscall on
        # every /api/status poll (which fires every ~500 ms from the frontend).
        self._disk_cache: dict = {"free_space": "Unknown", "expires": 0.0}

        # Virtual drive (RaiDrive-style mount); "" = auto-pick a free letter.
        from torbox_downloader.mount.manager import DriveMountManager
        self.mount_manager = DriveMountManager()
        self.mount_drive_letter = ""

        # Load default token if present
        if os.path.exists("token.txt"):
            try:
                with open("token.txt", "r", encoding="utf-8") as f:
                    self.token = f.read().strip()
                self.email = extract_email_from_token(self.token)
            except Exception:
                pass
                
        # Load or create persistent settings
        if not os.path.exists("settings.json"):
            self.save_settings()
        else:
            self.load_settings()
        self.mount_manager.set_preferred_letter(self.mount_drive_letter)

    def load_settings(self):
        if os.path.exists("settings.json"):
            try:
                with open("settings.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "dest_dir" in data:
                        self.dest_dir = data["dest_dir"]
                    if "delay" in data:
                        self.delay = data["delay"]
                    if "max_parallel_files" in data:
                        self.max_parallel_files = int(data["max_parallel_files"])
                    if "parallel_connections" in data:
                        self.parallel_connections = bool(data["parallel_connections"])
                    if "disk_profile" in data and data["disk_profile"] in ("auto", "hdd", "ssd"):
                        self.disk_profile = data["disk_profile"]
                    if "write_buffer_mb" in data:
                        self.write_buffer_mb = float(data["write_buffer_mb"])
                    if "watch_mode" in data:
                        self.watch_mode = bool(data["watch_mode"])
                    if "watch_interval" in data:
                        self.watch_interval = int(data["watch_interval"])
                    if "mount_drive_letter" in data:
                        letter = str(data["mount_drive_letter"]).strip().upper().rstrip(":")
                        self.mount_drive_letter = letter if len(letter) == 1 and letter.isalpha() else ""
                    if "browser_op_lock_timeout_s" in data:
                        try:
                            self.browser_op_lock_timeout_s = max(1.0, float(data["browser_op_lock_timeout_s"]))
                        except (TypeError, ValueError):
                            pass
            except Exception:
                pass

    def save_settings(self):
        try:
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump({
                    "dest_dir": self.dest_dir,
                    "delay": self.delay,
                    "max_parallel_files": self.max_parallel_files,
                    "parallel_connections": self.parallel_connections,
                    "disk_profile": self.disk_profile,
                    "write_buffer_mb": self.write_buffer_mb,
                    "watch_mode": self.watch_mode,
                    "watch_interval": self.watch_interval,
                    "mount_drive_letter": self.mount_drive_letter,
                    "browser_op_lock_timeout_s": self.browser_op_lock_timeout_s
                }, f)
        except Exception:
            pass

    def handle_token_expired(self):
        self.abort_flag = True
        self.cancel_event.set()
        with self.lock:
            self.token = ""
            self.email = ""
            self.queue = []
            self.status = "error"
            self.error_message = "Your Torbox session token has expired or is invalid."
        
        self.stop_session()
        self.drop_vfs()

        if os.path.exists("token.txt"):
            try:
                os.remove("token.txt")
            except Exception:
                pass
        self.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Session token has expired. Gracefully logged out.")

    def get_or_create_session(self, token=None, browser_type=None, headless=None):
        with self.session_lock:
            if not self.session:
                tok = token or self.token
                if tok:
                    from torbox_downloader.browser_session import TorboxBrowserSession
                    b_type = browser_type or self.browser_type
                    h_less = headless if headless is not None else self.headless
                    self.session = TorboxBrowserSession(tok, b_type, h_less)
                    self.session.start()
            return self.session

    def stop_session(self):
        with self.session_lock:
            if self.session:
                try:
                    self.session.stop()
                except Exception:
                    pass
                self.session = None

    def get_vfs(self):
        """Return the shared TorboxVfs for the current session, creating it on
        first use.

        A single instance is shared by the drive mount and the Stream page so
        both reuse the same tree and the same CDN-link cache. It is rebuilt
        automatically when the underlying browser session was replaced (token
        change / re-login), which keeps a stale tree from outliving its session.
        """
        with self._vfs_lock:
            session = self.get_or_create_session()
            if session is None:
                raise RuntimeError("No valid session token provided.")
            current = self._vfs
            if current is None or getattr(current, "_session", None) is not session:
                from torbox_downloader.mount.vfs import TorboxVfs
                current = TorboxVfs(
                    session,
                    browser_lock=self.browser_op_lock,
                    browser_op_timeout=self.browser_op_lock_timeout_s,
                )
                self._vfs = current
            return current

    def drop_vfs(self):
        """Forget the shared VFS. Called when the session ends (logout/token
        expiry) so a stale tree cannot be served to a new session."""
        with self._vfs_lock:
            vfs = self._vfs
            self._vfs = None
        if vfs is not None:
            try:
                vfs.close_all_readers()
            except Exception:
                pass

    def clear_logs(self):
        with self.lock:
            self.logs = []

    def add_log(self, message):
        with self.lock:
            self.logs.append(message)
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def set_status(self, status, error=""):
        with self.lock:
            self.status = status
            if error:
                self.error_message = error

    def update_queue(self, torrents):
        with self.lock:
            self.queue = []
            self._queue_index = {}
            for t in torrents:
                entry = {
                    "name": t["name"],
                    "path": "",
                    "size": t["size"],
                    "status": "queued",
                    "progress": 0,
                    "speed": "0 KB/s"
                }
                self._queue_index[t["name"]] = len(self.queue)
                self.queue.append(entry)

    def update_file_progress(self, filename, status, downloaded, total, speed_bytes):
        with self.lock:
            idx = self._queue_index.get(filename)
            if idx is not None:
                item = self.queue[idx]
                item["status"] = status
                item["progress"] = int((downloaded / total) * 100) if total > 0 else 0
                item["speed"] = _fmt_speed(speed_bytes)

    def update_active_download(self, filename, status, downloaded, total, speed_bytes):
        with self.lock:
            if status in ['completed_file', 'failed_file']:
                self.active_downloads[filename] = {
                    "downloaded": downloaded,
                    "total": total,
                    "speed": "0 KB/s",
                    "status": "completed" if status == 'completed_file' else "failed",
                    "progress": 100 if status == 'completed_file' else 0
                }
            else:
                self.active_downloads[filename] = {
                    "downloaded": downloaded,
                    "total": total,
                    "speed": _fmt_speed(speed_bytes),
                    "status": "downloading",
                    "progress": int((downloaded / total) * 100) if total > 0 else 0
                }

    def get_serializable_state(self):
        with self.lock:
            token_set = bool(self.token)
            email = self.email
            dest_dir = self.dest_dir
            delay = self.delay
            status = self.status
            error_message = self.error_message
            queue_list = list(self.queue)
            logs_list = list(self.logs)
            active_downloads = dict(self.active_downloads)
            max_parallel_files = self.max_parallel_files
            parallel_connections = self.parallel_connections
            disk_profile = self.disk_profile
            write_buffer_mb = self.write_buffer_mb
            watch_mode = self.watch_mode
            watch_interval = self.watch_interval

        # Serve free-space at most once every 5 s to avoid a filesystem syscall
        # on every /api/status poll (frontend polls every ~500 ms).
        now_ts = time.time()
        if now_ts >= self._disk_cache["expires"]:
            free_space_str = "Unknown"
            try:
                probe = dest_dir
                while probe and not os.path.exists(probe):
                    parent = os.path.dirname(probe)
                    if parent == probe:
                        break
                    probe = parent
                if not probe or not os.path.exists(probe):
                    probe = os.path.expanduser("~")
                _total, _used, free = shutil.disk_usage(probe)
                free_gb = free / (1024 ** 3)
                if free_gb > 1024:
                    free_space_str = f"{free_gb / 1024:.1f} TB"
                else:
                    free_space_str = f"{free_gb:.1f} GB"
            except Exception:
                pass
            self._disk_cache["free_space"] = free_space_str
            self._disk_cache["expires"] = now_ts + 5.0
        free_space_str = self._disk_cache["free_space"]

        return {
            "token_set": token_set,
            "email": email,
            "dest_dir": dest_dir,
            "delay": delay,
            "status": status,
            "error_message": error_message,
            "queue": queue_list,
            "logs": logs_list,
            "active_downloads": active_downloads,
            "free_space": free_space_str,
            "max_parallel_files": max_parallel_files,
            "parallel_connections": parallel_connections,
            "disk_profile": disk_profile,
            "write_buffer_mb": write_buffer_mb,
            "watch_mode": watch_mode,
            "watch_interval": watch_interval,
            "mount": self.mount_manager.status_snapshot(),
            "mount_drive_letter": self.mount_drive_letter
        }

app_state = AppState()

# Stdout & Logging Redirection
class LogRedirector:
    def __init__(self, state):
        self.state = state
        self.terminal = sys.stdout

    def write(self, text):
        self.terminal.write(text)
        if text.strip():
            clean = text.strip()
            if "\r" in text or "██" in text or "%" in text or "Connecting" in clean:
                if "Connecting to download" in clean:
                    self.state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: {clean}")
                return
            
            timestamp = f"[{time.strftime('%H:%M:%S')}]"
            level = "ERROR" if "error" in clean.lower() else "WARN" if "warning" in clean.lower() else "INFO"
            self.state.add_log(f"{timestamp} {level}: {clean}")

    def flush(self):
        self.terminal.flush()

def scan_torrents_background():
    if not app_state.token:
        return
        
    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Scanning Torbox torrents list in background...")
    
    try:
        session = app_state.get_or_create_session()
        if not session:
            return
            
        from torbox_downloader.browser_session import is_token_expired
        if is_token_expired(app_state.token):
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Session token is expired.")
            app_state.set_status("error", "Your Torbox session token has expired.")
            send_os_notification("Torbox Session Warning", "Your Torbox session token has expired.")
            return
            
        torrents = session.scan_torrents()
        if torrents:
            app_state.update_queue(torrents)
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Successfully scraped {len(torrents)} active torrents from Torbox.")
        else:
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Checked Torbox download queue: no active torrents found.")
            
        with app_state.lock:
            if app_state.status == "error":
                app_state.status = "idle"
                app_state.error_message = ""
    except Exception as e:
        if "session token has expired or is invalid" in str(e):
            app_state.handle_token_expired()
        else:
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Background scan failed: {e}")

def downloader_worker(url, dest_dir, delay, headless, browser_type, token, target_torrent_name=None, max_parallel_files=1, selected_files=None, parallel_connections=True, disk_profile="auto", write_buffer_mb=0):
    app_state.set_status("scanning")
    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Initializing Torbox Engine...")
    
    with app_state.lock:
        app_state.active_downloads.clear()

    from main import run_downloader
    
    def file_scraped_cb(files):
        if not target_torrent_name:
            app_state.update_queue(files)
        else:
            with app_state.lock:
                for f in files:
                    path_segments = f.get("path", [])
                    if path_segments:
                        app_state.file_to_torrent_map[f["name"]] = {
                            "torrent_name": path_segments[0],
                            "relative_path": "/".join(path_segments + [f["name"]])
                        }
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Found and matched torrent target.")
 
    def status_update_cb(filename, status, downloaded, total, speed):
        if app_state.abort_flag:
            raise Exception("Download task aborted by user.")
            
        if status in ['downloading_file', 'completed_file', 'failed_file']:
            app_state.update_active_download(filename, status, downloaded, total, speed)
        else:
            app_state.update_file_progress(filename, status, downloaded, total, speed)
            app_state.set_status("downloading")
   
    try:
        session = app_state.get_or_create_session(token, browser_type, headless)
            
        run_downloader(
            url=url,
            dest_dir=dest_dir,
            headless=headless,
            browser_type=browser_type,
            token=token,
            delay=delay,
            file_scraped_callback=file_scraped_cb,
            status_update_callback=status_update_cb,
            target_torrent_name=target_torrent_name,
            max_parallel_files=max_parallel_files,
            selected_files=selected_files,
            parallel_connections=parallel_connections,
            cancel_event=app_state.cancel_event,
            session=session,
            disk_profile=disk_profile,
            buffer_mb=(write_buffer_mb or None),
            browser_lock=app_state.browser_op_lock,
            browser_op_timeout=app_state.browser_op_lock_timeout_s
        )
        app_state.set_status("finished")
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: All downloads finished successfully!")
        send_os_notification("Torbox Downloader", "All downloads finished successfully!")
    except Exception as e:
        if "session token has expired or is invalid" in str(e):
            app_state.handle_token_expired()
        elif app_state.abort_flag:
            app_state.set_status("idle")
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Download task successfully aborted.")
        else:
            app_state.set_status("error", str(e))
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] ERROR: {str(e)}")
            send_os_notification("Torbox Downloader Error", f"Downloads failed: {str(e)}")

# Watch mode background thread loop
def watch_mode_loop():
    logger.info("Watch mode loop started.")
    last_run = 0
    while True:
        interval = getattr(app_state, "watch_interval", 60)
        watch_enabled = getattr(app_state, "watch_mode", False)
        
        if watch_enabled and app_state.token and app_state.status == "idle":
            now = time.time()
            if now - last_run >= interval:
                last_run = now
                app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Watch mode polling active...")
                try:
                    scan_torrents_background()
                    
                    with app_state.lock:
                        has_items = len(app_state.queue) > 0
                        is_idle = app_state.status == "idle"
                        
                    if has_items and is_idle:
                        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Watch mode: Auto-downloading {len(app_state.queue)} torrents...")
                        app_state.abort_flag = False
                        app_state.cancel_event.clear()
                        app_state.error_message = ""
                        
                        t = threading.Thread(
                            target=downloader_worker,
                            args=(
                                "https://torbox.app/download",
                                app_state.dest_dir,
                                app_state.delay,
                                app_state.headless,
                                app_state.browser_type,
                                app_state.token,
                                None,
                                app_state.max_parallel_files,
                                None,
                                app_state.parallel_connections,
                                app_state.disk_profile,
                                app_state.write_buffer_mb
                            ),
                            daemon=True
                        )
                        t.start()
                except Exception as e:
                    logger.error(f"Error in watch mode poll: {e}")
        time.sleep(2)

# API & Static Server Handlers
class GUIRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _stream_error(self, exc):
        """Map a Stream exception to a JSON error body. A stale token logs the
        session out once instead of surfacing an error per card."""
        message = str(exc)
        if "session token has expired or is invalid" in message:
            app_state.handle_token_expired()
            message = "Your Torbox session token has expired or is invalid."
        return {"success": False, "error": message}

    def _send_text(self, status, message):
        body = str(message).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    @staticmethod
    def _parse_range(range_header, size):
        """Parse a single-range header. Returns (start, end) or None."""
        match = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", range_header or "")
        if not match:
            return None
        start_txt, end_txt = match.group(1), match.group(2)
        if not start_txt and not end_txt:
            return None
        if start_txt:
            start = int(start_txt)
            end = int(end_txt) if end_txt else size - 1
        else:
            suffix = int(end_txt)          # "bytes=-N" = last N bytes
            if suffix <= 0:
                return None
            start, end = max(0, size - suffix), size - 1
        if start > end or start >= size:
            return None
        return start, min(end, size - 1)

    def _handle_stream_content(self, head_only=False):
        """Serve one file through the shared ChunkedRemoteReader.

        Range requests are honored so the player can seek. The reader resolves
        the CDN link lazily on its first read -- which only happens because the
        user clicked the file, so listing a folder never resolves anything.
        """
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        raw_path = query.get("path", [""])[0] or ""
        download = (query.get("download", ["0"])[0] or "0").lower() in ("1", "true", "yes")

        try:
            path = _clean_vfs_path(raw_path)
        except ValueError as exc:
            self._send_text(400, str(exc))
            return

        try:
            vfs = app_state.get_vfs()
            st = vfs.stat(path)
        except Exception as exc:
            if "session token has expired or is invalid" in str(exc):
                app_state.handle_token_expired()
            self._send_text(404, "File not found.")
            return

        if st is None:
            self._send_text(404, "File not found.")
            return
        if st.get("is_dir"):
            self._send_text(400, "Requested path is a directory, not a file.")
            return

        size = int(st.get("size") or 0)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"

        start, end, partial = 0, max(0, size - 1), False
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            parsed = self._parse_range(range_header, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            partial = True

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes" if size > 0 else "none")
        self.send_header("Cache-Control", "no-store")
        if download:
            filename = path.rsplit("/", 1)[-1].replace('"', "")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if size > 0:
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        try:
            reader = vfs.get_or_open_reader(path)
        except Exception as exc:
            logger.warning(f"Stream open failed for {path!r}: {exc}")
            return

        remaining = (end - start + 1) if size > 0 else None
        offset = start
        try:
            while remaining is None or remaining > 0:
                want = STREAM_BLOCK_BYTES if remaining is None else min(STREAM_BLOCK_BYTES, remaining)
                chunk = reader.read_range(offset, want)
                if not chunk:
                    break
                self.wfile.write(chunk)
                offset += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass            # client seeked away or closed the player
        except Exception as exc:
            logger.warning(f"Stream read failed for {path!r}: {exc}")

    def verify_api_key(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        key_in_query = query.get("api_key", [None])[0]
        key_in_header = self.headers.get("X-API-Key")
        
        expected = getattr(app_state, "api_key", None)
        if not expected:
            return True
            
        return key_in_query == expected or key_in_header == expected

    def do_GET(self):
        if self.path.startswith("/api/"):
            if not self.verify_api_key():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Unauthorized API request.")
                return

        if self.path.startswith("/api/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(app_state.get_serializable_state()).encode("utf-8"))
            return

        if self.path.startswith("/api/stream/library"):
            # Root torrent listing for the Stream tab. Metadata only: no links.
            try:
                entries = app_state.get_vfs().listdir_entries("/")
            except Exception as e:
                self._send_json(self._stream_error(e))
                return
            self._send_json({"success": True, "path": "/", "entries": entries})
            return

        if self.path.startswith("/api/stream/content"):
            self._handle_stream_content()
            return

        # Serve static UI files from /gui subfolder
        target_path = self.path
        if target_path == "/" or target_path.startswith("/?"):
            target_path = "/index.html"

        # Sanitize path to prevent directory traversal
        sanitized_path = os.path.normpath(target_path.split("?")[0].lstrip("/"))
        file_path = os.path.join(os.path.dirname(__file__), "gui", sanitized_path)

        if os.path.exists(file_path) and os.path.isfile(file_path):
            if file_path not in _static_cache:
                with open(file_path, "rb") as f:
                    _static_cache[file_path] = f.read()
            content = _static_cache[file_path]
            self.send_response(200)
            if file_path.endswith(".html"):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif file_path.endswith(".css"):
                self.send_header("Content-Type", "text/css; charset=utf-8")
            elif file_path.endswith(".js"):
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def do_HEAD(self):
        # Players probe length/type before requesting the body.
        if self.path.startswith("/api/") and not self.verify_api_key():
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/api/stream/content"):
            self._handle_stream_content(head_only=True)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/"):
            if not self.verify_api_key():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"Unauthorized API request.")
                return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8")
        
        try:
            params = json.loads(post_data) if post_data else {}
        except Exception:
            params = {}

        if self.path.startswith("/api/settings"):
            token_updated = False
            dest_err = None

            if "dest_dir" in params:
                new_dest = params["dest_dir"].strip()
                if new_dest:
                    try:
                        os.makedirs(new_dest, exist_ok=True)
                        test_file = os.path.join(new_dest, f".write_test_{uuid.uuid4().hex}")
                        with open(test_file, "w") as f:
                            f.write("test")
                        os.remove(test_file)
                    except Exception as e:
                        dest_err = f"Invalid or non-writable download directory '{new_dest}': {e}"

            if dest_err:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": dest_err}).encode("utf-8"))
                return

            with app_state.lock:
                if "token" in params:
                    new_token = params["token"].strip()
                    if new_token and new_token != '••••••••••••••••••••' and new_token != app_state.token:
                        app_state.token = new_token
                        app_state.email = extract_email_from_token(new_token)
                        token_updated = True
                        with open("token.txt", "w", encoding="utf-8") as f:
                            f.write(app_state.token)
                if "dest_dir" in params and not dest_err:
                    app_state.dest_dir = params["dest_dir"].strip()
                if "delay" in params:
                    app_state.delay = int(params["delay"])
                if "max_parallel_files" in params:
                    app_state.max_parallel_files = int(params["max_parallel_files"])
                if "parallel_connections" in params:
                    app_state.parallel_connections = bool(params["parallel_connections"])
                if "disk_profile" in params and params["disk_profile"] in ("auto", "hdd", "ssd"):
                    app_state.disk_profile = params["disk_profile"]
                if "write_buffer_mb" in params:
                    try:
                        app_state.write_buffer_mb = max(0.0, float(params["write_buffer_mb"]))
                    except (TypeError, ValueError):
                        pass
                if "watch_mode" in params:
                    app_state.watch_mode = bool(params["watch_mode"])
                if "watch_interval" in params:
                    app_state.watch_interval = int(params["watch_interval"])
                if "mount_drive_letter" in params:
                    letter = str(params["mount_drive_letter"]).strip().upper().rstrip(":")
                    if letter == "AUTO" or letter == "":
                        app_state.mount_drive_letter = ""
                    elif len(letter) == 1 and letter.isalpha():
                        app_state.mount_drive_letter = letter
                    app_state.mount_manager.set_preferred_letter(app_state.mount_drive_letter)
                app_state.save_settings()
            
            if token_updated:
                try:
                    session = app_state.get_or_create_session()
                    if session:
                        session.update_token(app_state.token)
                    t = threading.Thread(
                        target=scan_torrents_background,
                        daemon=True
                    )
                    t.start()
                except Exception as e:
                    if "session token has expired or is invalid" in str(e):
                        app_state.handle_token_expired()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                        return
                    else:
                        raise e
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/clear_logs"):
            app_state.clear_logs()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/stream/list"):
            # Folder listing for the Stream tab. Expanding a torrent here does
            # navigate the browser, but it never resolves a file's link.
            try:
                path = _clean_vfs_path(params.get("path", "/"))
            except ValueError as e:
                self._send_json({"success": False, "error": str(e)}, status=400)
                return
            try:
                entries = app_state.get_vfs().listdir_entries(path)
            except Exception as e:
                self._send_json(self._stream_error(e))
                return
            self._send_json({"success": True, "path": path, "entries": entries})
            return

        elif self.path.startswith("/api/stream/resolve"):
            # The only place a file's CDN link is resolved, and only because
            # the user clicked that file.
            try:
                path = _clean_vfs_path(params.get("path", ""))
            except ValueError as e:
                self._send_json({"success": False, "error": str(e)}, status=400)
                return
            try:
                from torbox_downloader.mount.vfs import CDN_LINK_TTL_S
                vfs = app_state.get_vfs()
                url = vfs.resolve_link(path)
                name = path.rsplit("/", 1)[-1]
                payload = {
                    "success": True,
                    "url": url,
                    "kind": vfs.classify_kind(name),
                    "playable": vfs.is_browser_playable(name),
                    "expires_at": time.time() + CDN_LINK_TTL_S,
                }
            except Exception as e:
                self._send_json(self._stream_error(e))
                return
            self._send_json(payload)
            return

        elif self.path.startswith("/api/stream/refresh"):
            try:
                torrents = app_state.get_vfs().refresh() or []
            except Exception as e:
                self._send_json(self._stream_error(e))
                return
            self._send_json({"success": True, "torrents": len(torrents)})
            return

        elif self.path.startswith("/api/fetch_structure"):
            url = params.get("url", "https://torbox.app/download")
            torrent_name = params.get("torrent_name")
            
            if not app_state.token or not torrent_name:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Missing token or torrent_name"}).encode("utf-8"))
                return
                
            from main import get_torrent_files_structure
            if not app_state.fetch_lock.acquire(blocking=False):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Another fetch is in progress. Please wait."}).encode("utf-8"))
                return
            try:
                session = app_state.get_or_create_session()
                if not session:
                    raise RuntimeError("No valid session token provided.")
                files = session.fetch_structure(torrent_name)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "files": files}).encode("utf-8"))
            except Exception as e:
                if "session token has expired or is invalid" in str(e):
                    app_state.handle_token_expired()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            finally:
                app_state.fetch_lock.release()
            return

        elif self.path.startswith("/api/generate_link"):
            torrent_name = params.get("torrent_name")
            name = params.get("name")
            size = params.get("size")
            selector = params.get("selector")
            
            if not app_state.token or not torrent_name or not name:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Missing token, torrent_name or file details"}).encode("utf-8"))
                return
                
            if not app_state.fetch_lock.acquire(blocking=True, timeout=30):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Another browser operation is in progress. Please wait."}).encode("utf-8"))
                return
                
            try:
                session = app_state.get_or_create_session()
                if not session:
                    raise RuntimeError("No valid session token provided.")
                
                # Expand torrent if not current
                if session.current_torrent_name != torrent_name or "download" not in session.page.url:
                    session.fetch_structure(torrent_name)
                    
                download_url = session.generate_download_link(name, size, selector)
                
                if download_url:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "download_url": download_url}).encode("utf-8"))
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": "Failed to generate link (Direct Download button not found/captured)"}).encode("utf-8"))
            except Exception as e:
                if "session token has expired or is invalid" in str(e):
                    app_state.handle_token_expired()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            finally:
                app_state.fetch_lock.release()
            return

        elif self.path.startswith("/api/start"):
            if app_state.status in ["scanning", "downloading"]:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Downloader is already running.")
                return

            app_state.abort_flag = False
            app_state.cancel_event.clear()
            app_state.error_message = ""
            url = params.get("url", "https://torbox.app/download")
            torrent_name = params.get("torrent_name", None)
            selected_files = params.get("selected_files", None)
            
            t = threading.Thread(
                target=downloader_worker,
                args=(
                    url,
                    app_state.dest_dir,
                    app_state.delay,
                    app_state.headless,
                    app_state.browser_type,
                    app_state.token,
                    torrent_name,
                    app_state.max_parallel_files,
                    selected_files,
                    app_state.parallel_connections,
                    app_state.disk_profile,
                    app_state.write_buffer_mb
                ),
                daemon=True
            )
            t.start()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/retry"):
            if app_state.status in ["scanning", "downloading"]:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Downloader is already running."}).encode("utf-8"))
                return

            filename = params.get("filename")
            if not filename or filename not in app_state.file_to_torrent_map:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Failed to find download information for the requested file."}).encode("utf-8"))
                return

            mapping = app_state.file_to_torrent_map[filename]
            torrent_name = mapping["torrent_name"]
            selected_files = [mapping["relative_path"]]

            app_state.abort_flag = False
            app_state.error_message = ""
            url = "https://torbox.app/download"

            with app_state.lock:
                idx = app_state._queue_index.get(filename)
                if idx is not None:
                    item = app_state.queue[idx]
                    item["status"] = "queued"
                    item["progress"] = 0
                    item["speed"] = "0 KB/s"

            t = threading.Thread(
                target=downloader_worker,
                args=(
                    url,
                    app_state.dest_dir,
                    app_state.delay,
                    app_state.headless,
                    app_state.browser_type,
                    app_state.token,
                    torrent_name,
                    app_state.max_parallel_files,
                    selected_files,
                    app_state.parallel_connections,
                    app_state.disk_profile,
                    app_state.write_buffer_mb
                ),
                daemon=True
            )
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/stop"):
            app_state.abort_flag = True
            app_state.cancel_event.set()
            app_state.set_status("idle")
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Stop signal received. Aborting queue...")
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/mount_drive"):
            if not app_state.token:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Save a session token before mounting."}).encode("utf-8"))
                return

            started = app_state.mount_manager.mount_async(app_state)
            if not started:
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "A mount or download operation is already in progress."}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/unmount_drive"):
            started = app_state.mount_manager.unmount_async(app_state)
            if not started:
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "No mounted drive to unmount right now."}).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path.startswith("/api/open_drive"):
            snap = app_state.mount_manager.status_snapshot()
            letter = snap.get("drive_letter")
            if not letter or snap.get("status") != "mounted":
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Drive is not mounted yet."}).encode("utf-8"))
                return
            try:
                os.startfile(f"{letter}:\\")
                result = {"success": True}
            except Exception as e:
                result = {"success": False, "error": f"Could not open {letter}:\\ : {e}"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        elif self.path.startswith("/api/logout"):
            app_state.abort_flag = True
            app_state.cancel_event.set()
            with app_state.lock:
                app_state.token = ""
                app_state.email = ""
                app_state.queue = []
                app_state.status = "idle"

            # Tear down any mounted virtual drive before killing the session.
            try:
                app_state.mount_manager.unmount_async(app_state)
            except Exception:
                pass

            app_state.stop_session()
            app_state.drop_vfs()

            if os.path.exists("token.txt"):
                try:
                    os.remove("token.txt")
                except Exception:
                    pass
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Logged out and cleared session token.")
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

def start_gui_server(token="", dest_dir="downloads", delay=5, browser_type="chromium", headless=True, port=8000):
    if token:
        app_state.token = token
        app_state.email = extract_email_from_token(token)
    if dest_dir and dest_dir != "downloads":
        app_state.dest_dir = dest_dir
    elif not app_state.dest_dir:
        app_state.dest_dir = "downloads"
        
    if delay != 5:
        app_state.delay = delay
        
    app_state.browser_type = browser_type
    app_state.headless = headless

    sys.stdout = LogRedirector(app_state)

    # Initialize shared browser session
    if app_state.token:
        app_state.get_or_create_session()
        
        # Pre-populate list on startup
        t = threading.Thread(
            target=scan_torrents_background,
            daemon=True
        )
        t.start()

    # Start watch mode polling loop
    t_watch = threading.Thread(target=watch_mode_loop, name="TorboxWatchThread", daemon=True)
    t_watch.start()

    server = ThreadingHTTPServer(("localhost", port), GUIRequestHandler)
    print(f"Torbox Downloader Web GUI is running at: http://localhost:{port}")
    
    # Open default browser with secure startup key
    webbrowser.open(f"http://localhost:{port}/?api_key={app_state.api_key}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI Server...")
        # Tear down the virtual drive (net use / fs stop) before the session.
        try:
            app_state.mount_manager.unmount_sync()
        except Exception:
            pass
        app_state.stop_session()
        try:
            server.server_close()
        except:
            pass
        import os
        os._exit(0)
    finally:
        app_state.stop_session()
