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

def download_files_pipeline(page, dest_dir, files_with_local_paths, max_parallel_files, status_update_callback=None, torrent_name_for_status=None):
    from threading import Lock
    from concurrent.futures import ThreadPoolExecutor
    from torbox_downloader.downloader import parse_size_to_bytes
    
    torrent_success = True
    progress_lock = Lock()
    
    downloaded_bytes = {}
    total_bytes_map = {}
    speeds = {}
    
    # Pre-populate sizes for accurate total percentage calculations
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
                    # Notify GUI server of individual file progress
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
                        # Fallback for single-file details view
                        status_update_callback(fname, 'downloading', dl, tot, sp)
        return cb

    def download_consumer(item):
        nonlocal torrent_success
        name, local_path, d_url, size = item
        
        # Check abort flag before starting download
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

    # Producer-consumer: main thread generates links, pool downloads them
    max_workers = min(max_parallel_files, len(files_with_local_paths))
    print(f"\nProcessing {len(files_with_local_paths)} files (Max {max_workers} parallel downloads)...")
    
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = []
    
    for f_idx, file_info in enumerate(files_with_local_paths):
        name = file_info['name']
        local_path = file_info['local_path']
        selector = file_info['selector']
        size = file_info['size']
        
        if status_update_callback:
            try:
                status_update_callback("", "check_abort", 0, 0, 0)
            except Exception as e:
                if "aborted" in str(e).lower():
                    print("Queue aborted by user request.")
                    torrent_success = False
                    break
        
        if status_update_callback and torrent_name_for_status:
            status_update_callback(torrent_name_for_status, 'downloading', int((f_idx / len(files_with_local_paths)) * 100), 100, sum(speeds.values()))
        
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            expected_bytes = parse_size_to_bytes(size)
            if expected_bytes and abs(local_size - expected_bytes) < 1024 * 1024:
                print(f"  [{f_idx + 1}/{len(files_with_local_paths)}] Skipping {name} (already downloaded).")
                with progress_lock:
                    downloaded_bytes[name] = expected_bytes
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'completed', expected_bytes, expected_bytes, 0)
                continue
        
        print(f"  [{f_idx + 1}/{len(files_with_local_paths)}] Generating link for: {name} ({size})")
        
        download_url = None
        try:
            row_locator = None
            if selector:
                row_locator = page.locator(selector)
            if not row_locator or row_locator.count() == 0:
                row_locator = page.locator("div", has_text=name).last
                
            if row_locator.count() == 0:
                print(f"    Could not locate row for file: {name}")
                torrent_success = False
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'failed', 0, 0, 0)
                continue
                
            dl_btn = row_locator.locator("button[title*='options'], button[title*='download'], button[title*='Options']").first
            if dl_btn.count() == 0:
                dl_btn = None
                
            if not dl_btn:
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
                        
            if not dl_btn:
                dl_btn = row_locator.locator("svg").last
                
            if not dl_btn:
                print(f"    Could not locate download options button for: {name}")
                torrent_success = False
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'failed', 0, 0, 0)
                continue
                
            dl_btn.evaluate("el => el.click()")
            page.wait_for_timeout(1000)
            
            direct_dl_btn = page.locator("button:visible, a:visible", has_text="Direct Download").first
            if direct_dl_btn.count() > 0:
                direct_dl_btn.wait_for(state="visible", timeout=10000)
                with page.expect_download(timeout=60000) as download_info:
                    direct_dl_btn.evaluate("el => el.click()")
                download = download_info.value
                download_url = download.url
                download.cancel()
                
            if not download_url:
                print("    Could not retrieve download link.")
                torrent_success = False
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'failed', 0, 0, 0)
                try:
                    dl_btn.evaluate("el => el.click()")
                    page.wait_for_timeout(500)
                except:
                    pass
                continue
                
            try:
                dl_btn.evaluate("el => el.click()")
                page.wait_for_timeout(500)
            except:
                pass
                
        except Exception as e:
            print(f"    Failed to generate download link: {e}")
            torrent_success = False
            if status_update_callback and not torrent_name_for_status:
                status_update_callback(name, 'failed', 0, 0, 0)
            continue
        
        if download_url:
            fut = executor.submit(download_consumer, (name, local_path, download_url, size))
            futures.append(fut)
            
    for fut in futures:
        try:
            fut.result()
        except Exception as e:
            print(f"    Download thread error: {e}")
            torrent_success = False
            
    executor.shutdown(wait=False)
    return torrent_success

def run_downloader(url, dest_dir, headless=True, browser_type='chromium', token=None, delay=5, file_scraped_callback=None, status_update_callback=None, target_torrent_name=None, max_parallel_files=1):
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
            if browser_type == 'chromium':
                browser = engine.launch(
                    headless=headless,
                    channel="chrome",
                    args=launch_args
                )
                print("Successfully launched system Google Chrome.")
            else:
                browser = engine.launch(headless=headless)
                print(f"Successfully launched {browser_type}.")
        except Exception as e:
            logger.warning(f"Could not launch browser with default channel: {e}. Falling back to default launch.")
            browser = engine.launch(headless=headless)
        
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            permissions=['clipboard-read', 'clipboard-write']
        )
        
        page = context.new_page()
        
        if token:
            print(f"Injecting session token into localStorage for Torbox...")
            try:
                page.goto("https://torbox.app/login", wait_until="load", timeout=30000)
                page.wait_for_timeout(1000)
                
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
        page.goto(url, wait_until="load", timeout=60000)
        
        if token:
            print("Waiting for authentication handshake...")
            page.wait_for_timeout(3000)
        
        # Check if we are on login page
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            if token:
                print("[*] Detected redirect to login page. Attempting token authentication reload...")
                page.wait_for_timeout(2000)
                # Try navigating to target url again
                print(f"Re-navigating to: {url}")
                page.goto(url, wait_until="load", timeout=60000)
                page.wait_for_timeout(2000)
                
        # Check if we are still on login page
        if "login" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            print("\n[!] Detected Torbox Login Page.")
            if headless:
                print("Cannot log in headlessly. Re-run this command with the --no-headless flag to log in manually first.")
                browser.close()
                return
            else:
                print("[*] Please log in to your Torbox account in the opened browser window.")
                print("[*] After logging in and navigating back to the torrent files page, press ENTER here to continue...")
                input()
                # Re-check URL
                if "login" in page.url.lower():
                    print("Still on login page. Exiting.")
                    browser.close()
                    return
        
        # Wait for the files container or content list to render
        print("Analyzing current page state...")
        page.wait_for_timeout(2000)
        
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
                        # Locate correct card button by finding ancestor containing torrent name
                        buttons = page.locator("button[title='Download files']")
                        count = buttons.count()
                        for b_idx in range(count):
                            candidate_btn = buttons.nth(b_idx)
                            ancestor = candidate_btn.locator("xpath=ancestor::div[contains(@class, 'flex')][3]")
                            if ancestor.count() > 0:
                                ancestor_text = ancestor.first.inner_text()
                                if torrent['name'].lower() in ancestor_text.lower():
                                    btn_to_click = candidate_btn
                                    break
                                    
                        if btn_to_click:
                            btn_to_click.evaluate("el => el.click()")
                            try:
                                page.locator("button[title*='options'], button[title*='Options']").first.wait_for(state="visible", timeout=10000)
                            except Exception as wait_err:
                                print(f"Timeout waiting for torrent details page: {wait_err}")
                        else:
                            print(f"Could not locate download button for torrent: {torrent['name']}")
                    except Exception as exp_err:
                        print(f"Could not click expand button: {exp_err}")
                        
                    card_html = page.content()
                    detector.set_html(card_html)
                    files = detector.parse_structure()
                    
                    if not files:
                        print(f"No files found inside torrent: {torrent['name']}")
                        if status_update_callback:
                            status_update_callback(torrent['name'], 'failed', 0, 100, 0)
                        continue
                    print(f"Scraped {len(files)} files inside {torrent['name']}.")
                    files_with_local_paths = create_folder_structure(dest_dir, files)
                    
                    torrent_success = download_files_pipeline(
                        page=page,
                        dest_dir=dest_dir,
                        files_with_local_paths=files_with_local_paths,
                        max_parallel_files=max_parallel_files,
                        status_update_callback=status_update_callback,
                        torrent_name_for_status=torrent['name']
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
                
        if not files:
            print("[!] Could not detect any files on the page.")
            print("Please make sure you are on the Torbox torrent download detail page (files list visible).")
            # Let's save page HTML for debugging
            debug_file = "torbox_debug.html"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html_content)
            print(f"Saved page HTML to {debug_file} for debugging.")
            browser.close()
            return
            
        print(f"Detected {len(files)} files and folder structures:")
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
            torrent_name_for_status=None
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
