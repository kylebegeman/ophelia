"""Descriptor-relative filesystem mutation primitives.

This module is intentionally additive.  Callers designate one existing directory
as trusted, and every destination mutation beneath it is anchored to the
retained directory descriptor rather than to a re-resolved pathname.
"""

from __future__ import annotations

import errno
import inspect
import os
import secrets
import stat
import unicodedata
from enum import Enum
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union


DEFAULT_DIRECTORY_MODE = 0o700
DEFAULT_FILE_MODE = 0o600
_COPY_BUFFER_SIZE = 128 * 1024
_TEMP_PREFIX = ".ophelia-tmp-"
_TestHook = Callable[[str, str], None]
_Path = Union[str, os.PathLike]


class FilesystemSafetyCode(str, Enum):
    """Stable classifications for fail-closed filesystem errors."""

    UNSUPPORTED_PLATFORM = "unsupported_platform"
    CLOSED_ROOT = "closed_root"
    INVALID_PATH = "invalid_path"
    INVALID_MODE = "invalid_mode"
    SYMLINK_COMPONENT = "symlink_component"
    MISSING_COMPONENT = "missing_component"
    NOT_DIRECTORY = "not_directory"
    UNSAFE_PARENT = "unsafe_parent"
    INVALID_DESTINATION = "invalid_destination"
    INVALID_SOURCE = "invalid_source"
    RACE_DETECTED = "race_detected"
    DURABILITY_UNAVAILABLE = "durability_unavailable"
    IO_FAILURE = "io_failure"


class FilesystemSafetyError(Exception):
    """A structured, non-contract error raised by the mutation boundary."""

    def __init__(
        self,
        code: FilesystemSafetyCode,
        operation: str,
        path: Optional[str],
        message: str,
    ) -> None:
        self.code = code
        self.operation = operation
        self.path = path
        self.message = message
        super().__init__("%s: %s" % (code.value, message))

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "code": self.code.value,
            "operation": self.operation,
            "path": self.path,
            "message": self.message,
        }


class ManagedRoot:
    """A secure mutation boundary rooted at a retained directory descriptor.

    The root pathname and its ancestors are trusted by the caller.  The root
    itself and every traversed destination parent must not be group- or
    world-writable.  Instances are not safe for concurrent use or close.
    """

    def __init__(
        self,
        root: _Path,
        *,
        _test_hook: Optional[_TestHook] = None,
    ) -> None:
        _require_platform_capabilities()
        root_path = _path_text(root, operation="open_root", destination=False)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(root_path, flags)
        except OSError as exc:
            code = _root_open_code(root_path, exc)
            raise FilesystemSafetyError(
                code,
                "open_root",
                root_path,
                _root_open_message(code),
            ) from exc

        try:
            try:
                root_stat = os.fstat(descriptor)
            except OSError as exc:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.IO_FAILURE,
                    "open_root",
                    root_path,
                    "could not inspect managed root",
                ) from exc
            if not stat.S_ISDIR(root_stat.st_mode):
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.NOT_DIRECTORY,
                    "open_root",
                    root_path,
                    "managed root must be an existing directory",
                )
            _require_safe_parent(root_stat, "open_root", root_path)
        except BaseException:
            os.close(descriptor)
            raise

        self._descriptor: Optional[int] = descriptor
        self._root_display = root_path
        self._test_hook = _test_hook

    @classmethod
    def open(
        cls,
        root: _Path,
        *,
        _test_hook: Optional[_TestHook] = None,
    ) -> "ManagedRoot":
        return cls(root, _test_hook=_test_hook)

    def __enter__(self) -> "ManagedRoot":
        self._require_open("enter")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is not None:
            self._descriptor = None
            os.close(descriptor)

    def fileno(self) -> int:
        return self._require_open("fileno")

    def mkdir(self, path: _Path, mode: int = DEFAULT_DIRECTORY_MODE) -> None:
        """Create exactly one directory below the managed root."""

        operation = "mkdir"
        components, display = _destination_components(path, operation)
        _validate_mode(mode, DEFAULT_DIRECTORY_MODE, operation, display)

        parent_fd, parent_components, name = self._open_parent(
            components,
            operation,
            display,
        )
        created_fd: Optional[int] = None
        try:
            self._call_hook("parent_opened", display)
            self._revalidate_parent(parent_fd, parent_components, operation, display)
            kind = _entry_kind(parent_fd, name, operation, display)
            if kind == "symlink":
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.SYMLINK_COMPONENT,
                    operation,
                    display,
                    "directory destination must not be a symlink",
                )
            if kind is not None:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.INVALID_DESTINATION,
                    operation,
                    display,
                    "directory destination already exists",
                )
            try:
                os.mkdir(name, mode, dir_fd=parent_fd)
                created_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                os.fchmod(created_fd, mode)
            except OSError as exc:
                raise _mutation_os_error(exc, operation, display, parent_fd, name) from exc
            _sync(created_fd, operation, display)
            _sync(parent_fd, operation, display)
        finally:
            if created_fd is not None:
                os.close(created_fd)
            os.close(parent_fd)

    def write_bytes(
        self,
        path: _Path,
        data: Union[bytes, bytearray, memoryview],
        mode: int = DEFAULT_FILE_MODE,
    ) -> None:
        """Atomically create or replace a regular file with bytes."""

        operation = "write_bytes"
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        immutable_data = bytes(data)

        def write_content(file_descriptor: int) -> None:
            _write_all(file_descriptor, immutable_data, operation, display)

        components, display = _destination_components(path, operation)
        _validate_mode(mode, DEFAULT_FILE_MODE, operation, display)
        self._atomic_file_mutation(
            components,
            display,
            operation,
            mode,
            write_content,
            require_existing=False,
        )

    def replace_bytes(
        self,
        path: _Path,
        data: Union[bytes, bytearray, memoryview],
        mode: int = DEFAULT_FILE_MODE,
    ) -> None:
        """Atomically replace an existing regular file with bytes."""

        operation = "replace_bytes"
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        immutable_data = bytes(data)

        def write_content(file_descriptor: int) -> None:
            _write_all(file_descriptor, immutable_data, operation, display)

        components, display = _destination_components(path, operation)
        _validate_mode(mode, DEFAULT_FILE_MODE, operation, display)
        self._atomic_file_mutation(
            components,
            display,
            operation,
            mode,
            write_content,
            require_existing=True,
        )

    def copy_in(
        self,
        source: _Path,
        destination: _Path,
        mode: int = DEFAULT_FILE_MODE,
    ) -> None:
        """Copy one externally opened regular file into the managed root."""

        operation = "copy_in"
        source_path = _path_text(source, operation=operation, destination=False)
        components, display = _destination_components(destination, operation)
        _validate_mode(mode, DEFAULT_FILE_MODE, operation, display)

        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            source_fd = os.open(source_path, flags)
        except OSError as exc:
            code = FilesystemSafetyCode.INVALID_SOURCE
            raise FilesystemSafetyError(
                code,
                operation,
                display,
                "copy source must be a non-symlink regular file",
            ) from exc
        try:
            try:
                source_stat = os.fstat(source_fd)
            except OSError as exc:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.IO_FAILURE,
                    operation,
                    display,
                    "could not inspect copy source",
                ) from exc
            if not stat.S_ISREG(source_stat.st_mode):
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.INVALID_SOURCE,
                    operation,
                    display,
                    "copy source must be a regular file",
                )

            def copy_content(file_descriptor: int) -> None:
                while True:
                    try:
                        chunk = os.read(source_fd, _COPY_BUFFER_SIZE)
                    except InterruptedError:
                        continue
                    except OSError as exc:
                        raise FilesystemSafetyError(
                            FilesystemSafetyCode.IO_FAILURE,
                            operation,
                            display,
                            "could not read copy source",
                        ) from exc
                    if not chunk:
                        return
                    _write_all(file_descriptor, chunk, operation, display)

            self._atomic_file_mutation(
                components,
                display,
                operation,
                mode,
                copy_content,
                require_existing=False,
            )
        finally:
            os.close(source_fd)

    def _require_open(self, operation: str) -> int:
        descriptor = self._descriptor
        if descriptor is None:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.CLOSED_ROOT,
                operation,
                None,
                "managed root is closed",
            )
        return descriptor

    def _call_hook(self, event: str, display: str) -> None:
        if self._test_hook is not None:
            self._test_hook(event, display)

    def _open_parent(
        self,
        components: Tuple[str, ...],
        operation: str,
        display: str,
    ) -> Tuple[int, Tuple[str, ...], str]:
        parent_components = components[:-1]
        parent_fd = self._walk_directories(parent_components, operation, display)
        return parent_fd, parent_components, components[-1]

    def _walk_directories(
        self,
        components: Sequence[str],
        operation: str,
        display: str,
    ) -> int:
        root_fd = self._require_open(operation)
        try:
            current_fd = os.dup(root_fd)
        except OSError as exc:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.IO_FAILURE,
                operation,
                display,
                "could not duplicate managed root descriptor",
            ) from exc

        try:
            try:
                current_stat = os.fstat(current_fd)
            except OSError as exc:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.IO_FAILURE,
                    operation,
                    display,
                    "could not inspect destination parent",
                ) from exc
            _require_safe_parent(current_stat, operation, display)
            for component in components:
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise _traversal_os_error(
                        exc,
                        operation,
                        display,
                        current_fd,
                        component,
                    ) from exc
                try:
                    try:
                        next_stat = os.fstat(next_fd)
                    except OSError as exc:
                        raise FilesystemSafetyError(
                            FilesystemSafetyCode.IO_FAILURE,
                            operation,
                            display,
                            "could not inspect destination parent component",
                        ) from exc
                    if not stat.S_ISDIR(next_stat.st_mode):
                        raise FilesystemSafetyError(
                            FilesystemSafetyCode.NOT_DIRECTORY,
                            operation,
                            display,
                            "destination parent component is not a directory",
                        )
                    _require_safe_parent(next_stat, operation, display)
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def _revalidate_parent(
        self,
        pinned_fd: int,
        components: Sequence[str],
        operation: str,
        display: str,
    ) -> None:
        try:
            reached_fd = self._walk_directories(components, operation, display)
        except FilesystemSafetyError as exc:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.RACE_DETECTED,
                operation,
                display,
                "destination parent changed during mutation",
            ) from exc
        try:
            pinned = os.fstat(pinned_fd)
            reached = os.fstat(reached_fd)
            if (pinned.st_dev, pinned.st_ino) != (reached.st_dev, reached.st_ino):
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.RACE_DETECTED,
                    operation,
                    display,
                    "destination parent changed during mutation",
                )
        except OSError as exc:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.RACE_DETECTED,
                operation,
                display,
                "destination parent changed during mutation",
            ) from exc
        finally:
            os.close(reached_fd)

    def _atomic_file_mutation(
        self,
        components: Tuple[str, ...],
        display: str,
        operation: str,
        mode: int,
        write_content: Callable[[int], None],
        *,
        require_existing: bool,
    ) -> None:
        parent_fd, parent_components, name = self._open_parent(
            components,
            operation,
            display,
        )
        temporary_name: Optional[str] = None
        temporary_fd: Optional[int] = None
        committed = False
        try:
            self._call_hook("parent_opened", display)
            self._revalidate_parent(parent_fd, parent_components, operation, display)
            _require_regular_destination(
                parent_fd,
                name,
                operation,
                display,
                require_existing=require_existing,
            )

            temporary_name, temporary_fd = _create_temporary(
                parent_fd,
                operation,
                display,
            )
            try:
                os.fchmod(temporary_fd, mode)
            except OSError as exc:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.IO_FAILURE,
                    operation,
                    display,
                    "could not set temporary file mode",
                ) from exc
            write_content(temporary_fd)
            _sync(temporary_fd, operation, display)
            os.close(temporary_fd)
            temporary_fd = None

            self._call_hook("before_commit", display)
            self._revalidate_parent(parent_fd, parent_components, operation, display)
            _require_regular_destination(
                parent_fd,
                name,
                operation,
                display,
                require_existing=require_existing,
            )
            try:
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            except (TypeError, NotImplementedError) as exc:
                raise FilesystemSafetyError(
                    FilesystemSafetyCode.UNSUPPORTED_PLATFORM,
                    operation,
                    display,
                    "descriptor-relative atomic replacement is unavailable",
                ) from exc
            except OSError as exc:
                raise _mutation_os_error(exc, operation, display, parent_fd, name) from exc
            committed = True
            _sync(parent_fd, operation, display)
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name is not None and not committed:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            os.close(parent_fd)


def _require_platform_capabilities() -> None:
    missing = []
    if os.name != "posix":
        missing.append("POSIX")
    for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"):
        if not hasattr(os, flag):
            missing.append(flag)
    for function in (os.open, os.mkdir, os.stat, os.unlink, os.rename):
        if function not in os.supports_dir_fd:
            missing.append(getattr(function, "__name__", "dir_fd"))
    if os.stat not in os.supports_follow_symlinks:
        missing.append("stat(follow_symlinks)")
    try:
        replace_parameters = inspect.signature(os.replace).parameters
    except (TypeError, ValueError):
        replace_parameters = {}
    if "src_dir_fd" not in replace_parameters or "dst_dir_fd" not in replace_parameters:
        missing.append("replace(dir_fd)")
    for name in ("dup", "fchmod", "fstat", "fsync", "read", "write"):
        if not hasattr(os, name):
            missing.append(name)
    if missing:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.UNSUPPORTED_PLATFORM,
            "open_root",
            None,
            "required descriptor-relative filesystem operations are unavailable",
        )


def _path_text(path: _Path, *, operation: str, destination: bool) -> str:
    try:
        text = os.fspath(path)
    except TypeError as exc:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            None,
            "path must be text or a text path-like value",
        ) from exc
    if not isinstance(text, str):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            None,
            "bytes paths are not allowed",
        )
    if not text:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            text,
            "path must not be empty",
        )
    if "\x00" in text:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            text,
            "path must not contain control characters",
        )
    if destination and any(unicodedata.category(character) == "Cc" for character in text):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            text,
            "path must not contain control characters",
        )
    return text


def _destination_components(path: _Path, operation: str) -> Tuple[Tuple[str, ...], str]:
    display = _path_text(path, operation=operation, destination=True)
    if os.path.isabs(display) or display.startswith("/"):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            display,
            "destination path must be relative",
        )
    if display.endswith("/"):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            display,
            "destination path must not end with a separator",
        )
    components = tuple(display.split("/"))
    if any(not component for component in components):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            display,
            "destination path must not contain empty components",
        )
    if any(component in (".", "..") for component in components):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_PATH,
            operation,
            display,
            "destination path must not contain dot segments",
        )
    return components, display


def _validate_mode(
    mode: int,
    allowed: int,
    operation: str,
    display: str,
) -> None:
    if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode & ~allowed:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_MODE,
            operation,
            display,
            "mode must contain owner permission bits only",
        )


def _require_safe_parent(
    value: os.stat_result,
    operation: str,
    display: str,
) -> None:
    if value.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.UNSAFE_PARENT,
            operation,
            display,
            "destination parent must not be group- or world-writable",
        )


def _root_open_code(path: str, error: OSError) -> FilesystemSafetyCode:
    try:
        root_stat = os.lstat(path)
    except OSError:
        root_stat = None
    if root_stat is not None and stat.S_ISLNK(root_stat.st_mode):
        return FilesystemSafetyCode.SYMLINK_COMPONENT
    if error.errno in (errno.ENOTDIR,):
        return FilesystemSafetyCode.NOT_DIRECTORY
    if error.errno in (errno.ENOENT,):
        return FilesystemSafetyCode.MISSING_COMPONENT
    if error.errno in (errno.ELOOP,):
        return FilesystemSafetyCode.SYMLINK_COMPONENT
    return FilesystemSafetyCode.IO_FAILURE


def _root_open_message(code: FilesystemSafetyCode) -> str:
    if code == FilesystemSafetyCode.SYMLINK_COMPONENT:
        return "managed root must not be a symlink"
    if code == FilesystemSafetyCode.NOT_DIRECTORY:
        return "managed root must be a directory"
    if code == FilesystemSafetyCode.MISSING_COMPONENT:
        return "managed root must already exist"
    return "could not open managed root"


def _entry_kind(
    parent_fd: int,
    name: str,
    operation: str,
    display: str,
) -> Optional[str]:
    try:
        value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.IO_FAILURE,
            operation,
            display,
            "could not inspect destination entry",
        ) from exc
    if stat.S_ISLNK(value.st_mode):
        return "symlink"
    if stat.S_ISDIR(value.st_mode):
        return "directory"
    if stat.S_ISREG(value.st_mode):
        return "regular"
    return "special"


def _require_regular_destination(
    parent_fd: int,
    name: str,
    operation: str,
    display: str,
    *,
    require_existing: bool,
) -> None:
    kind = _entry_kind(parent_fd, name, operation, display)
    if kind == "symlink":
        raise FilesystemSafetyError(
            FilesystemSafetyCode.SYMLINK_COMPONENT,
            operation,
            display,
            "file destination must not be a symlink",
        )
    if kind is None and require_existing:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_DESTINATION,
            operation,
            display,
            "replacement destination must already exist",
        )
    if kind not in (None, "regular"):
        raise FilesystemSafetyError(
            FilesystemSafetyCode.INVALID_DESTINATION,
            operation,
            display,
            "file destination must be absent or a regular file",
        )


def _traversal_os_error(
    error: OSError,
    operation: str,
    display: str,
    parent_fd: int,
    component: str,
) -> FilesystemSafetyError:
    kind = _entry_kind(parent_fd, component, operation, display)
    if kind == "symlink" or error.errno == errno.ELOOP:
        code = FilesystemSafetyCode.SYMLINK_COMPONENT
        message = "destination parent component must not be a symlink"
    elif kind is None or error.errno == errno.ENOENT:
        code = FilesystemSafetyCode.MISSING_COMPONENT
        message = "destination parent component does not exist"
    elif kind != "directory" or error.errno == errno.ENOTDIR:
        code = FilesystemSafetyCode.NOT_DIRECTORY
        message = "destination parent component is not a directory"
    else:
        code = FilesystemSafetyCode.IO_FAILURE
        message = "could not open destination parent component"
    return FilesystemSafetyError(code, operation, display, message)


def _mutation_os_error(
    error: OSError,
    operation: str,
    display: str,
    parent_fd: int,
    name: str,
) -> FilesystemSafetyError:
    kind = _entry_kind(parent_fd, name, operation, display)
    if kind == "symlink" or error.errno == errno.ELOOP:
        code = FilesystemSafetyCode.SYMLINK_COMPONENT
        message = "destination must not be a symlink"
    elif error.errno == errno.EEXIST:
        code = FilesystemSafetyCode.INVALID_DESTINATION
        message = "destination already exists"
    else:
        code = FilesystemSafetyCode.IO_FAILURE
        message = "filesystem mutation failed"
    return FilesystemSafetyError(code, operation, display, message)


def _create_temporary(
    parent_fd: int,
    operation: str,
    display: str,
) -> Tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )
    for _ in range(128):
        name = _TEMP_PREFIX + secrets.token_hex(16)
        try:
            descriptor = os.open(name, flags, DEFAULT_FILE_MODE, dir_fd=parent_fd)
            return name, descriptor
        except FileExistsError:
            continue
        except OSError as exc:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.IO_FAILURE,
                operation,
                display,
                "could not create destination-local temporary file",
            ) from exc
    raise FilesystemSafetyError(
        FilesystemSafetyCode.IO_FAILURE,
        operation,
        display,
        "could not allocate a unique temporary file",
    )


def _write_all(
    file_descriptor: int,
    data: bytes,
    operation: str,
    display: str,
) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(file_descriptor, remaining)
        except InterruptedError:
            continue
        except OSError as exc:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.IO_FAILURE,
                operation,
                display,
                "could not write temporary file",
            ) from exc
        if written <= 0:
            raise FilesystemSafetyError(
                FilesystemSafetyCode.IO_FAILURE,
                operation,
                display,
                "temporary file write made no progress",
            )
        remaining = remaining[written:]


def _sync(file_descriptor: int, operation: str, display: str) -> None:
    try:
        os.fsync(file_descriptor)
    except OSError as exc:
        raise FilesystemSafetyError(
            FilesystemSafetyCode.DURABILITY_UNAVAILABLE,
            operation,
            display,
            "filesystem could not provide the required durability sync",
        ) from exc
