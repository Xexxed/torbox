import os
import time
import requests
import logging
from tqdm import tqdm
import re
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

def parse_size_to_bytes(size_str):
    """
    Helper to convert sizes like "2GB", "304KB", "45B" to bytes.
    """
    if not size_str:
        return None
    
    match = re.match(r'^([\d.]+)\s*(GB|MB|KB|B)$', size_str.strip(), re.I)
    if not match:
        return None
        
    value, unit = match.groups()
    value = float(value)
    unit = unit.upper()
    
    units = {
        'B': 1,
        'KB': 1024,
        'MB': 1024 ** 2,
        'GB': 1024 ** 3
    }
    
    return int(value * units[unit])

def _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback, cancel_event=None):
    # Ensure parent directory exists
    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
        
    session = requests.Session()
    attempt = 0
    while attempt < retries:
        if cancel_event and cancel_event.is_set():
            logger.info("Download cancelled before single-threaded download start.")
            return False
        try:
            resume_header = {}
            existing_bytes = 0
            if os.path.exists(part_path):
                existing_bytes = os.path.getsize(part_path)
                resume_header = {'Range': f'bytes={existing_bytes}-'}
                logger.info(f"Resuming download from byte {existing_bytes}")
                
            merged_headers = {**headers, **resume_header}
            response = session.get(url, headers=merged_headers, stream=True, timeout=30)
            
            if response.status_code not in [200, 206]:
                if response.status_code == 416 and existing_bytes > 0:
                    try:
                        os.remove(part_path)
                    except:
                        pass
                    existing_bytes = 0
                    response = session.get(url, headers=headers, stream=True, timeout=30)
                else:
                    response.raise_for_status()
            
            mode = 'ab' if (response.status_code == 206 and existing_bytes > 0) else 'wb'
            stream_total = int(response.headers.get('content-length', 0)) + existing_bytes
            if stream_total == existing_bytes and total_size > 0:
                stream_total = total_size

            desc = os.path.basename(local_path)
            if len(desc) > 30:
                desc = desc[:27] + "..."
                
            last_update_time = 0.0
            update_interval = 0.25 # update GUI at most every 250ms
            
            with open(part_path, mode) as f:
                with tqdm(total=stream_total, initial=existing_bytes, unit='B', unit_scale=True, desc=desc, leave=True) as pbar:
                    downloaded = existing_bytes
                    start_time = time.time()
                    for chunk in response.iter_content(chunk_size=512 * 1024): # 512KB chunks
                        if cancel_event and cancel_event.is_set():
                            raise IOError("Aborted by user")
                        if chunk:
                            f.write(chunk)
                            chunk_len = len(chunk)
                            pbar.update(chunk_len)
                            downloaded += chunk_len
                            if progress_callback:
                                now = time.time()
                                if now - last_update_time >= update_interval or downloaded >= stream_total:
                                    last_update_time = now
                                    elapsed = now - start_time
                                    speed = (downloaded - existing_bytes) / elapsed if elapsed > 0 else 0
                                    progress_callback(os.path.basename(local_path), downloaded, stream_total, speed)
                                    
            downloaded_size = os.path.getsize(part_path)
            if stream_total > 0 and downloaded_size != stream_total:
                raise IOError(f"Integrity check failed: downloaded {downloaded_size} bytes, expected {stream_total} bytes")

            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except:
                    pass
            os.rename(part_path, local_path)
            print(f"Successfully downloaded: {os.path.basename(local_path)}")
            return True
            
        except (requests.RequestException, IOError) as e:
            if (cancel_event and cancel_event.is_set()) or "aborted" in str(e).lower():
                print(f"Download of {os.path.basename(local_path)} aborted.")
                return False
            attempt += 1
            wait_time = backoff_factor ** attempt
            print(f"Error downloading {os.path.basename(local_path)}: {e}")
            if attempt < retries:
                print(f"Retrying in {wait_time}s (Attempt {attempt + 1}/{retries})...")
                slept = 0.0
                while slept < wait_time:
                    if cancel_event and cancel_event.is_set():
                        return False
                    time.sleep(0.2)
                    slept += 0.2
            else:
                print(f"Failed to download {os.path.basename(local_path)} after {retries} attempts.")
                return False

def is_file_size_matching(local_size, expected_size_str):
    """
    Checks if local_size is close enough to expected_size_str.
    Allows 1MB difference for small files (<10MB), and 50%-200% tolerance for large files (>=10MB)
    to handle rounded size strings or uploader database discrepancies in the UI.
    """
    expected_bytes = parse_size_to_bytes(expected_size_str)
    if not expected_bytes:
        return False
    if expected_bytes < 10 * 1024 * 1024:
        return abs(local_size - expected_bytes) < 1024 * 1024
    ratio = local_size / expected_bytes
    return 0.5 <= ratio <= 2.0

def download_file(url, local_path, expected_size_str=None, headers=None, retries=5, backoff_factor=2, progress_callback=None, parallel=True, num_threads=4, cancel_event=None):
    """
    Downloads a file using parallel segmented chunk downloads (similar to IDM)
    if the server supports it, otherwise falls back to single-threaded chunked downloads.
    """
    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        if is_file_size_matching(local_size, expected_size_str):
            print(f"Skipping {os.path.basename(local_path)} (already downloaded)")
            expected_bytes = parse_size_to_bytes(expected_size_str) or local_size
            if progress_callback:
                progress_callback(os.path.basename(local_path), expected_bytes, expected_bytes, 0)
            return True
            
    part_path = local_path + ".part"
    
    if headers is None:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
    total_size = 0
    accept_ranges = False
    try:
        r = requests.head(url, headers=headers, timeout=15)
        if r.status_code not in [200, 206] or not r.headers.get('content-length'):
            r = requests.get(url, headers=headers, stream=True, timeout=15)
            r.close()
        
        if r.status_code in [200, 206]:
            total_size = int(r.headers.get('content-length', 0))
            if expected_size_str and total_size > 0:
                expected_bytes = parse_size_to_bytes(expected_size_str)
                if expected_bytes:
                    is_match = False
                    if expected_bytes < 1024 * 1024:
                        is_match = abs(total_size - expected_bytes) < 512 * 1024
                    else:
                        ratio = total_size / expected_bytes
                        is_match = 0.5 <= ratio <= 2.0
                    
                    if not is_match:
                        print(f"Warning: Size mismatch for download. Expected {expected_size_str} ({expected_bytes} bytes), but server returned content-length of {total_size} bytes. Proceeding anyway.")

            accept_ranges = 'bytes' in r.headers.get('Accept-Ranges', '').lower() or r.status_code == 206
            if not accept_ranges and total_size > 1024 * 1024 * 1024:
                accept_ranges = True
    except Exception as e:
        logger.error(f"Error fetching headers: {e}")

    if total_size < 1024 * 1024 * 1024 or not accept_ranges or total_size == 0 or not parallel:
        return _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback, cancel_event)

    # Replicate IDM multi-connection speed boost (max 5 threads to reduce 429 rate limits)
    num_threads = max(1, min(int(num_threads), 5))
    segment_size = total_size // num_threads
    ranges = []
    for i in range(num_threads):
        start = i * segment_size
        end = total_size - 1 if i == num_threads - 1 else (start + segment_size - 1)
        ranges.append((start, end))

    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    # Check for existing segment files
    existing_segments = []
    if parent_dir:
        try:
            existing_segments = [f for f in os.listdir(parent_dir) if f.startswith(os.path.basename(part_path) + ".")]
        except Exception:
            pass
            
    existing_indices = []
    for f_name in existing_segments:
        try:
            idx = int(f_name.split('.')[-1])
            existing_indices.append(idx)
        except ValueError:
            pass
            
    # If the number of segment files doesn't match num_threads, or they are not sequential from 0 to num_threads-1,
    # clean them up and start fresh.
    should_clean = False
    if existing_indices:
        if len(existing_indices) != num_threads or set(existing_indices) != set(range(num_threads)):
            should_clean = True
            
    if should_clean:
        print("Number of connections changed or segment files mismatch. Cleaning up old segments...")
        for idx in existing_indices:
            try:
                os.remove(f"{part_path}.{idx}")
            except:
                pass

    # Calculate initially downloaded bytes from existing segment files
    initial_downloaded_bytes = 0
    for idx in range(num_threads):
        seg_path = f"{part_path}.{idx}"
        if os.path.exists(seg_path):
            initial_downloaded_bytes += os.path.getsize(seg_path)
            
    downloaded_lock = threading.Lock()
    downloaded_bytes = initial_downloaded_bytes
    start_time = time.time()
    errors = []

    desc = os.path.basename(local_path)
    if len(desc) > 30:
        desc = desc[:27] + "..."

    print(f"Connecting to download {os.path.basename(local_path)} using {num_threads} connections...")

    last_update_time = [0.0]
    update_interval = 0.25 # update GUI at most every 250ms

    def download_segment(segment_idx, start, end):
        nonlocal downloaded_bytes
        seg_path = f"{part_path}.{segment_idx}"
        
        seg_attempt = 0
        while seg_attempt < retries:
            if cancel_event and cancel_event.is_set():
                return False
            try:
                existing_bytes = 0
                if os.path.exists(seg_path):
                    existing_bytes = os.path.getsize(seg_path)
                
                # Check if we already finished this segment
                seg_total_size = end - start + 1
                if existing_bytes >= seg_total_size:
                    return True
                
                # Request only remaining bytes for this segment
                curr_start = start + existing_bytes
                seg_headers = {**headers, 'Range': f'bytes={curr_start}-{end}'}
                
                resp = requests.get(url, headers=seg_headers, stream=True, timeout=30)
                if resp.status_code not in [200, 206]:
                    if resp.status_code == 416 and existing_bytes > 0:
                        try:
                            os.remove(seg_path)
                        except:
                            pass
                        existing_bytes = 0
                        seg_headers = {**headers, 'Range': f'bytes={start}-{end}'}
                        resp = requests.get(url, headers=seg_headers, stream=True, timeout=30)
                        if resp.status_code not in [200, 206]:
                            resp.raise_for_status()
                    else:
                        resp.raise_for_status()
                
                mode = 'ab' if (resp.status_code == 206 and existing_bytes > 0) else 'wb'
                
                with open(seg_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=512 * 1024): # 512KB chunks
                        if cancel_event and cancel_event.is_set():
                            raise IOError("Aborted by user")
                        if chunk:
                            f.write(chunk)
                            chunk_len = len(chunk)
                            with downloaded_lock:
                                downloaded_bytes += chunk_len
                                if progress_callback:
                                    now = time.time()
                                    if now - last_update_time[0] >= update_interval or downloaded_bytes >= total_size:
                                        last_update_time[0] = now
                                        elapsed = now - start_time
                                        speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                                        progress_callback(os.path.basename(local_path), downloaded_bytes, total_size, speed)
                
                # Verify segment size
                if os.path.exists(seg_path) and os.path.getsize(seg_path) >= seg_total_size:
                    return True
                else:
                    raise IOError("Segment download incomplete")
            except Exception as e:
                if cancel_event and cancel_event.is_set() or "aborted" in str(e).lower() or "aborted" in str(getattr(e, '__context__', '')).lower():
                    errors.append(f"Aborted: {e}")
                    raise
                
                is_429 = False
                if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code == 429:
                    is_429 = True
                
                seg_attempt += 1
                if seg_attempt >= retries:
                    errors.append(f"Segment {segment_idx} failed: {e}")
                    return False

                if is_429:
                    retry_after_val = e.response.headers.get('Retry-After')
                    wait_time = 5
                    if retry_after_val:
                        try:
                            wait_time = int(retry_after_val)
                        except ValueError:
                            pass
                    else:
                        wait_time = 5 * (2 ** seg_attempt)
                    print(f"Warning: Rate limited (429) on segment {segment_idx} for {os.path.basename(local_path)}. Retrying in {wait_time}s...")
                else:
                    wait_time = backoff_factor ** seg_attempt

                slept = 0.0
                while slept < wait_time:
                    if cancel_event and cancel_event.is_set():
                        return False
                    time.sleep(0.2)
                    slept += 0.2
        return False

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(download_segment, idx, r[0], r[1]) for idx, r in enumerate(ranges)]
        
        with tqdm(total=total_size, initial=initial_downloaded_bytes, unit='B', unit_scale=True, desc=desc, leave=True) as pbar:
            last_val = initial_downloaded_bytes
            while True:
                with downloaded_lock:
                    curr = downloaded_bytes
                pbar.update(curr - last_val)
                last_val = curr
                if curr >= total_size or len(errors) > 0 or (cancel_event and cancel_event.is_set()):
                    break
                time.sleep(0.2)
            
            if len(errors) == 0 and not (cancel_event and cancel_event.is_set()):
                pbar.update(total_size - last_val)

    if (cancel_event and cancel_event.is_set()) or any("Aborted" in err for err in errors):
        print(f"Segmented download aborted: {os.path.basename(local_path)}")
        return False

    if errors:
        print(f"Segmented download failed: {errors[0]}. Falling back to single-threaded resume...")
        try:
            return _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback, cancel_event)
        except Exception as e:
            logger.error(f"Single-threaded fallback failed for {os.path.basename(local_path)}: {e}")
            return False

    # Merge segments sequentially to avoid disk thrashing and files lock contention
    print(f"Merging segments for {os.path.basename(local_path)}...")
    try:
        seg0_path = f"{part_path}.0"
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except:
                pass
        
        if os.path.exists(seg0_path):
            os.rename(seg0_path, part_path)
        else:
            raise IOError("Segment 0 file missing, cannot merge")

        with open(part_path, 'ab') as dest_f:
            for idx in range(1, num_threads):
                seg_path = f"{part_path}.{idx}"
                with open(seg_path, 'rb') as src_f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise IOError("Aborted during merge")
                        chunk = src_f.read(8 * 1024 * 1024) # 8MB buffer
                        if not chunk:
                            break
                        dest_f.write(chunk)
                try:
                    os.remove(seg_path)
                except Exception as e:
                    logger.warning(f"Could not remove temporary segment file {seg_path}: {e}")
    except Exception as merge_err:
        logger.error(f"Error merging segments for {os.path.basename(local_path)}: {merge_err}")
        if cancel_event and cancel_event.is_set():
            return False
        return False

    if total_size > 0:
        downloaded_size = os.path.getsize(part_path)
        if downloaded_size != total_size:
            logger.error(f"Integrity check failed for merged segmented download of {os.path.basename(local_path)}: downloaded {downloaded_size} bytes, expected {total_size} bytes")
            try:
                os.remove(part_path)
            except:
                pass
            return False

    if os.path.exists(local_path):
        try:
            os.remove(local_path)
        except:
            pass
    try:
        os.rename(part_path, local_path)
    except Exception as e:
        logger.error(f"Failed to rename merged part file to local path: {e}")
        return False
        
    print(f"Successfully downloaded: {os.path.basename(local_path)}")
    return True
