"""FFmpeg-backed live playback preparation, so a browser can play anything.

Browsers are fussy about two separate things: the *container* and the *codecs*.
This module fixes each with the cheapest tool that works, and never writes to
disk.

**Remux** -- the container is the only problem. Browsers cannot read Matroska at
all, but most scene-release MKVs already hold H.264 video and AAC audio, codecs
every browser decodes. Swapping Matroska for fragmented MP4 costs no encoding
whatsoever: FFmpeg copies the packets across and playback starts in well under a
second.

**Transcode** -- the codecs are the problem, which is the normal case for a
modern library: x265 releases are HEVC, and HEVC is not something browsers
decode reliably (Chrome and Edge need the HEVC Video Extensions *and* hardware
support, and 10-bit often fails even then). A remux cannot help, because it
copies codecs unchanged. Re-encoding to H.264 is the only thing that plays such
a file, so it is offered -- but only behind an explicit click, since it is the
one operation here that costs real CPU or GPU.

Both paths pipe fragmented MP4 to stdout, which the caller relays to the client.
Nothing is written to disk, and neither output is seekable: a live conversion
has no fixed length to seek within.
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

# Preference order for re-encoding: hardware first, software last. Every
# candidate is *tested* rather than trusted, because a build can list an encoder
# it cannot actually run -- h264_nvenc ships in most Windows builds whether or
# not the machine has an NVIDIA GPU, and it fails at encode time, not load time.
ENCODER_CANDIDATES = (
    "h264_nvenc",          # NVIDIA
    "h264_qsv",            # Intel Quick Sync
    "h264_amf",            # AMD
    "h264_videotoolbox",   # macOS
    "libx264",             # software; present in any normal build
)

# Per-encoder tuning. The target is "watchable now", not "archival": this runs
# live, so a slow preset starves the player instead of producing a nicer file.
ENCODER_ARGS = {
    "h264_nvenc": ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
    "h264_qsv": ["-preset", "veryfast", "-global_quality", "23"],
    "h264_amf": ["-quality", "speed", "-rc", "cqp", "-qp_i", "22", "-qp_p", "22"],
    "h264_videotoolbox": ["-q:v", "55"],
    "libx264": ["-preset", "veryfast", "-crf", "21"],
}

ENCODER_PROBE_TIMEOUT_S = 20.0

_encoder_cache = None
_encoder_lock = threading.Lock()


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


def _encoder_works(name):
    """Encode one synthetic frame, so we learn the truth rather than the list.

    `ffmpeg -encoders` only reports what the build was compiled with. A machine
    with no NVIDIA driver still lists h264_nvenc, and discovering that at
    playback time means the user waits for a failure instead of watching a
    film. One 320x240 frame costs a fraction of a second and settles it.
    """
    binary = ffmpeg_path()
    if not binary:
        return False
    command = [
        binary, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25:d=0.2",
        "-frames:v", "1", "-c:v", name, "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ENCODER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def pick_encoder(force=False):
    """The best usable H.264 encoder, or None. Detected once, then cached."""
    global _encoder_cache
    with _encoder_lock:
        if _encoder_cache is not None and not force:
            return _encoder_cache or None
        _encoder_cache = ""
        for name in ENCODER_CANDIDATES:
            if _encoder_works(name):
                _encoder_cache = name
                logger.info("Transcoding will use the %s encoder.", name)
                break
        else:
            logger.warning(
                "No usable H.264 encoder found; transcoding is unavailable."
            )
        return _encoder_cache or None


def reset_encoder_cache():
    """Forget the detected encoder. For tests; a real run never needs this."""
    global _encoder_cache
    with _encoder_lock:
        _encoder_cache = None


def can_transcode():
    """True when a re-encode is actually possible on this machine."""
    return bool(available() and pick_encoder())


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


def transcode_command(url, start=None, encoder=None):
    """The FFmpeg invocation that re-encodes a file into playable H.264.

    This is the expensive path, and the only one that can rescue HEVC, DTS or
    TrueHD -- none of which a browser decodes, and none of which a remux can
    change. Video becomes H.264, audio becomes AAC, and the result is the same
    fragmented MP4 on stdout, so the no-disk promise is unchanged.
    """
    encoder = encoder or pick_encoder()
    if not encoder:
        raise RemuxError("No usable H.264 encoder is available.")

    command = [ffmpeg_path() or "ffmpeg", "-nostdin", "-loglevel", "error"]
    if start and start > 0:
        command += ["-ss", "%.3f" % start]
    command += [
        "-i", url,
        # Same optional maps as the remux path: a file with no audio track is
        # still playable, and subtitles cannot be carried in MP4.
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-sn", "-dn",
        "-c:v", encoder,
        # 10-bit HEVC is the common case in x265 releases and 10-bit H.264 is
        # not something browsers accept. Forcing 8-bit is what makes the output
        # playable at all rather than merely well-encoded.
        "-pix_fmt", "yuv420p",
    ]
    command += ENCODER_ARGS.get(encoder, [])
    command += [
        # Audio is re-encoded rather than copied: "the audio codec is not
        # browser-safe" is exactly the situation that leads here, and AAC is
        # cheap to produce.
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    return command


def _stream_command(command, what, url, start=None, block=STREAM_READ_BYTES):
    """Yield stdout of a live FFmpeg process running `command`.

    Shared by the remux and transcode paths, which differ only in the command
    they hand FFmpeg: both are a long-running process writing fragmented MP4 to
    a pipe, and both need the same three guarantees.

    - **The stderr pipe is drained.** FFmpeg writes progress there, and if
      nobody reads it the pipe buffer fills and FFmpeg blocks forever -- which
      looks exactly like a slow network.
    - **A stalled encoder is killed**, or a dead CDN read leaves an orphaned
      process holding a socket.
    - **The process is always reaped** -- on normal end, on the idle timeout,
      and on the caller abandoning the generator, which is what happens when
      the client disconnects. Callers should still drive this in a try/finally
      and call `.close()`.
    """
    binary = ffmpeg_path()
    if not binary:
        raise RemuxError("ffmpeg is not installed.")

    command = list(command)
    command[0] = binary
    logger.info(
        "%s %s (start=%s).", what, _redact(url),
        ("%.1fs" % start) if start else "0",
    )

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
                    "%s stalled for %.0fs; killing ffmpeg.", what, IDLE_KILL_S
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


def stream_remux(url, start=None, block=STREAM_READ_BYTES):
    """Yield fragmented-MP4 bytes copied out of the source container."""
    return _stream_command(remux_command(url, start), "Remuxing", url, start, block)


def stream_transcode(url, start=None, encoder=None, block=STREAM_READ_BYTES):
    """Yield fragmented-MP4 bytes re-encoded to H.264 + AAC.

    Costs real CPU or GPU, so callers must only reach this on an explicit user
    request -- never as part of deciding how a file should play.
    """
    encoder = encoder or pick_encoder()
    if not encoder:
        raise RemuxError("No usable H.264 encoder is available.")
    command = transcode_command(url, start, encoder)
    return _stream_command(
        command, "Transcoding via %s" % encoder, url, start, block
    )
