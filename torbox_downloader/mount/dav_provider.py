"""Read-only WebDAV provider exposing :class:`TorboxVfs`.

Fallback backend when WinFsp isn't installed: WsgiDAV serves the virtual
tree over localhost and Windows maps it to a drive letter via ``net use``.
File content streams from the TorBox CDN through a seekable file-object,
honoring range requests without touching disk.
"""

import logging
import mimetypes
import time

try:
    from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN
    from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

    HAVE_WSGIDAV = True
except ImportError:
    HAVE_WSGIDAV = False

from torbox_downloader.mount.streaming import LazyRemoteFile

logger = logging.getLogger(__name__)


def _mount_time():
    return int(time.time())


if HAVE_WSGIDAV:

    class StreamedFileResource(DAVNonCollection):
        """A remote torrent file streamed live from the CDN."""

        def __init__(self, path, environ, vfs):
            super().__init__(path, environ)
            self._vfs = vfs
            try:
                st = vfs.stat(path)
            except Exception:
                st = None
            self._size = int(st["size"]) if st else 0

        # --- metadata ---

        def get_content_length(self):
            return self._size

        def get_content_type(self):
            guessed = mimetypes.guess_type(self.path)[0]
            return guessed or "application/octet-stream"

        def get_creation_date(self):
            return _mount_time()

        def get_last_modified(self):
            return _mount_time()

        def get_etag(self):
            return f"{self._size:x}-{_mount_time():x}"

        def support_etag(self):
            return True

        def support_modified(self):
            return True

        def support_content_length(self):
            return True

        def support_ranges(self):
            return True

        # --- content ---

        def get_content(self):
            reader = self._vfs.open_reader(self.path)
            return LazyRemoteFile(reader)

        # --- mutations are refused ---

        def begin_write(self, *args, **kwargs):
            raise DAVError(HTTP_FORBIDDEN)

        def create_empty_resource(self):
            raise DAVError(HTTP_FORBIDDEN)

        def delete(self):
            raise DAVError(HTTP_FORBIDDEN)

        def copy_move_single(self, *args, **kwargs):
            raise DAVError(HTTP_FORBIDDEN)

    class FolderResource(DAVCollection):
        """A torrent or subfolder inside the virtual tree."""

        def __init__(self, path, environ, vfs):
            super().__init__(path, environ)
            self._vfs = vfs

        def get_member_names(self):
            try:
                return [item[0] for item in self._vfs.listdir(self.path)]
            except Exception as e:
                logger.warning(f"listdir failed for {self.path!r}: {e}")
                return []

        def get_member(self, name):
            child_path = self.path.rstrip("/") + "/" + str(name)
            return self.provider.get_resource_inst(child_path, self.environ)

    class TorboxDavProvider(DAVProvider):
        """Maps every WebDAV URL onto the TorboxVfs tree."""

        def __init__(self, vfs):
            super().__init__()
            self._vfs = vfs

        def _safe_stat(self, path):
            try:
                return self._vfs.stat(path)
            except Exception:
                return None

        def exists(self, path):
            if path in ("", "/"):
                return True
            return self._safe_stat(path) is not None

        def is_collection(self, path):
            if path in ("", "/"):
                return True
            st = self._safe_stat(path)
            return bool(st and st["is_dir"])

        def get_resource_inst(self, path, environ):
            if path in ("", "/"):
                return FolderResource("/", environ, self._vfs)
            st = self._safe_stat(path)
            if st is None:
                return None
            if st["is_dir"]:
                return FolderResource(path, environ, self._vfs)
            return StreamedFileResource(path, environ, self._vfs)


def create_dav_server(vfs, host="127.0.0.1", port=0):
    """Build (but do not start) a cheroot server serving the vfs over WebDAV.

    Anonymous access, bound to localhost only. Returns the cheroot Server;
    callers start it in a thread via ``server.start()`` and stop it with
    ``server.stop()``.
    """
    from cheroot import wsgi as cheroot_wsgi
    from wsgidav.wsgidav_app import WsgiDAVApp

    config = {
        "provider_mapping": {"/": TorboxDavProvider(vfs)},
        "http_authenticator": {
            "domain_controller": None,
            "accept_basic": True,
            "accept_digest": True,
            "default_to_digest": False,
        },
        "simple_dc": {"user_mapping": {"*": True}},
        "verbose": 0,
        "logging": {"enable_loggers": []},
        "property_manager": False,
        "lock_storage": False,
        "dir_browser": {"enable": False},
    }
    app = WsgiDAVApp(config)
    return cheroot_wsgi.Server((host, port), app, server_name="torbox-dav")
