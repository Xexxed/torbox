import os
import time
import requests
import logging
from tqdm import tqdm
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .diskio import (
    DEFAULT_CHUNK_BYTES,
    SegmentWriter,
    allocate_file,
    configure_write_throttle,
    recommended_buffer_bytes,
    recommended_connections,
    resolve_disk_profile,
    write_throttle,
)

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

def _download_single_threaded(url, part_path, local_path, total_size, headers, retries, backoff_factor, progress_callback, cancel_event=None, buffer_bytes=None, chunk_bytes=DEFAULT_CHUNK_BYTES, drop_cache=False):
    # Ensure parent directory exists
    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    if buffer_bytes is None:
        buffer_bytes = recommended_buffer_bytes("ssd")
        
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
            
            # Write through a large coalescing buffer instead of pushing every
            # 512 KB straight at the filesystem. Only one sequential stream is
            # open here, so the throttle is just protecting the drive from
            # other files being downloaded in parallel.
            if mode == 'wb':
                # Truncate/create, then reopen r+b so SegmentWriter can pwrite.
                open(part_path, 'wb').close()
                write_offset = 0
            else:
                write_offset = existing_bytes

            with write_throttle:
                with SegmentWriter(part_path, write_offset,
                                   buffer_bytes=buffer_bytes,
                                   drop_cache=drop_cache) as writer:
                    with tqdm(total=stream_total, initial=existing_bytes, unit='B', unit_scale=True, desc=desc, leave=True) as pbar:
                        downloaded = existing_bytes
                        start_time = time.time()
                        for chunk in response.iter_content(chunk_size=chunk_bytes):
                            if cancel_event and cancel_event.is_set():
                                raise IOError("Aborted by user")
                            if chunk:
                                writer.append(chunk)
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

def download_file(url, local_path, expected_size_str=None, headers=None, retries=5, backoff_factor=2, progress_callback=None, parallel=True, num_threads=4, cancel_event=None, disk_profile="auto", buffer_mb=None, drop_cache=None):
    """
    Downloads a file using parallel segmented chunk downloads (similar to IDM)
    if the server supports it, otherwise falls back to single-threaded chunked downloads.

    ``disk_profile`` selects the I/O strategy: ``"auto"`` detects whether the
    destination is a rotational disk, ``"hdd"`` forces the low-seek strategy
    (single connection, large write buffers), and ``"ssd"`` allows the full
    parallel segmented path.
    """
    # Decide the I/O strategy once, before any bytes move.
    profile = resolve_disk_profile(os.path.dirname(local_path) or ".", disk_profile)
    buffer_bytes = recommended_buffer_bytes(profile, buffer_mb)
    if drop_cache is None:
        # Streaming a multi-GB file through the page cache evicts everything
        # else and causes bursty writeback; only worth it for big transfers.
        drop_cache = (profile == "hdd")
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

    # On a rotational disk, splitting one file across N connections means N
    # write cursors thousands of MB apart; the head spends most of its time
    # seeking rather than writing. Collapse to a single sequential stream.
    effective_threads = recommended_connections(profile, num_threads)
    if profile == "hdd" and parallel and num_threads > 1:
        logger.info(
            "Rotational disk detected for %s - using 1 sequential connection "
            "with a %d MB write buffer to avoid seek thrashing.",
            os.path.basename(local_path), buffer_bytes // (1024 * 1024),
        )

    if (total_size < 1024 * 1024 * 1024 or not accept_ranges or total_size == 0
            or not parallel or effective_threads <= 1):
        return _download_single_threaded(
            url, part_path, local_path, total_size, headers, retries,
            backoff_factor, progress_callback, cancel_event,
            buffer_bytes=buffer_bytes, drop_cache=drop_cache,
        )

    # Multi-connection parallel download using write-at-offset (no merge step needed).
    # Each thread writes directly to its own byte-range offset in the pre-allocated output file.
    num_threads = max(1, min(int(effective_threads), 5))
    segment_size = total_size // num_threads
    ranges = []
    for i in range(num_threads):
        start = i * segment_size
        end = total_size - 1 if i == num_threads - 1 else (start + segment_size - 1)
        ranges.append((start, end))

    parent_dir = os.path.dirname(part_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    # Clean up any old-style separate segment files (.part.0, .part.1, ...) from previous versions.
    if parent_dir:
        try:
            base = os.path.basename(part_path)
            for f_name in os.listdir(parent_dir):
                if f_name.startswith(base + ".") and not f_name.endswith(".done"):
                    suffix = f_name[len(base) + 1:]
                    if suffix.isdigit():
                        try:
                            os.remove(os.path.join(parent_dir, f_name))
                            logger.info(f"Removed old segment file: {f_name}")
                        except Exception:
                            pass
        except Exception:
            pass

    # Determine which segments are already fully downloaded via .done marker files.
    # Each completed segment leaves a zero-byte marker: <part_path>.<idx>.done
    completed_segments = set()
    for idx in range(num_threads):
        if os.path.exists(f"{part_path}.{idx}.done"):
            completed_segments.add(idx)

    initial_downloaded_bytes = sum(
        ranges[idx][1] - ranges[idx][0] + 1
        for idx in completed_segments
    )

    # Pre-allocate the output file at the full size so threads can write at arbitrary offsets.
    # On NTFS (Windows) this creates a sparse file and is essentially instant.
    needs_prealloc = True
    if os.path.exists(part_path):
        if os.path.getsize(part_path) == total_size:
            needs_prealloc = False  # Already the right size — safe to reuse.
        else:
            # Wrong size (stale/corrupt partial file). Delete it and start fresh.
            try:
                os.remove(part_path)
            except Exception:
                pass
            # Also invalidate any existing .done markers since the file is gone.
            completed_segments.clear()
            initial_downloaded_bytes = 0
            for idx in range(num_threads):
                marker = f"{part_path}.{idx}.done"
                try:
                    if os.path.exists(marker):
                        os.remove(marker)
                except Exception:
                    pass

    if needs_prealloc:
        print(f"Pre-allocating {os.path.basename(local_path)} ({total_size:,} bytes)...")
        try:
            # Reserve real extents rather than creating a sparse file. Filling a
            # sparse file from several offsets at once makes the allocator
            # interleave each writer's extents, fragmenting the finished file.
            allocate_file(part_path, total_size)
        except Exception as e:
            logger.error(f"Pre-allocation failed for {os.path.basename(local_path)}: {e}. Falling back to single-threaded.")
            return _download_single_threaded(
                url, part_path, local_path, total_size, headers, retries,
                backoff_factor, progress_callback, cancel_event,
                buffer_bytes=buffer_bytes, drop_cache=drop_cache,
            )

    downloaded_lock = threading.Lock()
    downloaded_bytes = initial_downloaded_bytes
    start_time = time.time()
    errors = []

    desc = os.path.basename(local_path)
    if len(desc) > 30:
        desc = desc[:27] + "..."

    print(f"Connecting to download {os.path.basename(local_path)} using {num_threads} connections...")

    last_update_time = [0.0]
    update_interval = 0.25  # update GUI at most every 250 ms

    def download_segment(segment_idx, seg_start, seg_end):
        nonlocal downloaded_bytes
        done_marker = f"{part_path}.{segment_idx}.done"

        # Skip segments that are already fully downloaded.
        if segment_idx in completed_segments:
            return True

        seg_total_size = seg_end - seg_start + 1
        seg_attempt = 0

        while seg_attempt < retries:
            if cancel_event and cancel_event.is_set():
                return False
            try:
                seg_headers = {**headers, 'Range': f'bytes={seg_start}-{seg_end}'}
                resp = requests.get(url, headers=seg_headers, stream=True, timeout=30)

                if resp.status_code not in [200, 206]:
                    if resp.status_code == 416:
                        # Range not satisfiable — retry without range header.
                        resp = requests.get(url, headers=headers, stream=True, timeout=30)
                        if resp.status_code not in [200, 206]:
                            resp.raise_for_status()
                    else:
                        resp.raise_for_status()

                # Write into the pre-allocated file at this segment's offset via a
                # large coalescing buffer. Each thread owns a non-overlapping
                # region, so no locking is needed for the writes themselves -
                # but buffering is what keeps the drive from seeing interleaved
                # 512 KB writes from every segment at once.
                bytes_written = 0
                with SegmentWriter(part_path, seg_start,
                                   buffer_bytes=buffer_bytes,
                                   drop_cache=drop_cache) as writer:
                    for chunk in resp.iter_content(chunk_size=DEFAULT_CHUNK_BYTES):
                        if cancel_event and cancel_event.is_set():
                            raise IOError("Aborted by user")
                        if chunk:
                            # Never spill past this segment into the next one's
                            # region (some servers ignore the Range header).
                            remaining = seg_total_size - bytes_written
                            if remaining <= 0:
                                break
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            writer.append(chunk)
                            chunk_len = len(chunk)
                            bytes_written += chunk_len
                            with downloaded_lock:
                                downloaded_bytes += chunk_len
                                if progress_callback:
                                    now = time.time()
                                    if now - last_update_time[0] >= update_interval or downloaded_bytes >= total_size:
                                        last_update_time[0] = now
                                        elapsed = now - start_time
                                        speed = downloaded_bytes / elapsed if elapsed > 0 else 0
                                        progress_callback(os.path.basename(local_path), downloaded_bytes, total_size, speed)

                if bytes_written >= seg_total_size:
                    # Mark this segment complete so it can be skipped on resume.
                    try:
                        open(done_marker, 'w').close()
                    except Exception:
                        pass
                    return True
                else:
                    raise IOError(f"Segment {segment_idx} incomplete: wrote {bytes_written} of {seg_total_size} bytes")

            except Exception as e:
                if (cancel_event and cancel_event.is_set()) or "aborted" in str(e).lower():
                    errors.append(f"Aborted: {e}")
                    raise

                is_429 = (
                    isinstance(e, requests.exceptions.HTTPError)
                    and e.response is not None
                    and e.response.status_code == 429
                )

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
                if curr >= total_size or errors or (cancel_event and cancel_event.is_set()):
                    break
                time.sleep(0.2)

            if not errors and not (cancel_event and cancel_event.is_set()):
                pbar.update(total_size - last_val)

    if (cancel_event and cancel_event.is_set()) or any("Aborted" in err for err in errors):
        print(f"Segmented download aborted: {os.path.basename(local_path)}")
        return False

    if errors:
        print(f"Segmented download failed: {errors[0]}. Falling back to single-threaded...")
        # Remove the pre-allocated file so the fallback can start clean.
        try:
            os.remove(part_path)
        except Exception:
            pass
        try:
            return _download_single_threaded(
                url, part_path, local_path, total_size, headers, retries,
                backoff_factor, progress_callback, cancel_event,
                buffer_bytes=buffer_bytes, drop_cache=drop_cache,
            )
        except Exception as e:
            logger.error(f"Single-threaded fallback failed for {os.path.basename(local_path)}: {e}")
            return False

    # ── No merge step needed ──────────────────────────────────────────────────
    # All threads wrote directly into part_path at their respective offsets.
    # Just verify integrity, clean up .done markers, and rename.

    # Clean up .done marker files.
    for idx in range(num_threads):
        marker = f"{part_path}.{idx}.done"
        try:
            if os.path.exists(marker):
                os.remove(marker)
        except Exception as e:
            logger.warning(f"Could not remove done marker {marker}: {e}")

    # Integrity check.
    if total_size > 0:
        downloaded_size = os.path.getsize(part_path)
        if downloaded_size != total_size:
            logger.error(
                f"Integrity check failed for {os.path.basename(local_path)}: "
                f"got {downloaded_size:,} bytes, expected {total_size:,} bytes"
            )
            try:
                os.remove(part_path)
            except Exception:
                pass
            return False

    if os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception:
            pass
    try:
        os.rename(part_path, local_path)
    except Exception as e:
        logger.error(f"Failed to rename part file to final path: {e}")
        return False

    print(f"Successfully downloaded: {os.path.basename(local_path)}")
    return True
