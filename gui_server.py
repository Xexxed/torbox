import os
import sys
import json
import time
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Global App State
class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()  # Serialize fetch_structure browser launches
        self.token = ""
        self.dest_dir = "downloads"
        self.delay = 5
        self.browser_type = "chromium"
        self.headless = True
        self.max_parallel_files = 1
        self.parallel_connections = True
        
        self.status = "idle"  # idle, scanning, downloading, finished, error
        self.error_message = ""
        self.queue = []       # list of file dicts: {name, path, size, status, progress, speed}
        self.logs = []        # list of console log lines
        self.abort_flag = False
        self.cancel_event = threading.Event()
        self.active_downloads = {}  # dict: {filename: {downloaded, total, speed, status, progress}}
        
        # Load default token if present
        if os.path.exists("token.txt"):
            try:
                with open("token.txt", "r", encoding="utf-8") as f:
                    self.token = f.read().strip()
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
            except Exception:
                pass

    def save_settings(self):
        try:
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump({
                    "dest_dir": self.dest_dir,
                    "delay": self.delay,
                    "max_parallel_files": self.max_parallel_files,
                    "parallel_connections": self.parallel_connections
                }, f)
        except Exception:
            pass

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

    def update_queue(self, files):
        with self.lock:
            self.queue = []
            for f in files:
                self.queue.append({
                    "name": f["name"],
                    "path": " / ".join(f["path"]) if "path" in f else "",
                    "size": f["size"],
                    "status": f.get("status", "queued"),
                    "progress": f.get("progress", 0),
                    "speed": f.get("speed", "0 KB/s")
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
                    
                    # Convert speed
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
            token = self.token
            dest_dir = self.dest_dir
            delay = self.delay
            status = self.status
            error_message = self.error_message
            queue = list(self.queue)
            logs = list(self.logs)
            active_downloads = dict(self.active_downloads)
            max_parallel_files = self.max_parallel_files
            parallel_connections = self.parallel_connections

        # Compute free space OUTSIDE the lock so a slow/blocking filesystem
        # call (e.g. on an unready/removable/local path) can't freeze the
        # whole status endpoint and leave the UI stuck on "Scanning...".
        # Also avoid os.makedirs here so we don't create/scan the directory
        # on every 800ms poll.
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
            "token": token,
            "dest_dir": dest_dir,
            "delay": delay,
            "status": status,
            "error_message": error_message,
            "queue": queue,
            "logs": logs,
            "active_downloads": active_downloads,
            "free_space": free_space_str,
            "max_parallel_files": max_parallel_files,
            "parallel_connections": parallel_connections
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
            # Ignore interactive prompt printouts or raw TQDM progress blocks
            if "\r" in text or "██" in text or "%" in text or "Connecting" in clean:
                if "Connecting to download" in clean:
                    self.state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: {clean}")
                return
            
            timestamp = f"[{time.strftime('%H:%M:%S')}]"
            level = "ERROR" if "error" in clean.lower() else "WARN" if "warning" in clean.lower() else "INFO"
            self.state.add_log(f"{timestamp} {level}: {clean}")

    def flush(self):
        self.terminal.flush()

def scan_torrents_background(token, browser_type, headless):
    if not token:
        return
        
    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Scanning Torbox torrents list in background...")
    
    from playwright.sync_api import sync_playwright
    from torbox_downloader.detector import TorboxDetector
    
    browser = None
    context = None
    try:
        with sync_playwright() as p:
            try:
                browser = getattr(p, browser_type).launch(headless=True)
                context = browser.new_context(viewport={'width': 1280, 'height': 800})
                page = context.new_page()
                
                def route_blocker(route):
                    req_url = route.request.url.lower()
                    if "torbox.app" in req_url:
                        route.continue_()
                        return
                    if any(kw in req_url for kw in ['intercom', 'jam.dev', 'featurebase', 'sentry', 'hotjar', 'analytics', 'doubleclick', 'google-analytics']):
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/*", route_blocker)
                
                # Inject token fast via commit wait
                page.goto("https://torbox.app/login", wait_until="commit", timeout=15000)
                page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(token)})")
                page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(token)})")
                
                page.goto("https://torbox.app/download", wait_until="domcontentloaded", timeout=60000)
                
                # Handle auth handshake redirect
                if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
                    page.wait_for_timeout(2000)
                    page.goto("https://torbox.app/download", wait_until="domcontentloaded", timeout=60000)
                    
                # Validate if we are still stuck on the login page (token expired or invalid)
                if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
                    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Session token is expired or invalid. Please paste a fresh token.")
                    app_state.set_status("error", "Your Torbox session token is expired or invalid. Please log in again at torbox.app and paste a fresh token.")
                    return
                    
                try:
                    page.wait_for_selector("button[title='Download files'], button[title*='Download'], button[aria-label*='Download'], a[download]", timeout=15000)
                except Exception:
                    pass
                
                html = page.content()
                detector = TorboxDetector(html)
                torrents = detector.parse_torrents()
                
                if torrents:
                    app_state.update_queue(torrents)
                    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Successfully scraped {len(torrents)} active torrents from Torbox.")
                else:
                    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Checked Torbox download queue: no active torrents found.")
                    
                # Clear error status if scanner succeeded
                with app_state.lock:
                    if app_state.status == "error":
                        app_state.status = "idle"
                        app_state.error_message = ""
            finally:
                if context:
                    try:
                        context.close()
                    except:
                        pass
                if browser:
                    try:
                        browser.close()
                    except:
                        pass
    except Exception as e:
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Background scan failed: {e}")

def downloader_worker(url, dest_dir, delay, headless, browser_type, token, target_torrent_name=None, max_parallel_files=1, selected_files=None, parallel_connections=True):
    app_state.set_status("scanning")
    app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Initializing Torbox Engine...")
    
    # Clear active downloads list
    with app_state.lock:
        app_state.active_downloads.clear()

    # Import run_downloader dynamically
    from main import run_downloader
    
    def file_scraped_cb(files):
        if not target_torrent_name:
            app_state.update_queue(files)
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Found and matched torrent target.")
 
    def status_update_cb(filename, status, downloaded, total, speed):
        # Handle abort signal injection
        if app_state.abort_flag:
            raise Exception("Download task aborted by user.")
            
        if status in ['downloading_file', 'completed_file', 'failed_file']:
            app_state.update_active_download(filename, status, downloaded, total, speed)
        else:
            app_state.update_file_progress(filename, status, downloaded, total, speed)
            app_state.set_status("downloading")
  
    try:
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
            cancel_event=app_state.cancel_event
        )
        app_state.set_status("finished")
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: All downloads finished successfully!")
    except Exception as e:
        if app_state.abort_flag:
            app_state.set_status("idle")
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Download task successfully aborted.")
        else:
            app_state.set_status("error", str(e))
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] ERROR: {str(e)}")

# API & Static Server Handlers
class GUIRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging HTTP requests to console to keep outputs clean
        pass

    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(app_state.get_serializable_state()).encode("utf-8"))
            return

        # Serve static UI files from /gui subfolder
        target_path = self.path
        if target_path == "/":
            target_path = "/index.html"

        # Sanitize path to prevent directory traversal
        sanitized_path = os.path.normpath(target_path.lstrip("/"))
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
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length).decode("utf-8")
        
        try:
            params = json.loads(post_data) if post_data else {}
        except Exception:
            params = {}

        if self.path == "/api/settings":
            token_updated = False
            with app_state.lock:
                if "token" in params:
                    new_token = params["token"].strip()
                    if new_token != app_state.token:
                        app_state.token = new_token
                        token_updated = True
                    # Write to token.txt automatically
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
                app_state.save_settings()
            
            if token_updated:
                t = threading.Thread(
                    target=scan_torrents_background,
                    args=(app_state.token, app_state.browser_type, app_state.headless),
                    daemon=True
                )
                t.start()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path == "/api/fetch_structure":
            url = params.get("url", "https://torbox.app/download")
            torrent_name = params.get("torrent_name")
            token = params.get("token") or app_state.token
            
            if not token or not torrent_name:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Missing token or torrent_name"}).encode("utf-8"))
                return
                
            from main import get_torrent_files_structure
            # Serialize browser launches to prevent concurrent Chromium instances from crashing
            if not app_state.fetch_lock.acquire(blocking=False):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": "Another fetch is in progress. Please wait."}).encode("utf-8"))
                return
            try:
                files = get_torrent_files_structure(
                    token=token,
                    url=url,
                    target_torrent_name=torrent_name,
                    browser_type=app_state.browser_type,
                    headless=app_state.headless
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "files": files}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
            finally:
                app_state.fetch_lock.release()
            return

        elif self.path == "/api/start":
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

        elif self.path == "/api/stop":
            app_state.abort_flag = True
            app_state.cancel_event.set()
            app_state.set_status("idle")
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Stop signal received. Aborting queue...")
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path == "/api/logout":
            app_state.abort_flag = True
            app_state.cancel_event.set()
            with app_state.lock:
                app_state.token = ""
                app_state.queue = []
                app_state.status = "idle"
            # Delete token.txt
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
    # Initialize global state from CLI args if they were provided and override persistent settings
    if token:
        app_state.token = token
    if dest_dir and dest_dir != "downloads":
        app_state.dest_dir = dest_dir
    elif not app_state.dest_dir:
        app_state.dest_dir = "downloads"
        
    if delay != 5:
        app_state.delay = delay
        
    app_state.browser_type = browser_type
    app_state.headless = headless

    # Redirect stdout
    sys.stdout = LogRedirector(app_state)

    if app_state.token:
        # Pre-populate list on startup
        t = threading.Thread(
            target=scan_torrents_background,
            args=(app_state.token, app_state.browser_type, app_state.headless),
            daemon=True
        )
        t.start()

    server = ThreadingHTTPServer(("localhost", port), GUIRequestHandler)
    print(f"Torbox Downloader Web GUI is running at: http://localhost:{port}")
    
    # Open default browser automatically
    webbrowser.open(f"http://localhost:{port}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI Server...")
        server.server_close()
