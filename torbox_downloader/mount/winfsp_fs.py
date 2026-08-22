"""Read-only WinFsp filesystem backed by :class:`TorboxVfs`.

Mirrors winfspy's in-memory reference implementation (`winfspy.memfs`) but
serves directory listings and file bytes straight from the virtual tree:
file reads stream from the TorBox CDN through ranged requests, so opening a
file plays it instantly and copy-paste downloads land at the paste target.
Nothing is written to disk.

Optional dependency: ``winfspy`` (plus the WinFsp driver MSI). When missing,
the drive falls back to the WebDAV backend instead.
"""

import logging
import threading

try:
    from winfspy import (
        FileSystem,
        BaseFileSystemOperations,
        FILE_ATTRIBUTE,
        NTStatusObjectNameNotFound,
        NTStatusDirectoryNotEmpty,
        NTStatusNotADirectory,
        NTStatusAccessDenied,
        NTStatusEndOfFile,
        NTStatusMediaWriteProtected,
    )
    from winfspy.plumbing.win32_filetime import filetime_now
    from winfspy.plumbing.security_descriptor import SecurityDescriptor

    HAVE_WINFSPY = True
except (ImportError, Exception):
    HAVE_WINFSPY = False
    BaseFileSystemOperations = object
    FileSystem = None
    FILE_ATTRIBUTE = None
    NTStatusObjectNameNotFound = Exception
    NTStatusDirectoryNotEmpty = Exception
    NTStatusNotADirectory = Exception
    NTStatusAccessDenied = Exception
    NTStatusEndOfFile = Exception
    NTStatusMediaWriteProtected = Exception
    filetime_now = lambda: 0
    SecurityDescriptor = None

from torbox_downloader.mount.vfs import (
    VfsError,
    VfsIsADirectory,
    VfsNotADirectory,
    VfsNotFound,
)

logger = logging.getLogger(__name__)

VOLUME_LABEL = "Torbox"
ALLOCATION_UNIT = 4096


def _to_vfs_path(file_name):
    """WinFsp hands us '\\Torrent Name\\Sub\\File.mkv'; normalize to vfs form."""
    cleaned = str(file_name).replace("\\", "/").lstrip("/")
    return "/" + cleaned


class _OpenedCtx:
    __slots__ = ("vfs", "vfs_path", "is_dir", "size", "_reader", "_reader_lock")

    def __init__(self, vfs, vfs_path, is_dir, size):
        self.vfs = vfs
        self.vfs_path = vfs_path
        self.is_dir = is_dir
        self.size = size
        self._reader = None
        self._reader_lock = threading.Lock()

    @property
    def reader(self):
        with self._reader_lock:
            if self._reader is None:
                self._reader = self.vfs.open_reader(self.vfs_path)
            return self._reader

    def release_reader(self):
        with self._reader_lock:
            if self._reader is not None:
                try:
                    self._reader.close()
                except Exception:
                    pass
                self._reader = None


def map_vfs_errors(fn):
    """Translate vfs errors into WinFsp statuses; log generic failures."""
    name = fn.__name__

    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except VfsNotFound as e:
            logger.info(f"FS {name}: not found ({e})")
            raise NTStatusObjectNameNotFound() from None
        except VfsNotADirectory as e:
            logger.info(f"FS {name}: not a directory ({e})")
            raise NTStatusNotADirectory() from None
        except VfsIsADirectory as e:
            logger.info(f"FS {name}: is a directory ({e})")
            raise NTStatusAccessDenied() from None
        except VfsError as e:
            logger.warning(f"FS {name} failed: {e}")
            raise NTStatusObjectNameNotFound() from None

    wrapper.__name__ = fn.__name__
    return wrapper


class TorboxFsOperations(BaseFileSystemOperations):
    """Read-only file system operations over :class:`TorboxVfs`."""

    def __init__(self, vfs, volume_label=VOLUME_LABEL):
        super().__init__()
        volume_label = volume_label or VOLUME_LABEL
        if len(volume_label) > 31:
            volume_label = volume_label[:31]
        self._volume_info = {
            "total_size": max(int(vfs.total_bytes), 1),
            "free_size": 0,
            "volume_label": volume_label,
        }
        self._sec_desc = SecurityDescriptor.from_string(
            "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"
        )
        now = filetime_now()
        self._now = now
        self._vfs = vfs

    # --- helpers -------------------------------------------------------------

    def _dir_entry_dict(self, name, is_dir, size=0):
        size = max(0, int(size or 0)) if not is_dir else 0
        alloc = ((size + ALLOCATION_UNIT - 1) // ALLOCATION_UNIT) * ALLOCATION_UNIT if size > 0 else 0
        info = {
            "file_attributes": (
                FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
                if is_dir
                else FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
            ),
            "allocation_size": alloc,
            "file_size": size,
            "creation_time": self._now,
            "last_access_time": self._now,
            "last_write_time": self._now,
            "change_time": self._now,
            "index_number": 0,
        }
        return {"file_name": name, **info}

    # --- volume --------------------------------------------------------------

    def get_volume_info(self):
        return dict(self._volume_info)

    def set_volume_label(self, volume_label):
        raise NTStatusMediaWriteProtected()

    # --- security ------------------------------------------------------------

    @map_vfs_errors
    def get_security_by_name(self, file_name):
        st = self._vfs.stat(_to_vfs_path(file_name))
        if st is None:
            logger.info(f"get_security_by_name: not found: {file_name!r}")
            raise NTStatusObjectNameNotFound()
        attrs = (
            FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            if st["is_dir"]
            else FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
        )
        return (attrs, self._sec_desc.handle, self._sec_desc.size)

    def get_security(self, file_context):
        return self._sec_desc

    def set_security(self, *args, **kwargs):
        raise NTStatusAccessDenied()

    # --- files ----------------------------------------------------------------

    @map_vfs_errors
    def open(self, file_name, create_options, granted_access):
        path = _to_vfs_path(file_name)
        st = self._vfs.stat(path)
        if st is None:
            logger.info(f"open: not found: {file_name!r}")
            raise NTStatusObjectNameNotFound()
        ctx = _OpenedCtx(self._vfs, path, st["is_dir"], 0 if st["is_dir"] else st["size"])
        logger.debug(f"open ok: {path} (dir={st['is_dir']}, size={ctx.size})")
        return ctx

    def close(self, file_context):
        file_context.release_reader()

    @map_vfs_errors
    def get_file_info(self, file_context):
        size = file_context.size
        alloc = ((size + ALLOCATION_UNIT - 1) // ALLOCATION_UNIT) * ALLOCATION_UNIT if size > 0 else 0
        attrs = (
            FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            if file_context.is_dir
            else FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
        )
        return {
            "file_attributes": attrs,
            "allocation_size": alloc,
            "file_size": size,
            "creation_time": self._now,
            "last_access_time": self._now,
            "last_write_time": self._now,
            "change_time": self._now,
            "index_number": 0,
        }

    def read(self, file_context, offset, length):
        if file_context.is_dir:
            raise NTStatusAccessDenied()
        if offset >= file_context.size and file_context.size > 0:
            raise NTStatusEndOfFile()
        try:
            data = file_context.reader.read_range(offset, length)
            if not data and length > 0:
                raise NTStatusEndOfFile()
            return data
        except NTStatusEndOfFile:
            raise
        except Exception as e:
            logger.warning(f"FS read error at {file_context.vfs_path} offset {offset}: {e}")
            raise NTStatusObjectNameNotFound() from None

    def can_delete(self, file_context, file_name):
        raise NTStatusAccessDenied()

    def cleanup(self, file_context, file_name, flags):
        pass

    def flush(self, file_context):
        pass

    # --- directories ----------------------------------------------------------

    @map_vfs_errors
    def read_directory(self, file_context, marker):
        if not file_context.is_dir:
            raise NTStatusNotADirectory()
        entries = []
        if file_context.vfs_path not in ("", "/"):
            entries.append(self._dir_entry_dict(".", True, 0))
            entries.append(self._dir_entry_dict("..", True, 0))
        for name, is_dir, size in self._vfs.listdir_details(file_context.vfs_path):
            entries.append(self._dir_entry_dict(name, is_dir, size))
        if marker is None:
            return entries
        for index, entry in enumerate(entries):
            if entry["file_name"] == marker:
                return entries[index + 1:]
        return []

    @map_vfs_errors
    def get_dir_info_by_name(self, file_context, file_name):
        st = self._vfs.stat(file_context.vfs_path.rstrip("/") + "/" + str(file_name))
        if st is None:
            logger.info(f"get_dir_info_by_name: not found: {file_name!r}")
            raise NTStatusObjectNameNotFound()
        return self._dir_entry_dict(str(file_name), st["is_dir"], st.get("size", 0))

    # --- mutations are refused (read-only drive) ------------------------------

    def create(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()

    def write(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()

    def overwrite(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()

    def rename(self, *args, **kwargs):
        raise NTStatusAccessDenied()

    def set_basic_info(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()

    def set_file_size(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()


class WinfspMountHandle:
    """Owns the winfspy FileSystem instance for one mounted drive letter."""

    def __init__(self, vfs, drive_letter, volume_label=VOLUME_LABEL):
        operations = TorboxFsOperations(vfs, volume_label)
        self.operations = operations
        self.drive_letter = drive_letter
        self._fs = FileSystem(
            str(drive_letter),
            operations,
            sector_size=512,
            sectors_per_allocation_unit=1,
            volume_creation_time=filetime_now(),
            volume_serial_number=0,
            file_info_timeout=1000,
            case_sensitive_search=1,
            case_preserved_names=1,
            unicode_on_disk=1,
            persistent_acls=1,
            post_cleanup_when_modified_only=1,
            um_file_context_is_user_context2=1,
            file_system_name="torbox-drive",
            prefix="",
            debug=False,
            reject_irp_prior_to_transact0=False,
        )

    def start(self):
        self._fs.start()

    def stop(self):
        try:
            self._fs.stop()
        except Exception:
            pass
