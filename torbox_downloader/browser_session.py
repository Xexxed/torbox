import os
import sys
import json
import time
import logging
import base64
import re
import queue
import threading

logger = logging.getLogger(__name__)

# The Torbox dashboard lazy-loads its torrent list (infinite scroll): right
# after page load only the few cards near the viewport exist in the DOM, so a
# single HTML snapshot yields only ~5 torrents. These tunables control how we
# scroll the list until every card has been fetched and rendered.
SCAN_MAX_ROUNDS = 60        # hard cap on scroll rounds (safety net)
SCAN_STALE_ROUNDS = 3       # stop after this many consecutive rounds with no new cards
SCAN_SCROLL_WAIT_MS = 400   # wait between scroll rounds so the SPA can render

def is_token_expired(token):
    """
    Checks offline if a token is expired. Supports raw JWT strings
    and JSON object strings matching Supabase auth structure.
    """
    if not token:
        return True
    try:
        access_token = token.strip()
        if access_token.startswith('{') and access_token.endswith('}'):
            try:
                data = json.loads(access_token)
                access_token = data.get("access_token", access_token)
            except Exception:
                pass
        
        parts = access_token.split('.')
        if len(parts) != 3:
            # Fallback: assume not expired if not a standard JWT
            return False
            
        payload_b64 = parts[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode('utf-8'))
        
        exp = payload.get('exp')
        if exp:
            # 10 second buffer
            return time.time() >= (exp - 10)
    except Exception as e:
        logger.warning(f"Error checking token expiry: {e}")
    return False


class BrowserCommand:
    def __init__(self, action, args=None, kwargs=None):
        self.action = action
        self.args = args or []
        self.kwargs = kwargs or {}
        self.result_queue = queue.Queue()


class TorboxBrowserSession:
    """
    Thread-safe Browser Session Manager for Torbox.
    Initializes and controls Playwright on a dedicated worker thread
    to comply with Playwright's thread-affinity constraints.
    """
    def __init__(self, token, browser_type="chromium", headless=True, download_url="https://torbox.app/download"):
        self.token = token
        self.browser_type = browser_type
        self.headless = headless
        self.download_url = download_url
        
        self.command_queue = queue.Queue()
        self.worker_thread = None
        self._running = False
        
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        self._intercepted_download_url = None
        self._recovery_count = 0

    def start(self):
        """Spawns the background worker thread."""
        if self._running:
            return
        self._running = True
        self.worker_thread = threading.Thread(target=self._run_worker, name="TorboxBrowserWorker", daemon=True)
        self.worker_thread.start()
        
        # Block until worker is ready
        self._send_command("ping")

    def stop(self):
        """Stops the worker thread and shuts down the browser."""
        if not self._running:
            return
        self._running = False
        try:
            self._send_command("close")
        except Exception:
            pass
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
            self.worker_thread = None

    def scan_torrents(self):
        """Thread-safe call to scan active torrent cards."""
        return self._send_command("scan_torrents")

    def fetch_structure(self, torrent_name):
        """Thread-safe call to fetch file list inside a specific torrent card."""
        return self._send_command("fetch_structure", args=[torrent_name])

    def generate_download_link(self, name, size, selector):
        """Thread-safe call to click a file's download details and intercept CDN link."""
        return self._send_command("generate_download_link", args=[name, size, selector])

    def go_to_dashboard(self):
        """Thread-safe call to reset view back to the dashboard URL."""
        return self._send_command("go_to_dashboard")

    def update_token(self, new_token):
        """Thread-safe call to update login token and reload browser."""
        return self._send_command("update_token", args=[new_token])

    def get_page_content(self):
        """Thread-safe call to get raw page HTML content."""
        return self._send_command("get_page_content")

    # --- Worker Thread Implementation ---

    def _send_command(self, action, args=None, kwargs=None):
        if not self._running:
            raise RuntimeError("Browser worker thread is not running.")
        cmd = BrowserCommand(action, args, kwargs)
        self.command_queue.put(cmd)
        success, result = cmd.result_queue.get()
        if not success:
            raise result
        return result

    def _run_worker(self):
        from playwright.sync_api import sync_playwright
        
        try:
            self.playwright = sync_playwright().start()
            self._init_browser()
            
            while self._running:
                try:
                    cmd = self.command_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                if cmd.action == "ping":
                    cmd.result_queue.put((True, "pong"))
                    self.command_queue.task_done()
                    continue
                
                if cmd.action == "close":
                    self._close_browser()
                    self.playwright.stop()
                    cmd.result_queue.put((True, None))
                    self.command_queue.task_done()
                    break
                
                try:
                    res = self._execute_action(cmd.action, *cmd.args, **cmd.kwargs)
                    cmd.result_queue.put((True, res))
                except Exception as ex:
                    logger.exception(f"Error executing command {cmd.action}: {ex}")
                    cmd.result_queue.put((False, ex))
                finally:
                    self.command_queue.task_done()
        except Exception as e:
            logger.exception(f"Fatal exception in BrowserWorker: {e}")
            self._running = False

    def _init_browser(self):
        engine = getattr(self.playwright, self.browser_type)
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled"
        ]
        launch_args = browser_args if self.browser_type == 'chromium' else []
        
        try:
            self.browser = engine.launch(headless=self.headless, args=launch_args)
        except Exception:
            self.browser = engine.launch(headless=self.headless)
            
        self.context = self.browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        self.page = self.context.new_page()
        
        # Route Blocker for analytics and other heavy trackers
        def route_blocker(route):
            req_url = route.request.url.lower()
            if "torbox.app" in req_url:
                route.continue_()
                return
            if any(kw in req_url for kw in ['intercom', 'jam.dev', 'featurebase', 'sentry', 'hotjar', 'analytics', 'doubleclick', 'google-analytics']):
                route.abort()
            else:
                route.continue_()
                
        self.page.route("**/*", route_blocker)
        
        # Setup RequestDL and CDN handlers
        def _capture_requestdl(route):
            try:
                response = route.fetch()
                try:
                    body = response.json()
                    if body.get("success") and body.get("data"):
                        self._intercepted_download_url = body["data"]
                        # Fulfill with same-page hash to prevent main frame navigation and crash
                        modified_body = {
                            "success": True,
                            "data": "https://torbox.app/download#intercepted"
                        }
                        route.fulfill(
                            status=200,
                            headers=response.headers,
                            json=modified_body
                        )
                        return
                except Exception:
                    pass
                route.fulfill(response=response)
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
        
        def _block_cdn_navigation(route):
            route.abort("blockedbyclient")
            
        self.page.route("**/requestdl*", _capture_requestdl)
        self.page.route("**tb-cdn**", _block_cdn_navigation)
        
        # Authenticate and go to destination
        self._authenticate()

    def _authenticate(self):
        if self.token:
            try:
                self.page.goto("https://torbox.app/login", wait_until="commit", timeout=15000)
                self.page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(self.token)})")
                self.page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(self.token)})")
            except Exception as e:
                logger.warning(f"Failed to inject session token during initialization: {e}")
                
        logger.info(f"Navigating to download dashboard: {self.download_url}")
        self.page.goto(self.download_url, wait_until="domcontentloaded", timeout=60000)
        
        # Check for Supabase auth handshake redirect
        if "login" in self.page.url.lower() or self.page.locator("input[type='password']").count() > 0:
            if self.token:
                self.page.wait_for_timeout(2000)
                self.page.goto(self.download_url, wait_until="domcontentloaded", timeout=60000)
                
        # Final validation
        if "login" in self.page.url.lower() or self.page.locator("input[type='password']").count() > 0:
            raise Exception("Your Torbox session token is expired or invalid. Please paste a fresh token.")
            
        try:
            self.page.wait_for_selector("button[title='Download files'], button[title*='Download'], button[aria-label*='Download'], a[download]", timeout=15000)
        except Exception:
            pass

    def _close_browser(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        self.page = None

    def _recover_page_if_crashed(self):
        try:
            current_url = self.page.url
        except Exception:
            current_url = ""
            
        is_crashed = (
            "chrome-error://" in current_url
            or current_url == "about:blank"
            or not current_url
        )
        
        if not is_crashed:
            self._recovery_count = 0
            return True

        self._recovery_count = getattr(self, "_recovery_count", 0) + 1
        if self._recovery_count > 3:
            logger.error("Browser session crashed repeatedly (3+ recovery attempts failed). Aborting recovery.")
            raise RuntimeError("Browser session crashed and could not be recovered after 3 attempts.")

        logger.info(f"Browser page crashed (URL: {current_url}). Initiating automatic recovery (Attempt {self._recovery_count}/3)...")
        try:
            self._close_browser()
            self._init_browser()
            
            # Re-expand current torrent if set!
            torrent_name = getattr(self, "current_torrent_name", None)
            if torrent_name:
                logger.info(f"Re-expanding torrent card during recovery: {torrent_name}")
                btn_to_click = self._locate_torrent_download_button(torrent_name)
                if btn_to_click is None or btn_to_click.count() == 0:
                    # Card may be lazy-loaded below the fold after the reload.
                    btn_to_click = self._locate_torrent_download_button_scrolling(torrent_name)
                if btn_to_click is not None and btn_to_click.count() > 0:
                    btn_to_click.evaluate("el => el.click()")
                    try:
                        self.page.locator("button[title*='Options'], button[title*='options']").first.wait_for(state="visible", timeout=10000)
                    except Exception:
                        pass
            return True
        except Exception as e:
            logger.error(f"Failed to recover browser session: {e}")
            return False

    def _reset_to_dashboard(self):
        try:
            logger.info("Resetting SPA view by navigating to home page first...")
            # Go to home page (forces SPA router out of the download dashboard route)
            self.page.goto("https://torbox.app/", wait_until="domcontentloaded", timeout=15000)
            self.page.wait_for_timeout(500)
            # Navigate back to download dashboard (forces a clean re-mount of download dashboard)
            self.page.goto(self.download_url, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_selector("button[title='Download files']", timeout=15000)
            self.page.wait_for_timeout(1000)
            return True
        except Exception as e:
            logger.warning(f"Error resetting to dashboard: {e}")
            try:
                # Fallback: force browser reload
                self.page.reload(wait_until="domcontentloaded", timeout=30000)
                self.page.wait_for_timeout(1000)
            except Exception:
                pass
            return False

    def _locate_torrent_download_button(self, torrent_name):
        """Find the 'Download files' button of the torrent card whose title
        matches torrent_name. Returns a Playwright locator or None."""
        try:
            title_el = self.page.locator("p, span, div, h1, h2, h3, h4").filter(has_text=torrent_name).last
            if title_el.count() > 0:
                card = title_el
                for _ in range(10):
                    card = card.locator("..")
                    if card.locator("button[title='Download files']").count() > 0:
                        return card.locator("button[title='Download files']").first
        except Exception:
            pass
        return None

    def _scroll_page_down(self):
        """Scroll the dashboard list down by one viewport to trigger the next
        lazy-loaded batch. Prefers wheel events (bubbling to whichever element
        is scrollable under the cursor) with a window.scrollBy fallback."""
        try:
            self.page.mouse.wheel(0, 800)
        except Exception:
            self.page.evaluate("window.scrollBy(0, window.innerHeight)")

    def _collect_dashboard_torrents(self):
        """
        Scroll the dashboard list and repeatedly parse the DOM, accumulating
        unique torrent cards.

        The Torbox dashboard lazy-loads its list (infinite scroll): right after
        page load only the few cards near the viewport exist in the DOM, so a
        single HTML snapshot used to yield only ~5 torrents. Scrolling forces
        the SPA to fetch and render the rest; merging snapshots by name also
        copes with virtualised lists that remove off-screen cards.
        """
        from torbox_downloader.detector import TorboxDetector

        collected = {}
        stale_rounds = 0

        # Wheel events must land on the scrollable area; park the cursor mid-page.
        try:
            self.page.mouse.move(640, 400)
        except Exception:
            pass

        for round_idx in range(SCAN_MAX_ROUNDS):
            try:
                html_content = self.page.content()
                found = TorboxDetector(html_content).parse_torrents()
            except Exception as e:
                logger.warning(f"scan_torrents: parse failed on scroll round {round_idx}: {e}")
                found = []

            new_count = 0
            for t in found:
                if t["name"] not in collected:
                    collected[t["name"]] = t
                    new_count += 1

            if new_count:
                stale_rounds = 0
            else:
                stale_rounds += 1
                if stale_rounds >= SCAN_STALE_ROUNDS:
                    break

            try:
                self._scroll_page_down()
            except Exception as e:
                logger.warning(f"scan_torrents: scroll failed on round {round_idx}: {e}")
            self.page.wait_for_timeout(SCAN_SCROLL_WAIT_MS)

        # Return to the top so subsequent card lookups start from a sane state.
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(200)
        except Exception:
            pass

        return list(collected.values())

    def _locate_torrent_download_button_scrolling(self, torrent_name, max_rounds=SCAN_MAX_ROUNDS, wait_ms=SCAN_SCROLL_WAIT_MS):
        """
        Scroll the lazy-loaded dashboard list until the torrent card's download
        button appears. Needed when the requested torrent sits below the fold
        (only ~5 cards are rendered near the initial viewport).
        """
        try:
            self.page.mouse.move(640, 400)
        except Exception:
            pass

        for _ in range(max_rounds):
            btn = self._locate_torrent_download_button(torrent_name)
            if btn is not None and btn.count() > 0:
                return btn

            try:
                self._scroll_page_down()
            except Exception as e:
                logger.warning(f"Card search scroll failed for '{torrent_name}': {e}")
            self.page.wait_for_timeout(wait_ms)

        return None

    def _execute_action(self, action, *args, **kwargs):
        # Always run recovery check before executing command
        self._recover_page_if_crashed()
        
        # Early Token Expiration Check
        if action != "update_token":
            if "login" in self.page.url.lower() or self.page.locator("input[type='password']").count() > 0:
                raise Exception("Your Torbox session token has expired or is invalid.")
            
        if action == "scan_torrents":
            # If we're not on the main dashboard url, or if there are no 'Download files' buttons,
            # we force a reset to clear the SPA state back to the active torrents dashboard list.
            try:
                if "download" not in self.page.url or self.page.locator("button[title='Download files']").count() == 0:
                    logger.info("scan_torrents: Not on dashboard or no cards found. Resetting dashboard view...")
                    self._reset_to_dashboard()
            except Exception as e:
                logger.warning(f"Error checking/navigating to dashboard in scan_torrents: {e}")
                
            return self._collect_dashboard_torrents()
            
        elif action == "fetch_structure":
            torrent_name = args[0]
            self.current_torrent_name = torrent_name
            from torbox_downloader.detector import TorboxDetector
            
            # Locate download button for target torrent.
            # If not found or if the torrent card isn't visible, navigate to dashboard first.
            btn_to_click = self._locate_torrent_download_button(torrent_name)

            if btn_to_click is None or btn_to_click.count() == 0:
                logger.info(f"Torrent '{torrent_name}' download button not found or not visible. Resetting dashboard view...")
                self._reset_to_dashboard()
                btn_to_click = self._locate_torrent_download_button(torrent_name)

            if btn_to_click is None or btn_to_click.count() == 0:
                # The card may sit below the fold: the dashboard lazy-loads its
                # list, so only the first few cards exist in the DOM until we scroll.
                logger.info(f"Torrent '{torrent_name}' still not found. Scrolling dashboard to locate card...")
                btn_to_click = self._locate_torrent_download_button_scrolling(torrent_name)

            if btn_to_click and btn_to_click.count() > 0:
                btn_to_click.evaluate("el => el.click()")
                try:
                    self.page.locator("button[title*='Options'], button[title*='options'], button[aria-label*='Options'], button[aria-label*='options']").first.wait_for(state="visible", timeout=10000)
                except Exception:
                    pass
                    
            html_content = self.page.content()
            detector = TorboxDetector(html_content)
            files = detector.parse_structure()
            return files
            
        elif action == "generate_download_link":
            name, size, selector = args[0], args[1], args[2]
            
            # 1. Dismiss open menus
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(200)
            except Exception:
                pass
                
            # 2. Find row element
            row_locator = None
            def is_row_size_matching(row_text, expected_size_str):
                if not expected_size_str:
                    return True
                match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|B))', row_text, re.I)
                if not match:
                    return False
                row_size_str = match.group(1)
                from torbox_downloader.downloader import parse_size_to_bytes
                expected_bytes = parse_size_to_bytes(expected_size_str)
                row_bytes = parse_size_to_bytes(row_size_str)
                if not expected_bytes or not row_bytes:
                    return False
                ratio = row_bytes / expected_bytes
                return 0.7 <= ratio <= 1.3

            try:
                file_label = self.page.locator("p, span, div").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$")).last
                if file_label.count() == 0:
                    file_label = self.page.locator("p, span, div", has_text=name).last
                
                row = file_label
                found_container = False
                for _ in range(8):
                    row = row.locator("..")
                    if row.locator("button[title*='options'], button[title*='Options'], button[title*='download'], button[title*='Download']").count() > 0:
                        row_locator = row.locator("..")
                        found_container = True
                        break
                if not found_container:
                    row_locator = row
                
                if size and row_locator.count() > 0:
                    row_text = row_locator.text_content()
                    if not is_row_size_matching(row_text, size):
                        candidates = self.page.locator("p, span, div", has_text=name)
                        for c_idx in range(candidates.count()):
                            cand_label = candidates.nth(c_idx)
                            cand_row = cand_label
                            found_cand = False
                            for _ in range(8):
                                cand_row = cand_row.locator("..")
                                if cand_row.locator("button[title*='options'], button[title*='Options']").count() > 0:
                                    cand_row_container = cand_row.locator("..")
                                    found_cand = True
                                    break
                            if not found_cand:
                                cand_row_container = cand_row
                                
                            if is_row_size_matching(cand_row_container.text_content(), size):
                                row_locator = cand_row_container
                                break
            except Exception as e:
                logger.warning(f"Failed to locate row via exact matching: {e}. Trying fallback selector...")
                
            if not row_locator or row_locator.count() == 0:
                if selector:
                    row_locator = self.page.locator(selector)
                    if row_locator.count() > 1:
                        row_locator = self.page.locator("div", has_text=name).last
                        
            if not row_locator or row_locator.count() == 0:
                # Proactive recovery: if we can't find the row, try to re-expand the active torrent folder
                torrent_name = getattr(self, "current_torrent_name", None)
                if torrent_name:
                    logger.warning(f"File row '{name}' not found. Attempting to re-expand torrent card '{torrent_name}'...")
                    try:
                        # Force reload to reset the SPA view to the dashboard and locate the card correctly
                        if "download" not in self.page.url or self.page.locator("button[title='Download files']").count() == 0:
                            self._reset_to_dashboard()

                        btn_to_click = self._locate_torrent_download_button(torrent_name)
                        if btn_to_click is None or btn_to_click.count() == 0:
                            # Card may be lazy-loaded below the fold.
                            btn_to_click = self._locate_torrent_download_button_scrolling(torrent_name)
                        if btn_to_click is not None and btn_to_click.count() > 0:
                            btn_to_click.evaluate("el => el.click()")
                            try:
                                self.page.locator("button[title*='Options'], button[title*='options']").first.wait_for(state="visible", timeout=6000)
                            except Exception:
                                pass
                                
                            # Re-try finding the row
                            file_label = self.page.locator("p, span, div").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$")).last
                            if file_label.count() == 0:
                                file_label = self.page.locator("p, span, div", has_text=name).last
                            
                            row = file_label
                            found_container = False
                            for _ in range(8):
                                row = row.locator("..")
                                if row.locator("button[title*='options'], button[title*='Options'], button[title*='download'], button[title*='Download']").count() > 0:
                                    row_locator = row.locator("..")
                                    found_container = True
                                    break
                            if not found_container:
                                row_locator = row
                    except Exception as re_err:
                        logger.warning(f"Re-expansion attempt failed: {re_err}")

            if not row_locator or row_locator.count() == 0:
                logger.error(f"Could not locate row for file: {name}")
                return None
                
            # 3. Find and click options button
            dl_btn = row_locator.locator("button[title*='options'], button[title*='Options']").first
            if dl_btn.count() == 0:
                dl_btn = row_locator.locator("button[title*='download'], button[title*='Download']").first
            if dl_btn.count() == 0:
                buttons = row_locator.locator("button")
                for b_idx in range(buttons.count()):
                    btn_item = buttons.nth(b_idx)
                    btn_html = btn_item.inner_html().lower()
                    btn_title = (btn_item.get_attribute("title") or "").lower()
                    if any(x in btn_html or x in btn_title for x in ['play', 'stream', 'watch', 'video']):
                        continue
                    if "download" in btn_html or "svg" in btn_html or "icon" in btn_html or "dl" in btn_html:
                        dl_btn = btn_item
                        break
                        
            if not dl_btn or dl_btn.count() == 0:
                logger.error(f"Could not locate download options button for: {name}")
                return None
                
            dl_btn.evaluate("el => el.click()")
            self.page.wait_for_timeout(1000)
            
            # 4. Find Direct Download
            direct_dl_btn = row_locator.locator("button:visible, a:visible", has_text="Direct Download").first
            if direct_dl_btn.count() == 0:
                logger.error(f"Could not find 'Direct Download' button for: {name}")
                try:
                    self.page.keyboard.press("Escape")
                except Exception:
                    pass
                return None
                
            direct_dl_btn.wait_for(state="visible", timeout=10000)
            
            # 5. Click and await CDN URL capture
            self._intercepted_download_url = None
            direct_dl_btn.evaluate("el => el.click()")
            
            for _ in range(75):
                self.page.wait_for_timeout(200)
                if self._intercepted_download_url:
                    break
                    
            result_url = self._intercepted_download_url
            self._intercepted_download_url = None
            
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(300)
            except Exception:
                pass
                
            return result_url
            
        elif action == "go_to_dashboard":
            try:
                self.page.goto(self.download_url, wait_until="load", timeout=30000)
                self.page.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"Error resetting view: {e}")
            return True
            
        elif action == "update_token":
            new_token = args[0]
            self.token = new_token
            self._authenticate()
            return True
            
        elif action == "get_page_content":
            return self.page.content()
            
        else:
            raise ValueError(f"Unknown action: {action}")
