import os
import sys
import json
import time
import shutil
import threading
import webbrowser
import uuid
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

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
        self.fetch_lock = threading.Lock()  # Serialize fetch_structure browser launches
        self.token = ""
        self.email = ""
        self.dest_dir = "downloads"
        self.delay = 5
        self.browser_type = "chromium"
        self.headless = True
        self.max_parallel_files = 1
        self.parallel_connections = True
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
        self.api_key = str(uuid.uuid4())
        
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
                    if "watch_mode" in data:
                        self.watch_mode = bool(data["watch_mode"])
                    if "watch_interval" in data:
                        self.watch_interval = int(data["watch_interval"])
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
                    "watch_mode": self.watch_mode,
                    "watch_interval": self.watch_interval
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
        
        if self.session:
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None

        if os.path.exists("token.txt"):
            try:
                os.remove("token.txt")
            except Exception:
                pass
        self.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Session token has expired. Gracefully logged out.")

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
            for t in torrents:
                self.queue.append({
                    "name": t["name"],
                    "path": "",
                    "size": t["size"],
                    "status": "queued",
                    "progress": 0,
                    "speed": "0 KB/s"
                })

    def update_file_progress(self, filename, status, downloaded, total, speed_bytes):
        with self.lock:
            for item in self.queue:
                if item["name"] == filename:
                    item["status"] = status
                    if total > 0:
                        item["progress"] = int((downloaded / total) * 100)
                    else:
                        item["progress"] = 0
                    
                    if speed_bytes > 1024 * 1024:
                        item["speed"] = f"{speed_bytes / (1024*1024):.1f} MB/s"
                    elif speed_bytes > 1024:
                        item["speed"] = f"{speed_bytes / 1024:.1f} KB/s"
                    elif speed_bytes > 0:
                        item["speed"] = f"{int(speed_bytes)} B/s"
                    else:
                        item["speed"] = "0 KB/s"
                    break

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
                progress = int((downloaded / total) * 100) if total > 0 else 0
                if speed_bytes > 1024 * 1024:
                    speed_str = f"{speed_bytes / (1024*1024):.1f} MB/s"
                elif speed_bytes > 1024:
                    speed_str = f"{speed_bytes / 1024:.1f} KB/s"
                elif speed_bytes > 0:
                    speed_str = f"{int(speed_bytes)} B/s"
                else:
                    speed_str = "0 KB/s"
                    
                self.active_downloads[filename] = {
                    "downloaded": downloaded,
                    "total": total,
                    "speed": speed_str,
                    "status": "downloading",
                    "progress": progress
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
            watch_mode = self.watch_mode
            watch_interval = self.watch_interval

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
            total, used, free = shutil.disk_usage(probe)
            free_gb = free / (1024 ** 3)
            if free_gb > 1024:
                free_space_str = f"{free_gb / 1024:.1f} TB"
            else:
                free_space_str = f"{free_gb:.1f} GB"
        except Exception:
            pass

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
            "watch_mode": watch_mode,
            "watch_interval": watch_interval
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
        if not app_state.session:
            from torbox_downloader.browser_session import TorboxBrowserSession
            app_state.session = TorboxBrowserSession(app_state.token, app_state.browser_type, app_state.headless)
            app_state.session.start()
            
        from torbox_downloader.browser_session import is_token_expired
        if is_token_expired(app_state.token):
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Session token is expired.")
            app_state.set_status("error", "Your Torbox session token has expired.")
            send_os_notification("Torbox Session Warning", "Your Torbox session token has expired.")
            return
            
        torrents = app_state.session.scan_torrents()
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

def downloader_worker(url, dest_dir, delay, headless, browser_type, token, target_torrent_name=None, max_parallel_files=1, selected_files=None, parallel_connections=True):
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
        if not app_state.session:
            from torbox_downloader.browser_session import TorboxBrowserSession
            app_state.session = TorboxBrowserSession(token, browser_type, headless)
            app_state.session.start()
            
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
            session=app_state.session
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
                                app_state.parallel_connections
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

        # Serve static UI files from /gui subfolder
        target_path = self.path
        if target_path == "/" or target_path.startswith("/?"):
            target_path = "/index.html"

        # Sanitize path to prevent directory traversal
        sanitized_path = os.path.normpath(target_path.split("?")[0].lstrip("/"))
        file_path = os.path.join(os.path.dirname(__file__), "gui", sanitized_path)

        if os.path.exists(file_path) and os.path.isfile(file_path):
            self.send_response(200)
            if file_path.endswith(".html"):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif file_path.endswith(".css"):
                self.send_header("Content-Type", "text/css; charset=utf-8")
            elif file_path.endswith(".js"):
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            
            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")

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
            with app_state.lock:
                if "token" in params:
                    new_token = params["token"].strip()
                    if new_token and new_token != '••••••••••••••••••••' and new_token != app_state.token:
                        app_state.token = new_token
                        app_state.email = extract_email_from_token(new_token)
                        token_updated = True
                        with open("token.txt", "w", encoding="utf-8") as f:
                            f.write(app_state.token)
                if "dest_dir" in params:
                    app_state.dest_dir = params["dest_dir"].strip()
                if "delay" in params:
                    app_state.delay = int(params["delay"])
                if "max_parallel_files" in params:
                    app_state.max_parallel_files = int(params["max_parallel_files"])
                if "parallel_connections" in params:
                    app_state.parallel_connections = bool(params["parallel_connections"])
                if "watch_mode" in params:
                    app_state.watch_mode = bool(params["watch_mode"])
                if "watch_interval" in params:
                    app_state.watch_interval = int(params["watch_interval"])
                app_state.save_settings()
            
            if token_updated:
                try:
                    if app_state.session:
                        app_state.session.update_token(app_state.token)
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
                if not app_state.session:
                    from torbox_downloader.browser_session import TorboxBrowserSession
                    app_state.session = TorboxBrowserSession(app_state.token, app_state.browser_type, app_state.headless)
                    app_state.session.start()
                    
                files = app_state.session.fetch_structure(torrent_name)
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
                    app_state.parallel_connections
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
                for item in app_state.queue:
                    if item["name"] == filename:
                        item["status"] = "queued"
                        item["progress"] = 0
                        item["speed"] = "0 KB/s"
                        break

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
                    selected_files
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

        elif self.path.startswith("/api/logout"):
            app_state.abort_flag = True
            app_state.cancel_event.set()
            with app_state.lock:
                app_state.token = ""
                app_state.email = ""
                app_state.queue = []
                app_state.status = "idle"
            
            if app_state.session:
                try:
                    app_state.session.stop()
                except:
                    pass
                app_state.session = None

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
        from torbox_downloader.browser_session import TorboxBrowserSession
        app_state.session = TorboxBrowserSession(app_state.token, app_state.browser_type, app_state.headless)
        app_state.session.start()
        
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
        try:
            server.server_close()
        except:
            pass
        import os
        os._exit(0)
    finally:
        if app_state.session:
            try:
                app_state.session.stop()
            except:
                pass
