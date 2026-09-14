"""Opening a stream in VLC, the desktop player.

A web page cannot launch a local application, so the server does it on the
page's behalf: the page names a file in the library, the server resolves that
file's CDN link and hands it to VLC.

The server resolves the link itself rather than accepting a URL from the
request, and that is deliberate. An endpoint that opens a player on whatever
address it is handed would happily be pointed at a local file, a UNC share, or
some other machine entirely -- a way to make the user's own media player act on
a stranger's behalf. The only thing a caller gets to choose is which of their
own files to play.

VLC gets the CDN link directly, not this server's proxy. It is the better
client for the job: full range support, real seeking, subtitles and every audio
track intact, and no Python process in the middle of a 4K stream. The trade-off
is that the link is only valid for its ~30-minute lifetime, so a very long film
can outlive it -- the proxy path exists for the cases where that matters.
"""

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


class VlcError(RuntimeError):
    """VLC could not be found, or could not be started."""


def _redact(url):
    """CDN links carry an auth token in the query string; never log it."""
    return str(url).split("?", 1)[0]


def _windows_candidates():
    """Where VLC lands on Windows, most trustworthy source first.

    The registry entry is what VLC's own installer writes, so it is the only
    one that survives a non-default install location. The literal defaults are
    still worth listing because `%ProgramFiles%` is not guaranteed to be in the
    environment at all -- it is missing entirely under some shells, and this
    app is routinely started from one.
    """
    candidates = []
    try:
        import winreg
    except ImportError:                      # not Windows after all
        winreg = None
    if winreg is not None:
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\VideoLAN\VLC"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    install_dir = winreg.QueryValueEx(handle, "InstallDir")[0]
            except OSError:
                continue
            if install_dir:
                candidates.append(os.path.join(install_dir, "vlc.exe"))

    roots = [os.environ.get("ProgramFiles"),
             os.environ.get("ProgramFiles(x86)"),
             os.environ.get("ProgramW6432"),
             r"C:\Program Files",
             r"C:\Program Files (x86)"]
    local = os.environ.get("LOCALAPPDATA")
    if local:                                # a per-user install
        roots.append(os.path.join(local, "Programs"))
    for root in roots:
        if root:
            candidates.append(os.path.join(root, "VideoLAN", "VLC", "vlc.exe"))
    return candidates


def _install_candidates():
    """The usual install locations, for when VLC is not on PATH.

    This is the common case rather than the fallback: the Windows installer
    does not add VLC to PATH, so `shutil.which` alone finds nothing on a
    perfectly ordinary install.
    """
    if sys.platform == "darwin":
        return ["/Applications/VLC.app/Contents/MacOS/VLC"]
    if os.name == "nt":
        return _windows_candidates()
    return ["/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc"]


def vlc_path():
    """Absolute path to VLC, or None.

    `TORBOX_VLC_PATH` overrides detection outright, for portable installs and
    hand-unpacked zips that nothing else would find. An override that does not
    exist is treated as "no VLC" rather than silently falling back, so a typo
    is visible instead of confusing.
    """
    override = (os.environ.get("TORBOX_VLC_PATH") or "").strip()
    if override:
        return override if os.path.isfile(override) else None

    found = shutil.which("vlc")
    if found:
        return found
    for candidate in _install_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def available():
    return vlc_path() is not None


def open_url(url):
    """Hand `url` to VLC, detached from this process. Returns the executable.

    Non-blocking on purpose: VLC outlives the request that started it, and the
    user may well keep it open long after the browser tab is closed.
    """
    binary = vlc_path()
    if not binary:
        raise VlcError(
            "VLC was not found. Install it, or set TORBOX_VLC_PATH to the "
            "vlc executable."
        )

    # Only ever hand VLC a real web address. As well as ruling out local paths,
    # this guarantees the argument cannot begin with a dash -- VLC would read
    # something like "--video-filter" as one of its own options.
    if not str(url).lower().startswith(("http://", "https://")):
        raise VlcError("Refusing to open a non-HTTP media URL.")

    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # Detach the player so it is not tied to the server's console and does
        # not die with the server.
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        options["start_new_session"] = True

    try:
        subprocess.Popen([binary, url], **options)
    except OSError as exc:
        raise VlcError("Could not start VLC: %s" % exc)

    logger.info("Opened %s in VLC.", _redact(url))
    return binary
