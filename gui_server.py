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
        self.token = ""
        self.dest_dir = "downloads"
        self.delay = 5
        self.browser_type = "chromium"
        self.headless = True
        self.max_parallel_files = 1
        
        self.status = "idle"  # idle, scanning, downloading, finished, error
        self.error_message = ""
        self.queue = []       # list of file dicts: {name, path, size, status, progress, speed}
        self.logs = []        # list of console log lines
        self.abort_flag = False
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
            except Exception:
                pass

    def save_settings(self):
        try:
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump({
                    "dest_dir": self.dest_dir,
                    "delay": self.delay,
                    "max_parallel_files": self.max_parallel_files
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
            # Re-read free space dynamically
            free_space_str = "Unknown"
            try:
                os.makedirs(self.dest_dir, exist_ok=True)
                total, used, free = shutil.disk_usage(self.dest_dir)
                free_gb = free / (1024**3)
                if free_gb > 1024:
                    free_space_str = f"{free_gb / 1024:.1f} TB"
                else:
                    free_space_str = f"{free_gb:.1f} GB"
            except Exception:
                pass

            return {
                "token": self.token,
                "dest_dir": self.dest_dir,
                "delay": self.delay,
                "status": self.status,
                "error_message": self.error_message,
                "queue": list(self.queue),
                "logs": list(self.logs),
                "active_downloads": dict(self.active_downloads),
                "free_space": free_space_str,
                "max_parallel_files": self.max_parallel_files
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
    
    try:
        with sync_playwright() as p:
            browser = getattr(p, browser_type).launch(headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 800})
            page = context.new_page()
            
            # Inject token
            page.goto("https://torbox.app/login", wait_until="load", timeout=30000)
            page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(token)})")
            page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(token)})")
            
            page.goto("https://torbox.app/download", wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)
            
            html = page.content()
            detector = TorboxDetector(html)
            torrents = detector.parse_torrents()
            
            if torrents:
                app_state.update_queue(torrents)
                app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Successfully scraped {len(torrents)} active torrents from Torbox.")
            else:
                app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Checked Torbox download queue: no active torrents found.")
                
            browser.close()
    except Exception as e:
        app_state.add_log(f"[{time.strftime('%H:%M:%S')}] WARNING: Background scan failed: {e}")

def downloader_worker(url, dest_dir, delay, headless, browser_type, token, target_torrent_name=None, max_parallel_files=1):
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
            max_parallel_files=max_parallel_files
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

        elif self.path == "/api/start":
            if app_state.status in ["scanning", "downloading"]:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Downloader is already running.")
                return

            app_state.abort_flag = False
            app_state.error_message = ""
            url = params.get("url", "https://torbox.app/download")
            torrent_name = params.get("torrent_name", None)
            
            # Start background scraping/download thread
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
                    app_state.max_parallel_files
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
            app_state.set_status("idle")
            app_state.add_log(f"[{time.strftime('%H:%M:%S')}] INFO: Stop signal received. Aborting queue...")
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return

        elif self.path == "/api/logout":
            app_state.abort_flag = True
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
