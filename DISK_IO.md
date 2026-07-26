# Reducing HDD Usage During Downloads

This documents the diagnosis, what changed, and the options you can still turn on.

## What the screenshot shows

The settings in the screenshot are the exact worst case for a spinning disk:

| Setting | Value | Effect |
|---|---|---|
| Parallel Files | **4 Files** | 4 files written at once |
| Use parallel connections | **on, up to 4** | x4 byte-ranges per file |
| Download path | `f:/torbox` | a large secondary drive (606 GB free) |

4 x 4 = **up to 16 write cursors scattered across the platter simultaneously.**

The decisive number is the throughput: **11.2 MB/s on a 39 GB file.** A healthy
modern HDD does 120–200 MB/s sequentially. Getting ~11 MB/s while the drive
reports 100% busy is the signature of seek saturation — the drive is working
flat out, but almost all of that work is head movement rather than writing. It
is *not* a network limit and *not* a full disk.

Also note `fg-01.bin` at 39 GB: comfortably over the 1 GB threshold, so it took
the 4-connection segmented path, with its four cursors ~10 GB apart.

**With the HDD profile this workload becomes one file, one sequential stream,
32 MB writes** — the same bytes, but written in the order the platter spins.

## Important distinction: 100% "active time" is not "disk is full"

Windows Task Manager's disk **%** column is *active time* — the fraction of
wall-clock time the drive had at least one request outstanding. It is **not**
throughput. A hard drive doing 8 MB/s of scattered writes will read 100% while
the same drive doing 150 MB/s of sequential writes might read 30%. So the goal
is not "write less data" (you need every byte of the file) — it's **write the
same bytes in far fewer, larger, sequential operations**.

## Bug found via the screenshot: `f:/torbox` broke drive detection

The path is typed with **forward slashes**. The first version of my Windows
detection used `os.path.splitdrive(...).rstrip(":")`, which is correct on
Windows but silently returned `""` when reasoning about such paths elsewhere,
and would have passed a bogus value to `Get-Partition`. Auto-detect would have
fallen back to the SSD profile and **changed nothing for you.** Fixed by parsing
with `ntpath` explicitly and rejecting anything that isn't a single drive
letter; UNC paths (`\\server\share`) now get their own `network` profile that
keeps parallelism but still uses large buffers.

## Root causes found in the code

### 1. Parallel segments = a seek storm (the main one)

`download_file` split files >= 1 GB into up to 5 byte-ranges, each downloaded by
its own thread, each writing straight into the shared `.part` file at its own
offset:

```python
with open(part_path, 'r+b') as f:
    f.seek(seg_start)
    for chunk in resp.iter_content(chunk_size=512 * 1024):
        f.write(chunk)          # 512 KB, then another thread writes 2 GB away
```

For a 10 GB file with 5 threads, the write cursors sit ~2 GB apart. Every time
the OS scheduler switched threads the drive head had to physically travel across
the platter. An HDD does ~100–200 seeks/sec; at ~10 ms per seek the drive spends
nearly all its time moving rather than writing — hence 100% active time at low
throughput. SSDs don't care; **spinning disks care enormously.**

### 2. Multiplied by parallel files

`max_parallel_files` (up to 5) × per-file connections (up to 5) meant **up to 25
independent write cursors** on one drive, with nothing coordinating them.

### 3. Sparse preallocation caused fragmentation

```python
f.seek(total_size - 1); f.write(b'\x00')
```

The comment claimed this was instant on NTFS — it is, but on ext4/XFS/APFS it
creates a **sparse** file. Blocks get allocated lazily as each thread writes, so
the filesystem interleaves extents from five writers and the finished file is
badly fragmented. That penalty persists forever: every later read of that movie
costs extra seeks too.

### 4. Small writes and page-cache churn

512 KB chunks went straight to the filesystem, so a 10 GB file issued ~20,000
write syscalls. Nothing ever hinted that written data wasn't needed again, so
multi-GB transfers filled the page cache and the kernel flushed in bursts —
which is what produces those rhythmic spikes to 100%.

## What changed

New module **`torbox_downloader/diskio.py`**:

| Component | What it does |
|---|---|
| `SegmentWriter` | Buffers chunks in RAM, flushes **8 MB (SSD) / 32 MB (HDD)** blocks via `pwrite` |
| `allocate_file` | `posix_fallocate` — real contiguous extents, no sparse file |
| `WriteThrottle` | Process-wide cap on how many write streams touch the disk at once |
| `detect_disk_type` | Reads `/sys/block/*/queue/rotational` (Linux) or `MSFT_PhysicalDisk` (Windows) |
| `drop_cache_range` | `POSIX_FADV_DONTNEED` on flushed ranges, so writeback stays smooth |

Applied in `downloader.py`:

- **Both** the single-threaded and segmented paths write through `SegmentWriter`.
- Network chunk size raised 512 KB → 1 MB (fewer, larger appends).
- **On a detected HDD, a file downloads over one sequential connection** — the
  single highest-impact change. Parallel connections help when the *server* is
  the bottleneck; when the *disk* is the bottleneck they actively hurt.
- On HDD, `max_parallel_files` is forced to 1 for the same reason.
- Segment writes are clamped to their range, so a server that ignores `Range`
  can no longer corrupt the neighbouring segment.

Measured on a 256 MB file:

```
OLD  4 segments x 512 KB writes    512 writes   avg   512 KB
NEW  HDD profile, 32 MB buffer       8 writes   avg 32768 KB   -> 64x fewer ops
```

## Settings

GUI (Settings → Storage Type / Write Buffer), or CLI:

```bash
python main.py --disk-profile hdd --write-buffer-mb 64 --no-gui
```

- `auto` (default) — detect the drive and pick the profile.
- `hdd` — one sequential stream, 32 MB buffer. Lowest disk utilisation.
- `ssd` — full parallel segmented downloads, 8 MB buffer.

Buffer size is a RAM-vs-seeks trade: 32 MB × 1 file ≈ 32 MB RAM. Raising it to
64–128 MB reduces seeks further if you have the memory.

## Further options, not implemented (deliberately)

1. **Download to SSD, move to HDD afterwards.** A staging directory on fast
   storage plus a single sequential copy at the end gives a perfectly
   unfragmented file. Costs double the writes and needs scratch space, so it
   should be opt-in — happy to add it if you have an SSD to stage on.
2. **Serialising across torrents globally.** `WriteThrottle` already gates
   streams; a strict global queue would go further but would idle the network
   whenever the disk is busy.
3. **`O_DIRECT` / `FILE_FLAG_NO_BUFFERING`.** Bypasses the page cache entirely.
   Requires sector-aligned buffers and offsets, and is easy to get subtly wrong
   — the `FADV_DONTNEED` hints capture most of the benefit at far less risk.
4. **Verify with real numbers.** On Windows, `resmon` → Disk → *Disk Queue
   Length* is the metric to watch. Sustained queue length > 2 on an HDD means
   it's still the bottleneck; if so, raise `--write-buffer-mb`.
5. **Check for antivirus scanning.** Defender scanning every completed multi-GB
   file can itself pin the disk at 100% *after* a download finishes. Excluding
   the downloads folder is often as impactful as any code change.

## Tests

`tests/test_diskio.py` (19) and `tests/test_download_io.py` (9) cover
non-sparse allocation, write coalescing, threaded-segment integrity, Windows
drive-letter parsing (including `f:/torbox` and UNC paths), and end-to-end
downloads (single-threaded, segmented, resumed, no-Range fallback) against a
local range-capable HTTP server, with SHA-256 verification.

```
python -m unittest tests.test_diskio tests.test_downloader \
                   tests.test_download_io tests.test_detector
# Ran 36 tests - OK
```

`tests/test_integration.py` still requires `playwright` and is unaffected.
