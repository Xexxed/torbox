"""On-the-fly MKV remuxing, so Matroska files can play in a browser.

Browsers cannot decode the Matroska container at all. But most scene-release
MKVs already hold H.264 video and AAC audio -- codecs every browser decodes.
For those files the *container* is the only problem, and swapping Matroska for
fragmented MP4 costs no encoding whatsoever: FFmpeg copies the packets across
and playback starts in well under a second.

Anything whose codecs are not browser-safe (HEVC, DTS, TrueHD, ...) cannot be
fixed this way. Re-encoding would fix it, but that is a deliberate non-goal --
it burns the user's CPU and the plan for this feature is remux-only.

Nothing here writes to disk. FFmpeg reads from a URL and writes fragmented MP4
to stdout, which the caller relays to the client.
"""

import json
import logging
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 25.0
STREAM_READ_BYTES = 64 * 1024

# Kill FFmpeg if it produces nothing for this long while we are waiting on it.
# Without this a stalled CDN read leaves an orphaned process holding a socket.
IDLE_KILL_S = 45.0

# Codecs a browser decodes once they arrive inside fragmented MP4. Restricting
# to these is what makes "copy, never encode" safe to promise.
REMUXABLE_VIDEO = frozenset({"h264"})
REMUXABLE_AUDIO = frozenset({"aac", "mp3"})


class RemuxError(RuntimeError):
    """FFmpeg is missing, or could not be used for this file."""


def ffmpeg_path():
    return shutil.which("ffmpeg")


def ffprobe_path():
    return shutil.which("ffprobe")


def available():
    """Both binaries are needed: ffprobe decides whether to remux."""
    return bool(ffmpeg_path() and ffprobe_path())


def _redact(url):
    """CDN links carry an auth token in the query string; never log it."""
    return str(url).split("?", 1)[0]


def _to_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _summarise(data):
    """Reduce ffprobe's JSON to the three things the decision needs."""
    streams = data.get("streams") or []
    video = next((s.get("codec_name") for s in streams
                  if s.get("codec_type") == "video"), None)
    audio = next((s.get("codec_name") for s in streams
                  if s.get("codec_type") == "audio"), None)

    duration = _to_seconds((data.get("format") or {}).get("duration"))
    if duration is None:
        for stream in streams:
            duration = _to_seconds(stream.get("duration"))
            if duration is not None:
                break

    return {
        "video": (video or "").lower() or None,
        "audio": (audio or "").lower() or None,
        "duration": duration,
    }


def probe(url, timeout=PROBE_TIMEOUT_S):
    """Inspect a remote file's codecs. Raises RemuxError on any failure."""
    binary = ffprobe_path()
    if not binary:
        raise RemuxError("ffprobe is not installed.")

    command = [
        binary, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-i", url,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RemuxError("Timed out reading the file's header.")
    except OSError as exc:
        raise RemuxError("Could not run ffprobe: %s" % exc)

    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RemuxError(detail.splitlines()[-1] if detail else "ffprobe failed.")

    try:
        data = json.loads(result.stdout or b"{}")
    except ValueError:
        raise RemuxError("ffprobe returned unreadable output.")
    return _summarise(data)


def can_remux(info):
    """True when the container can be swapped without re-encoding."""
    if not info:
        return False
    # Codec names are compared case-insensitively: ffprobe reports lowercase,
    # but a caller-supplied or future source may not.
    video = (info.get("video") or "").lower()
    if video not in REMUXABLE_VIDEO:
        return False
    # Audio is optional -- a silent video is still playable -- but if it is
    # present it has to be something MP4 can legally carry and browsers decode.
    audio = (info.get("audio") or "").lower()
    if audio and audio not in REMUXABLE_AUDIO:
        return False
    return True


def remux_command(url, start=None):
    """The FFmpeg invocation that produces a streamable fragmented MP4.

    `+faststart` is deliberately absent: it needs a seekable output to move the
    moov atom and fails outright on a pipe ("muxer does not support non
    seekable output"). `empty_moov` plus `frag_keyframe` is the streaming form
    -- the header is written up front and media follows in keyframe-aligned
    fragments, so the browser can start before the file has been read.
    """
    command = [ffmpeg_path() or "ffmpeg", "-nostdin", "-loglevel", "error"]
    if start and start > 0:
        # Input seeking, before -i: FFmpeg seeks the source instead of reading
        # and discarding everything up to the offset, which over HTTP means a
        # range request rather than a full download.
        command += ["-ss", "%.3f" % start]
    command += [
        "-i", url,
        # Only the first video and first audio track, both optional so a file
        # missing either still works. Subtitles and data streams are dropped:
        # MP4 cannot carry SubRip, and the browser would not render them.
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-sn", "-dn",
        "-c", "copy",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    return command


def stream_remux(url, start=None, block=STREAM_READ_BYTES):
    """Yield fragmented-MP4 bytes read from a live FFmpeg process.

    The process is always reaped -- on normal end, on the caller closing the
    generator early (which is what happens when the client disconnects), and on
    a stall longer than IDLE_KILL_S. Callers should drive this in a
    try/finally and call `.close()` so an abandoned stream cannot leave FFmpeg
    running.
    """
    binary = ffmpeg_path()
    if not binary:
        raise RemuxError("ffmpeg is not installed.")

    command = remux_command(url, start)
    command[0] = binary
    logger.info("Remuxing %s (start=%s).", _redact(url), ("%.1fs" % start) if start else "0")

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        raise RemuxError("Could not start ffmpeg: %s" % exc)

    stop = threading.Event()
    # `waiting` distinguishes "FFmpeg is silent" from "we are blocked writing
    # to a slow client". Only the former is a stall worth killing -- a browser
    # buffering 30s ahead would otherwise look identical to a hung encoder.
    state = {"waiting": False, "last_read": time.monotonic()}

    def drain_stderr():
        try:
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if text:
                    logger.debug("ffmpeg: %s", text)
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def watch_idle():
        while not stop.wait(5.0):
            if not state["waiting"]:
                continue
            if time.monotonic() - state["last_read"] > IDLE_KILL_S:
                logger.warning(
                    "Remux stalled for %.0fs; killing ffmpeg.", IDLE_KILL_S
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return

    threading.Thread(target=drain_stderr, daemon=True).start()
    threading.Thread(target=watch_idle, daemon=True).start()

    try:
        while True:
            state["waiting"] = True
            try:
                chunk = proc.stdout.read(block)
            finally:
                state["waiting"] = False
            if not chunk:
                break
            state["last_read"] = time.monotonic()
            yield chunk
    finally:
        stop.set()
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
