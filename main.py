import os
import sys
import argparse
import time
import json
import logging
from torbox_downloader.detector import TorboxDetector
from torbox_downloader.creator import create_folder_structure
from torbox_downloader.downloader import download_file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    logger.error("Playwright is not installed. Please run: pip install playwright && playwright install")
    sys.exit(1)

def download_files_pipeline(page, dest_dir, files_with_local_paths, max_parallel_files, status_update_callback=None, torrent_name_for_status=None, url=None, token=None):
    from threading import Lock
    from concurrent.futures import ThreadPoolExecutor
    from torbox_downloader.downloader import parse_size_to_bytes, is_file_size_matching
    import re
    
    torrent_success = True
    progress_lock = Lock()
    _MAX_LINK_RETRIES = 2  # Retry failed link generations up to 2 times
    
    # --- Route-based interception setup ---
    # Shared variable to capture the download URL from the requestdl API response.
    # Using a list so the route handler closure can mutate it.
    _intercepted_download_url = [None]
    
    def _capture_requestdl(route):
        """Intercept the requestdl API call, fetch the response ourselves,
        extract the CDN download URL, then fulfill the page's request normally."""
        try:
            response = route.fetch()
            try:
                body = response.json()
                if body.get("success") and body.get("data"):
                    _intercepted_download_url[0] = body["data"]
            except Exception:
                pass
            # Fulfill with original response (page JS will try to navigate to CDN URL,
            # but our CDN blocker below will prevent that)
            route.fulfill(response=response)
        except Exception:
            # If fetch fails, let the request proceed normally
            try:
                route.continue_()
            except Exception:
                pass
    
    def _block_cdn_navigation(route):
        """Block the page from navigating to CDN download URLs.
        This prevents the chrome-error:// crash that occurs when Chromium
        tries to render a binary download as a web page."""
        route.abort("blockedbyclient")
    
    # Register route handlers (LIFO order: last registered = checked first)
    # These must be registered BEFORE entering the download loop
    _routes_installed = False
    try:
        page.route("**/requestdl*", _capture_requestdl)
        page.route("**tb-cdn**", _block_cdn_navigation)
        _routes_installed = True
    except Exception as route_err:
        print(f"    Warning: Could not install route interceptors: {route_err}")
    
    # --- Page recovery helper (safety net) ---
    def _recover_page_if_crashed():
        """Detect if the browser tab has crashed and recover."""
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        
        is_crashed = (
            "chrome-error://" in current_url
            or current_url == "about:blank"
            or not current_url
        )
        
        if not is_crashed:
            return True
        
        nav_url = url or "https://torbox.app/download"
        print(f"    Page crashed (URL: {current_url}). Recovering...")
        
        try:
            if token:
                page.goto("https://torbox.app/login", wait_until="commit", timeout=15000)
                page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(token)})")
                page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(token)})")
            
            page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
            
            if "login" in page.url.lower():
                page.wait_for_timeout(2000)
                page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
            
            try:
                page.wait_for_selector("button[title='Download files'], button[title*='Download']", timeout=15000)
            except Exception:
                pass
            
            if torrent_name_for_status:
                try:
                    title_el = page.locator("p, span, div, h1, h2, h3, h4").filter(has_text=torrent_name_for_status).last
                    card = title_el
                    for _ in range(10):
                        card = card.locator("..")
                        if card.locator("button[title='Download files']").count() > 0:
                            break
                    btn = card.locator("button[title='Download files']").first
                    if btn.count() > 0:
                        btn.evaluate("el => el.click()")
                        try:
                            page.locator("button[title*='Options'], button[title*='options']").first.wait_for(state="visible", timeout=10000)
                        except Exception:
                            pass
                        print(f"    Recovery successful: re-expanded torrent card.")
                except Exception as re_err:
                    print(f"    Recovery warning: could not re-expand torrent card: {re_err}")
            
            return True
        except Exception as nav_err:
            print(f"    Recovery navigation failed: {nav_err}")
            return False
    
    # --- Link generation helper ---
    def _generate_download_link(name, size, selector):
        """Attempt to generate a download link for a single file.
        Returns the CDN download URL string, or None on failure."""
        row_locator = None
        
        def is_row_size_matching(row_text, expected_size_str):
            if not expected_size_str:
                return True
            match = re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|B))', row_text, re.I)
            if not match:
                return False
            row_size_str = match.group(1)
            expected_bytes = parse_size_to_bytes(expected_size_str)
            row_bytes = parse_size_to_bytes(row_size_str)
            if not expected_bytes or not row_bytes:
                return False
            ratio = row_bytes / expected_bytes
            return 0.7 <= ratio <= 1.3

        try:
            # Dismiss any previously open dropdown
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            
            # 1. Find the exact filename element
            file_label = page.locator("p, span, div").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$")).last
            if file_label.count() == 0:
                file_label = page.locator("p, span, div", has_text=name).last
            
            # Walk up to find the row container
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
            
            # 2. Size validation
            if size and row_locator.count() > 0:
                row_text = row_locator.text_content()
                if not is_row_size_matching(row_text, size):
                    candidates = page.locator("p, span, div", has_text=name)
                    cand_count = candidates.count()
                    for c_idx in range(cand_count):
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
        except Exception as find_err:
            print(f"    Failed to locate row using exact matching: {find_err}. Falling back to defaults...")
            
        # Fallback to CSS selector
        if not row_locator or row_locator.count() == 0:
            if selector:
                row_locator = page.locator(selector)
                if row_locator.count() > 1:
                    row_locator = page.locator("div", has_text=name).last
            
        if not row_locator or row_locator.count() == 0:
            print(f"    Could not locate row for file: {name} (Current URL: {page.url})")
            return None
            
        # 3. Find and click the options button
        dl_btn = row_locator.locator("button[title*='options'], button[title*='Options']").first
        if dl_btn.count() == 0:
            dl_btn = row_locator.locator("button[title*='download'], button[title*='Download']").first
        if dl_btn.count() == 0:
            buttons = row_locator.locator("button")
            btn_count = buttons.count()
            for b_idx in range(btn_count):
                btn_item = buttons.nth(b_idx)
                btn_html = btn_item.inner_html().lower()
                btn_title = (btn_item.get_attribute("title") or "").lower()
                if any(x in btn_html or x in btn_title for x in ['play', 'stream', 'watch', 'video']):
                    continue
                if "download" in btn_html or "svg" in btn_html or "icon" in btn_html or "dl" in btn_html:
                    dl_btn = btn_item
                    break
                    
        if not dl_btn or dl_btn.count() == 0:
            print(f"    Could not locate download options button for: {name}")
            return None
            
        dl_btn.evaluate("el => el.click()")
        page.wait_for_timeout(1000)
        
        # 4. Find and click the Direct Download button
        direct_dl_btn = row_locator.locator("button:visible, a:visible", has_text="Direct Download").first
        if direct_dl_btn.count() == 0:
            print(f"    Could not find 'Direct Download' button for: {name}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        
        direct_dl_btn.wait_for(state="visible", timeout=10000)
        
        # 5. Click Direct Download and wait for route-intercepted URL
        _intercepted_download_url[0] = None
        direct_dl_btn.evaluate("el => el.click()")
        
        # Poll for the intercepted URL (route handler fills it asynchronously)
        for _ in range(75):  # 75 * 200ms = 15 seconds max wait
            page.wait_for_timeout(200)
            if _intercepted_download_url[0]:
                break
        
        result_url = _intercepted_download_url[0]
        _intercepted_download_url[0] = None  # Reset for next file
        
        # Dismiss dropdown
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        
        if not result_url:
            print(f"    Could not retrieve download link for: {name}")
        
        return result_url
    
    # --- Progress tracking setup ---
    downloaded_bytes = {}
    total_bytes_map = {}
    speeds = {}
    
    for f_info in files_with_local_paths:
        fname = f_info['name']
        fsize = parse_size_to_bytes(f_info['size']) or 0
        total_bytes_map[fname] = fsize
        downloaded_bytes[fname] = 0
        speeds[fname] = 0
        
    def make_file_progress_callback(fname):
        def cb(filename, dl, tot, sp):
            with progress_lock:
                downloaded_bytes[fname] = dl
                total_bytes_map[fname] = tot
                speeds[fname] = sp
                
                if status_update_callback:
                    try:
                        status_update_callback(fname, 'downloading_file', dl, tot, sp)
                    except Exception as e:
                        if "aborted" in str(e).lower():
                            raise
                            
                    if torrent_name_for_status:
                        overall_dl = sum(downloaded_bytes.values())
                        overall_tot = sum(total_bytes_map.values())
                        overall_speed = sum(speeds.values())
                        percent = int((overall_dl / overall_tot * 100)) if overall_tot > 0 else 0
                        status_update_callback(torrent_name_for_status, 'downloading', percent, 100, overall_speed)
                    else:
                        status_update_callback(fname, 'downloading', dl, tot, sp)
        return cb

    def download_consumer(item):
        nonlocal torrent_success
        name, local_path, d_url, size = item
        
        if status_update_callback:
            try:
                status_update_callback("", "check_abort", 0, 0, 0)
            except Exception as e:
                if "aborted" in str(e).lower():
                    torrent_success = False
                    return
        
        print(f"    Starting download: {name}...")
        cb = make_file_progress_callback(name)
        success = download_file(d_url, local_path, expected_size_str=size, progress_callback=cb)
        
        if success:
            with progress_lock:
                downloaded_bytes[name] = total_bytes_map[name]
                speeds[name] = 0
            if status_update_callback:
                status_update_callback(name, 'completed_file', total_bytes_map[name], total_bytes_map[name], 0)
                if not torrent_name_for_status:
                    status_update_callback(name, 'completed', total_bytes_map[name], total_bytes_map[name], 0)
        else:
            torrent_success = False
            if status_update_callback:
                status_update_callback(name, 'failed_file', 0, 0, 0)
                if not torrent_name_for_status:
                    status_update_callback(name, 'failed', 0, 0, 0)

    # --- Main producer-consumer loop ---
    max_workers = min(max_parallel_files, len(files_with_local_paths))
    print(f"\nProcessing {len(files_with_local_paths)} files (Max {max_workers} parallel downloads)...")
    
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = []
    failed_files = []  # Track files that failed link generation for retry
    
    for f_idx, file_info in enumerate(files_with_local_paths):
        name = file_info['name']
        local_path = file_info['local_path']
        selector = file_info['selector']
        size = file_info['size']
        
        # Check abort
        if status_update_callback:
            try:
                status_update_callback("", "check_abort", 0, 0, 0)
            except Exception as e:
                if "aborted" in str(e).lower():
                    print("Queue aborted by user request.")
                    torrent_success = False
                    break
        
        # Check page health / recover from crash
        if not _recover_page_if_crashed():
            print(f"  [{f_idx + 1}/{len(files_with_local_paths)}] Skipping {name} (page unrecoverable).")
            torrent_success = False
            if status_update_callback and not torrent_name_for_status:
                status_update_callback(name, 'failed', 0, 0, 0)
            continue
        
        # Check for token expiry
        try:
            current_page_url = page.url.lower()
        except Exception:
            current_page_url = ""
        if "login" in current_page_url or page.locator("input[type='password']").count() > 0:
            print("Session token has expired or is invalid. Aborting download pipeline.")
            torrent_success = False
            raise Exception("Your Torbox session token is expired or invalid. Please log in again at torbox.app and paste a fresh token.")
            
        if status_update_callback and torrent_name_for_status:
            status_update_callback(torrent_name_for_status, 'downloading', int((f_idx / len(files_with_local_paths)) * 100), 100, sum(speeds.values()))
        
        # Skip already downloaded files
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            if is_file_size_matching(local_size, size):
                print(f"  [{f_idx + 1}/{len(files_with_local_paths)}] Skipping {name} (already downloaded).")
                expected_bytes = parse_size_to_bytes(size) or local_size
                with progress_lock:
                    downloaded_bytes[name] = expected_bytes
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'completed', expected_bytes, expected_bytes, 0)
                continue
        
        print(f"  [{f_idx + 1}/{len(files_with_local_paths)}] Generating link for: {name} ({size})")
        
        download_url = _generate_download_link(name, size, selector)
        
        if not download_url:
            # Queue for retry instead of failing immediately
            failed_files.append((f_idx, file_info))
            torrent_success = False
            if status_update_callback and not torrent_name_for_status:
                status_update_callback(name, 'failed', 0, 0, 0)
            continue
        
        fut = executor.submit(download_consumer, (name, local_path, download_url, size))
        futures.append(fut)
    
    # --- Retry failed link generations ---
    if failed_files:
        print(f"\n  Retrying {len(failed_files)} failed link generation(s)...")
        
        for retry_attempt in range(1, _MAX_LINK_RETRIES + 1):
            if not failed_files:
                break
                
            still_failed = []
            # Wait before retrying to avoid rate limiting
            page.wait_for_timeout(3000)
            
            # Make sure page is in a good state before retrying
            if not _recover_page_if_crashed():
                print(f"    Page unrecoverable during retry. Skipping remaining files.")
                break
            
            for orig_idx, file_info in failed_files:
                name = file_info['name']
                local_path = file_info['local_path']
                selector = file_info['selector']
                size = file_info['size']
                
                # Skip if already downloaded by another path
                if os.path.exists(local_path):
                    local_size = os.path.getsize(local_path)
                    if is_file_size_matching(local_size, size):
                        continue
                
                print(f"  [Retry {retry_attempt}/{_MAX_LINK_RETRIES}] Generating link for: {name} ({size})")
                
                download_url = _generate_download_link(name, size, selector)
                
                if download_url:
                    fut = executor.submit(download_consumer, (name, local_path, download_url, size))
                    futures.append(fut)
                else:
                    still_failed.append((orig_idx, file_info))
            
            failed_files = still_failed
        
        if failed_files:
            print(f"    {len(failed_files)} file(s) failed after all retries: {', '.join(f[1]['name'] for f in failed_files)}")
            
    for fut in futures:
        try:
            fut.result()
        except Exception as e:
            print(f"    Download thread error: {e}")
            torrent_success = False
    
    executor.shutdown(wait=False)
    
    # Clean up route handlers
    if _routes_installed:
        try:
            page.unroute("**/requestdl*")
            page.unroute("**tb-cdn**")
        except Exception:
            pass
    
    return torrent_success


def get_torrent_files_structure(token, url, target_torrent_name, browser_type='chromium', headless=True):
    from playwright.sync_api import sync_playwright
    from torbox_downloader.detector import TorboxDetector
    
    with sync_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled"
        ]
        engine = getattr(p, browser_type)
        launch_args = browser_args if browser_type == 'chromium' else []
        
        try:
            browser = engine.launch(headless=headless, args=launch_args)
        except Exception:
            browser = engine.launch(headless=headless)
            
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
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
        
        if token:
            try:
                page.goto("https://torbox.app/login", wait_until="commit", timeout=15000)
                page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(token)})")
                page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(token)})")
            except Exception as e:
                print(f"Failed to inject session token during fetch: {e}")
                
        print(f"Fetch structure - Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # Handle auth handshake redirect
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            if token:
                page.wait_for_timeout(2000)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
        # Validate if we are still stuck on the login page (token expired or invalid)
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            raise Exception("Your Torbox session token is expired or invalid. Please log in again at torbox.app and paste a fresh token.")
        
        try:
            page.wait_for_selector("button[title='Download files'], button[title*='Download'], button[aria-label*='Download'], a[download]", timeout=15000)
        except Exception:
            pass
            
        html_content = page.content()
        detector = TorboxDetector(html_content)
        
        # Locate the card download button directly via the target torrent name
        title_el = page.locator("p, span, div, h1, h2, h3, h4").filter(has_text=target_torrent_name).last
        card = title_el
        for _ in range(10):
            card = card.locator("..")
            if card.locator("button[title='Download files']").count() > 0:
                break
        btn_to_click = card.locator("button[title='Download files']").first
        
        if btn_to_click.count() > 0:
            btn_to_click.evaluate("el => el.click()")
            try:
                page.locator("button[title*='Options'], button[title*='options'], button[aria-label*='Options'], button[aria-label*='options']").first.wait_for(state="visible", timeout=10000)
            except Exception:
                pass
                
        html_content = page.content()
        detector.set_html(html_content)
        files = detector.parse_structure()
        
        browser.close()
        return files

def run_downloader(url, dest_dir, headless=True, browser_type='chromium', token=None, delay=5, file_scraped_callback=None, status_update_callback=None, target_torrent_name=None, max_parallel_files=1, selected_files=None):
    """
    Runs the full automation workflow:
    1. Launches Playwright browser context.
    2. Navigates to the page and waits for DOM load.
    3. Parses the page DOM to build file tree and folder structures.
    4. Recreates folder structure locally.
    5. Automates parallel downloading.
    """
    with sync_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled"
        ]
        
        browser = None
        context = None
        
        print(f"Launching clean {browser_type} context...")
        engine = getattr(p, browser_type)
        
        # Select launch arguments based on browser type
        launch_args = browser_args if browser_type == 'chromium' else []
        
        try:
            browser = engine.launch(headless=headless, args=launch_args)
            print(f"Successfully launched {browser_type}.")
        except Exception as e:
            logger.warning(f"Could not launch browser: {e}. Falling back to default launch.")
            browser = engine.launch(headless=headless)
        
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            permissions=['clipboard-read', 'clipboard-write']
        )
        
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
        
        if token:
            print(f"Injecting session token into localStorage for Torbox...")
            try:
                page.goto("https://torbox.app/login", wait_until="commit", timeout=15000)
                
                page.evaluate(f"localStorage.setItem('sb-db-auth-token', {json.dumps(token)})")
                page.evaluate(f"localStorage.setItem('torbox_session_token', {json.dumps(token)})")
                print("Session token successfully injected.")
                
                val = page.evaluate("localStorage.getItem('sb-db-auth-token')")
                if val:
                    print(f"Verified: token is in localStorage (length: {len(val)} characters).")
                else:
                    print("Warning: verification failed, localStorage is empty!")
                    
            except Exception as e:
                print(f"Failed to inject session token: {e}")
                
        print(f"Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        # Handle auth handshake redirect
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            if token:
                page.wait_for_timeout(2000)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
        # Validate if we are still stuck on the login page (token expired or invalid)
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            raise Exception("Your Torbox session token is expired or invalid. Please log in again at torbox.app and paste a fresh token.")
        
        # Wait for either the dashboard list or active files list to render
        print("Analyzing current page state...")
        try:
            page.wait_for_selector("button[title='Download files'], button[title*='Download'], button[aria-label*='Download'], a[download]", timeout=15000)
        except Exception:
            pass
        
        # Get active page HTML
        html_content = page.content()
        
        # Parse layout using TorboxDetector
        detector = TorboxDetector(html_content)
        files = detector.parse_structure()
        
        # If no files are detected, we check if we are on the dashboard with torrent cards
        if not files:
            torrents = detector.parse_torrents()
            if torrents:
                if target_torrent_name:
                    # Filter to target torrent only
                    filtered = [t for t in torrents if target_torrent_name.lower() in t['name'].lower()]
                    if filtered:
                        torrents = filtered
                    else:
                        print(f"Target torrent not found: {target_torrent_name}")
                        torrents = []
                
                print(f"Detected {len(torrents)} torrent folders to expand and download.")
                if file_scraped_callback:
                    file_scraped_callback(torrents)
                    
                for idx, torrent in enumerate(torrents):
                    # Check for abort flag in callbacks
                    if status_update_callback:
                        status_update_callback(torrent['name'], 'downloading', 0, 100, 0)
                        
                    print(f"\n[{idx + 1}/{len(torrents)}] Expanding torrent: {torrent['name']}")
                    btn_to_click = None
                    try:
                        title_el = page.locator("p, span, div, h1, h2, h3, h4").filter(has_text=torrent['name']).last
                        card = title_el
                        for _ in range(10):
                            card = card.locator("..")
                            if card.locator("button[title='Download files']").count() > 0:
                                break
                        btn_to_click = card.locator("button[title='Download files']").first
                        if btn_to_click.count() > 0:
                            btn_to_click.evaluate("el => el.click()")
                            try:
                                page.locator("button[title*='Options'], button[title*='options'], button[aria-label*='Options'], button[aria-label*='options']").first.wait_for(state="visible", timeout=10000)
                            except Exception as wait_err:
                                print(f"Timeout waiting for torrent details page: {wait_err}")
                        else:
                            print(f"Could not locate download button for torrent: {torrent['name']}")
                    except Exception as exp_err:
                        print(f"Could not click expand button: {exp_err}")
                        
                    card_html = page.content()
                    detector.set_html(card_html)
                    files = detector.parse_structure()
                    
                    if selected_files is not None:
                        files = [f for f in files if "/".join(f.get('path', []) + [f['name']]) in selected_files]
                        
                    if not files:
                        print(f"No matching files selected inside torrent: {torrent['name']}")
                        if status_update_callback:
                            status_update_callback(torrent['name'], 'failed', 0, 100, 0)
                        continue
                    print(f"Scraped {len(files)} matching/selected files inside {torrent['name']}.")
                    files_with_local_paths = create_folder_structure(dest_dir, files)
                    
                    torrent_success = download_files_pipeline(
                        page=page,
                        dest_dir=dest_dir,
                        files_with_local_paths=files_with_local_paths,
                        max_parallel_files=max_parallel_files,
                        status_update_callback=status_update_callback,
                        torrent_name_for_status=torrent['name'],
                        url=url,
                        token=token
                    )
                    
                    try:
                        # Navigate back to dashboard to reset the SPA view for next torrents
                        page.goto(url, wait_until="load", timeout=30000)
                        page.wait_for_timeout(2000)
                    except:
                        pass
                        
                    if status_update_callback:
                        if torrent_success:
                            status_update_callback(torrent['name'], 'completed', 100, 100, 0)
                        else:
                            status_update_callback(torrent['name'], 'failed', 0, 100, 0)
                            
                print(f"\nAll torrent folder queues completed.")
                try:
                    if context:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser and browser.is_connected():
                        browser.close()
                except Exception:
                    pass
                return

            else:
                if headless:
                    print("\n[!] No files or torrent cards detected in headless mode.")
                    browser.close()
                    return
                else:
                    print("\n[*] Headed browser active.")
                    print("[*] Please navigate/log in and click on the torrent you want to download in the browser window.")
                    print("[*] Once the files list is visible on the page, press ENTER here to start parsing & downloading...")
                    input()
                    
                    html_content = page.content()
                    detector.set_html(html_content)
                    files = detector.parse_structure()
                
        if selected_files is not None and files:
            files = [f for f in files if "/".join(f.get('path', []) + [f['name']]) in selected_files]
            
        if not files:
            print("[!] Could not detect any matching/selected files on the page.")
            print("Please make sure you are on the Torbox torrent download detail page (files list visible) and have valid file selections.")
            debug_file = "torbox_debug.html"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"Saved page HTML to {debug_file} for debugging.")
            browser.close()
            return
            
        print(f"Detected {len(files)} matching/selected files and folder structures:")
        for idx, f in enumerate(files):
            path_str = " / ".join(f['path'])
            print(f"  [{idx + 1}] {path_str} / {f['name']} ({f['size']})")
            
        # Recreate folders locally
        print("\nRecreating local folder architecture...")
        files_with_local_paths = create_folder_structure(dest_dir, files)
        
        if file_scraped_callback:
            file_scraped_callback(files_with_local_paths)
        
        # Start parallel downloading
        print("\nStarting parallel download queue...")
        torrent_success = download_files_pipeline(
            page=page,
            dest_dir=dest_dir,
            files_with_local_paths=files_with_local_paths,
            max_parallel_files=max_parallel_files,
            status_update_callback=status_update_callback,
            torrent_name_for_status=None,
            url=url,
            token=token
        )
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser and browser.is_connected():
                browser.close()
        except Exception:
            pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Torbox Folder-Structure Downloader GUI")
    parser.add_argument('--url', type=str, default="https://torbox.app/download", help="URL of the Torbox download page (default: https://torbox.app/download)")
    parser.add_argument('--dir', type=str, default="downloads", help="Local directory to recreate folder structure and save files")
    parser.add_argument('--no-headless', action='store_false', dest='headless', help="Run browser in headed mode (shows Chrome window for manual login)")
    parser.add_argument('--browser', '-b', type=str, default='chromium', choices=['chromium', 'firefox', 'webkit'], help="Browser engine to use (default: chromium)")
    parser.add_argument('--token', '-t', type=str, default=None, help="Torbox localStorage session token (torbox_session_token) to bypass Cloudflare Turnstile/Login redirects")
    parser.add_argument('--delay', type=int, default=5, help="Delay in seconds between downloads")
    parser.add_argument('--no-gui', action='store_false', dest='gui', help="Disable launching the Web GUI dashboard and run CLI directly")
    parser.add_argument('--parallel-files', type=int, default=1, help="Max number of parallel file downloads")
    
    args = parser.parse_args()
    
    # Check for token file
    token_val = args.token
    default_token_file = 'token.txt'
    if not token_val and os.path.exists(default_token_file):
        token_val = default_token_file
        print(f"Found default token file '{default_token_file}' in workspace. Loading it.")
        
    if token_val and os.path.exists(token_val):
        try:
            with open(token_val, 'r', encoding='utf-8') as f:
                token_val = f.read().strip()
            print(f"Successfully loaded token from file.")
        except Exception as e:
            print(f"[!] Failed to read token file: {e}")

    # Launch GUI by default unless --no-gui is specified
    if args.gui:
        from gui_server import start_gui_server
        start_gui_server(token=token_val, dest_dir=args.dir, delay=args.delay, browser_type=args.browser, headless=args.headless)
    else:
        run_downloader(
            url=args.url,
            dest_dir=args.dir,
            headless=args.headless,
            browser_type=args.browser,
            token=token_val,
            delay=args.delay,
            max_parallel_files=args.parallel_files
        )
