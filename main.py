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

def download_files_pipeline(session, dest_dir, files_with_local_paths, max_parallel_files, status_update_callback=None, torrent_name_for_status=None, url=None, token=None, delay=0, parallel_connections=True, cancel_event=None):
    from threading import Lock, Semaphore, Thread
    from concurrent.futures import ThreadPoolExecutor
    import queue
    import shutil
    from torbox_downloader.downloader import parse_size_to_bytes, is_file_size_matching, download_file

    torrent_success = True
    progress_lock = Lock()
    
    # --- Disk-space precheck ---
    total_expected_bytes = 0
    for f_info in files_with_local_paths:
        name = f_info['name']
        local_path = f_info['local_path']
        size = f_info['size']
        if os.path.exists(local_path):
            local_size = os.path.getsize(local_path)
            if is_file_size_matching(local_size, size):
                continue
        total_expected_bytes += parse_size_to_bytes(size) or 0
        
    try:
        os.makedirs(dest_dir, exist_ok=True)
        total, used, free = shutil.disk_usage(dest_dir)
        if free < total_expected_bytes:
            err_msg = f"Insufficient disk space in {dest_dir}. Required: {total_expected_bytes / (1024**3):.2f} GB, Free: {free / (1024**3):.2f} GB."
            logger.error(err_msg)
            if status_update_callback:
                status_update_callback("", "error", 0, 0, 0)
            raise IOError(err_msg)
    except Exception as e:
        if isinstance(e, IOError):
            raise
        pass

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

    # Pre-compute the total once — total_bytes_map doesn't change after setup.
    _overall_tot = sum(total_bytes_map.values())
    # Running counter updated by delta so we never call sum(downloaded_bytes.values())
    # inside the hot progress callback path.
    _cumul_dl = [0]

    def make_file_progress_callback(fname):
        def cb(filename, dl, tot, sp):
            with progress_lock:
                prev = downloaded_bytes[fname]
                _cumul_dl[0] += dl - prev        # O(1) delta update
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
                        overall_speed = sum(speeds.values())
                        percent = int(_cumul_dl[0] / _overall_tot * 100) if _overall_tot > 0 else 0
                        status_update_callback(torrent_name_for_status, 'downloading', percent, 100, overall_speed)
                    else:
                        status_update_callback(fname, 'downloading', dl, tot, sp)
        return cb

    # --- Link Prefetch Producer-Consumer Queue ---
    total_files = len(files_with_local_paths)
    max_workers = min(max_parallel_files, total_files)
    prefetch_queue = queue.Queue(maxsize=max_workers + 2)
    slot = Semaphore(max_workers)
    
    def link_producer():
        nonlocal torrent_success
        try:
            for f_idx, f_info in enumerate(files_with_local_paths):
                if cancel_event and cancel_event.is_set():
                    break
                
                name = f_info['name']
                local_path = f_info['local_path']
                selector = f_info['selector']
                size = f_info['size']
                
                if os.path.exists(local_path):
                    local_size = os.path.getsize(local_path)
                    if is_file_size_matching(local_size, size):
                        prefetch_queue.put((f_idx, f_info, "skip", None))
                        continue
                        
                print(f"  [{f_idx + 1}/{total_files}] Generating link for: {name} ({size})")
                
                download_url = None
                # Retry generating link up to 3 times with backoff
                for attempt in range(3):
                    if cancel_event and cancel_event.is_set():
                        break
                    try:
                        download_url = session.generate_download_link(name, size, selector)
                        if download_url:
                            break
                    except Exception as e:
                        print(f"    Link generation attempt {attempt + 1} failed: {e}")
                    time.sleep(2)
                    
                if not download_url:
                    prefetch_queue.put((f_idx, f_info, "fail", None))
                else:
                    prefetch_queue.put((f_idx, f_info, "download", download_url))
        except Exception as e:
            logger.error(f"Link producer exception: {e}")
        finally:
            prefetch_queue.put(None)

    def download_consumer(item):
        nonlocal torrent_success
        try:
            name, local_path, d_url, size = item
            
            if status_update_callback:
                try:
                    status_update_callback("", "check_abort", 0, 0, 0)
                except Exception as e:
                    if "aborted" in str(e).lower():
                        torrent_success = False
                        return

            if cancel_event and cancel_event.is_set():
                torrent_success = False
                return

            print(f"    Starting download: {name}...")
            cb = make_file_progress_callback(name)
            success = download_file(d_url, local_path, expected_size_str=size, progress_callback=cb, parallel=parallel_connections, cancel_event=cancel_event)

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
        finally:
            slot.release()

    print(f"\nProcessing {total_files} files (Max {max_workers} parallel downloads)...")
    
    # Start prefetch producer thread
    producer_thread = Thread(target=link_producer, daemon=True)
    producer_thread.start()

    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = []
    submitted_count = [0]

    try:
        while True:
            if cancel_event and cancel_event.is_set():
                torrent_success = False
                break
                
            item = prefetch_queue.get()
            if item is None:
                break
                
            f_idx, f_info, action, download_url = item
            name = f_info['name']
            local_path = f_info['local_path']
            size = f_info['size']
            
            if status_update_callback:
                try:
                    status_update_callback("", "check_abort", 0, 0, 0)
                except Exception as e:
                    if "aborted" in str(e).lower():
                        torrent_success = False
                        break
            
            if action == "skip":
                print(f"  [{f_idx + 1}/{total_files}] Skipping {name} (already downloaded).")
                expected_bytes = parse_size_to_bytes(size) or os.path.getsize(local_path)
                with progress_lock:
                    downloaded_bytes[name] = expected_bytes
                    _cumul_dl[0] += expected_bytes  # keep overall % accurate for skipped files
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'completed', expected_bytes, expected_bytes, 0)
                continue
                
            elif action == "fail":
                torrent_success = False
                if status_update_callback and not torrent_name_for_status:
                    status_update_callback(name, 'failed', 0, 0, 0)
                continue
                
            # Wait for a download worker slot
            slot.acquire()
            if cancel_event and cancel_event.is_set():
                slot.release()
                torrent_success = False
                break
                
            fut = executor.submit(download_consumer, (name, local_path, download_url, size))
            futures.append(fut)
            submitted_count[0] += 1
            if status_update_callback and torrent_name_for_status:
                status_update_callback(torrent_name_for_status, 'downloading', int((submitted_count[0] / total_files) * 100), 100, sum(speeds.values()))

        # Wait for threads to finish
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                print(f"    Download thread error: {e}")
                torrent_success = False
    finally:
        executor.shutdown(wait=True)
        
    return torrent_success


def get_torrent_files_structure(token, url, target_torrent_name, browser_type='chromium', headless=True):
    from torbox_downloader.browser_session import TorboxBrowserSession
    session = TorboxBrowserSession(token, browser_type, headless, download_url=url)
    session.start()
    try:
        files = session.fetch_structure(target_torrent_name)
        return files
    finally:
        session.stop()


def run_downloader(url, dest_dir, headless=True, browser_type='chromium', token=None, delay=5, file_scraped_callback=None, status_update_callback=None, target_torrent_name=None, max_parallel_files=1, selected_files=None, parallel_connections=True, cancel_event=None, session=None):
    """
    Runs the full automation workflow:
    1. Launches/uses TorboxBrowserSession.
    2. Navigates to the page and parses the structure.
    3. Recreates folder structure locally.
    4. Automates parallel prefetching downloads.
    """
    local_session = None
    if session is None:
        from torbox_downloader.browser_session import TorboxBrowserSession
        print(f"Launching clean {browser_type} context...")
        local_session = TorboxBrowserSession(token, browser_type, headless, download_url=url)
        local_session.start()
        session = local_session

    try:
        print("Analyzing current page state...")
        # Try to parse structure directly (if we are on the files list page already)
        html_content = session.get_page_content()
        detector = TorboxDetector(html_content)
        files = detector.parse_structure()

        # If no files are detected, we check if we are on the dashboard with torrent cards
        if not files:
            torrents = session.scan_torrents()
            if torrents:
                if target_torrent_name:
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
                    if cancel_event and cancel_event.is_set():
                        break
                    if status_update_callback:
                        status_update_callback(torrent['name'], 'downloading', 0, 100, 0)

                    print(f"\n[{idx + 1}/{len(torrents)}] Expanding torrent: {torrent['name']}")
                    try:
                        files = session.fetch_structure(torrent['name'])
                    except Exception as exp_err:
                        print(f"Could not expand torrent card: {exp_err}")
                        files = []

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
                        session=session,
                        dest_dir=dest_dir,
                        files_with_local_paths=files_with_local_paths,
                        max_parallel_files=max_parallel_files,
                        status_update_callback=status_update_callback,
                        torrent_name_for_status=torrent['name'],
                        url=url,
                        token=token,
                        delay=delay,
                        parallel_connections=parallel_connections,
                        cancel_event=cancel_event
                    )

                    try:
                        # Navigate back to dashboard to reset the SPA view for next torrents
                        session.go_to_dashboard()
                    except:
                        pass

                    if status_update_callback:
                        if torrent_success:
                            status_update_callback(torrent['name'], 'completed', 100, 100, 0)
                        else:
                            status_update_callback(torrent['name'], 'failed', 0, 100, 0)

                print(f"\nAll torrent folder queues completed.")
                return

            else:
                if headless:
                    print("\n[!] No files or torrent cards detected in headless mode.")
                    return
                else:
                    print("\n[*] Headed browser active.")
                    print("[*] Please navigate/log in and click on the torrent you want to download in the browser window.")
                    print("[*] Once the files list is visible on the page, press ENTER here to start parsing & downloading...")
                    input()

                    html_content = session.get_page_content()
                    detector.set_html(html_content)
                    files = detector.parse_structure()

        if selected_files is not None and files:
            files = [f for f in files if "/".join(f.get('path', []) + [f['name']]) in selected_files]

        if not files:
            print("[!] Could not detect any matching/selected files on the page.")
            print("Please make sure you are on the Torbox torrent download detail page (files list visible) and have valid file selections.")
            debug_file = "torbox_debug.html"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(session.get_page_content())
            print(f"Saved page HTML to {debug_file} for debugging.")
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
            session=session,
            dest_dir=dest_dir,
            files_with_local_paths=files_with_local_paths,
            max_parallel_files=max_parallel_files,
            status_update_callback=status_update_callback,
            torrent_name_for_status=None,
            url=url,
            token=token,
            delay=delay,
            parallel_connections=parallel_connections,
            cancel_event=cancel_event
        )
    finally:
        if local_session:
            try:
                local_session.stop()
            except:
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
