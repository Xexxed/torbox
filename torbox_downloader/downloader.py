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

def _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback):
    # Ensure parent directory exists
    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
        
    session = requests.Session()
    attempt = 0
    while attempt < retries:
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
                    os.remove(part_path)
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
                
            with open(part_path, mode) as f:
                with tqdm(total=stream_total, initial=existing_bytes, unit='B', unit_scale=True, desc=desc, leave=True) as pbar:
                    downloaded = existing_bytes
                    start_time = time.time()
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            chunk_len = len(chunk)
                            pbar.update(chunk_len)
                            downloaded += chunk_len
                            if progress_callback:
                                elapsed = time.time() - start_time
                                speed = (downloaded - existing_bytes) / elapsed if elapsed > 0 else 0
                                progress_callback(os.path.basename(local_path), downloaded, stream_total, speed)
                                
            if os.path.exists(local_path):
                os.remove(local_path)
            os.rename(part_path, local_path)
            print(f"Successfully downloaded: {os.path.basename(local_path)}")
            return True
            
        except (requests.RequestException, IOError) as e:
            attempt += 1
            wait_time = backoff_factor ** attempt
            print(f"Error downloading {os.path.basename(local_path)}: {e}")
            if attempt < retries:
                print(f"Retrying in {wait_time}s (Attempt {attempt + 1}/{retries})...")
                time.sleep(wait_time)
            else:
                print(f"Failed to download {os.path.basename(local_path)} after {retries} attempts.")
                return False

def download_file(url, local_path, expected_size_str=None, headers=None, retries=5, backoff_factor=2, progress_callback=None):
    """
    Downloads a file using parallel segmented chunk downloads (similar to IDM)
    if the server supports it, otherwise falls back to single-threaded chunked downloads.
    """
    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        expected_bytes = parse_size_to_bytes(expected_size_str)
        if expected_bytes and abs(local_size - expected_bytes) < 1024 * 1024:
            print(f"Skipping {os.path.basename(local_path)} (already downloaded)")
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
            accept_ranges = 'bytes' in r.headers.get('Accept-Ranges', '').lower() or r.status_code == 206
            if not accept_ranges and total_size > 5 * 1024 * 1024:
                accept_ranges = True
    except Exception as e:
        logger.error(f"Error fetching headers: {e}")

    if total_size < 5 * 1024 * 1024 or not accept_ranges or total_size == 0:
        return _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback)

    # Replicate IDM multi-connection speed boost (8 threads)
    num_threads = 8
    segment_size = total_size // num_threads
    ranges = []
    for i in range(num_threads):
        start = i * segment_size
        end = total_size - 1 if i == num_threads - 1 else (start + segment_size - 1)
        ranges.append((start, end))

    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    try:
        with open(part_path, 'wb') as f:
            f.truncate(total_size)
    except IOError as e:
        logger.error(f"Failed to pre-allocate file, falling back to single-thread: {e}")
        return _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback)

    downloaded_lock = threading.Lock()
    downloaded_bytes = 0
    start_time = time.time()
    errors = []

    desc = os.path.basename(local_path)
    if len(desc) > 30:
        desc = desc[:27] + "..."

    print(f"Connecting to download {os.path.basename(local_path)} using {num_threads} connections...")

    def download_segment(segment_idx, start, end):
        nonlocal downloaded_bytes
        seg_headers = {**headers, 'Range': f'bytes={start}-{end}'}
        
        seg_attempt = 0
        while seg_attempt < retries:
            try:
                resp = requests.get(url, headers=seg_headers, stream=True, timeout=30)
                if resp.status_code not in [200, 206]:
                    resp.raise_for_status()

                with open(part_path, 'r+b') as f:
                    f.seek(start)
                    for chunk in resp.iter_content(chunk_size=512 * 1024): # 512KB chunks
                        if chunk:
                            f.write(chunk)
                            chunk_len = len(chunk)
                            
                            with downloaded_lock:
                                downloaded_bytes += chunk_len
                                if progress_callback:
                                    elapsed = time.time() - start_time
                                    speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                                    progress_callback(os.path.basename(local_path), downloaded_bytes, total_size, speed)
                return True
            except Exception as e:
                if "aborted" in str(e).lower() or "aborted" in str(getattr(e, '__context__', '')).lower():
                    errors.append(f"Aborted: {e}")
                    raise
                seg_attempt += 1
                if seg_attempt >= retries:
                    errors.append(f"Segment {segment_idx} failed: {e}")
                    return False
                time.sleep(backoff_factor ** seg_attempt)
        return False

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(download_segment, idx, r[0], r[1]) for idx, r in enumerate(ranges)]
        
        with tqdm(total=total_size, unit='B', unit_scale=True, desc=desc, leave=True) as pbar:
            last_val = 0
            while True:
                with downloaded_lock:
                    curr = downloaded_bytes
                pbar.update(curr - last_val)
                last_val = curr
                if curr >= total_size or len(errors) > 0:
                    break
                time.sleep(0.2)
            
            if len(errors) == 0:
                pbar.update(total_size - last_val)

    if errors:
        print(f"Segmented download failed: {errors[0]}")
        if os.path.exists(part_path):
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
    os.rename(part_path, local_path)
    print(f"Successfully downloaded: {os.path.basename(local_path)}")
    return True
