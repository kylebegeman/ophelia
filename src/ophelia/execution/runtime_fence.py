"""Durable per-runtime fencing for side effects.

The security boundary assumes the effective UID is dedicated to one trusted
Ophelia writer domain. Descriptor-relative, no-follow operations prevent other
UIDs from redirecting managed paths, but POSIX advisory locks and permissions
cannot contain a malicious same-UID process that renames or edits those paths.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union


PathLike = Union[str, os.PathLike]
FENCE_AREA_NAME = ".ophelia-runtime-fences"
FENCE_RECORD_NAME = "fence.json"
FENCE_LOCK_NAME = "mutation.lock"
MAX_RECORD_BYTES = 16 * 1024
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
SAME_UID_TRUST_ASSUMPTION = (
    "the effective UID is a trusted single-writer domain; malicious same-UID "
    "processes are outside this fence's containment guarantee"
)

_PATH_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_STORED_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class RuntimeFenceError(Exception):
    """Base error for runtime fence failures."""


class InvalidRuntimeFence(RuntimeFenceError, ValueError):
    """The requested fence contains invalid input."""


class RuntimeFenceRejected(RuntimeFenceError):
    """The durable monotonic record rejected this mutation attempt."""


class RuntimeFenceSafetyError(RuntimeFenceError):
    """The filesystem cannot safely host the runtime fence."""


class RuntimeFenceRecordError(RuntimeFenceError):
    """The durable fence record is malformed or cannot be persisted."""


class RuntimeSideEffectFence:
    """Hold exclusive permission to mutate one host/app/environment runtime.

    Instances are single-use context managers. The accepted record is durable
    before entering returns, and the OS lock remains held until exiting
    completes, including when the caller raises.
    """

    def __init__(
        self,
        runtime_root: PathLike,
        host_id: str,
        app: str,
        environment: str,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        self._runtime_root = _validate_runtime_root(runtime_root)
        self._host_id = _validate_stored_identifier(host_id, "host_id")
        self._app = _validate_path_identifier(app, "app")
        self._environment = _validate_path_identifier(environment, "environment")
        self._operation_id = _validate_stored_identifier(operation_id, "operation_id")
        self._owner_id = _validate_stored_identifier(owner_id, "owner_id")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise InvalidRuntimeFence("fencing_token must be a positive integer")
        self._fencing_token = fencing_token
        self._thread_key = os.path.abspath(os.fspath(self._runtime_root)) + "::" + "/".join(
            (self._host_id, self._app, self._environment)
        )
        self._thread_lock: Optional[threading.Lock] = None
        self._root_fd: Optional[int] = None
        self._scope_fd: Optional[int] = None
        self._lock_fd: Optional[int] = None
        self._entered = False
        self._accepted_record: Optional[Dict[str, Any]] = None

    @property
    def accepted_record(self) -> Optional[Dict[str, Any]]:
        """Return a copy of the accepted authoritative record, if entered."""

        if self._accepted_record is None:
            return None
        return dict(self._accepted_record)

    def __enter__(self) -> "RuntimeSideEffectFence":
        if self._entered:
            raise RuntimeFenceError("runtime fence instances are single-use")
        self._entered = True
        thread_lock = _thread_lock_for(self._thread_key)
        thread_lock.acquire()
        self._thread_lock = thread_lock
        try:
            self._root_fd = _open_runtime_root(self._runtime_root)
            area_fd = _open_or_create_private_directory(
                self._root_fd, FENCE_AREA_NAME, sync_parent=True
            )
            try:
                host_fd = _open_or_create_private_directory(
                    area_fd, self._host_id, sync_parent=True
                )
            finally:
                os.close(area_fd)
            try:
                app_fd = _open_or_create_private_directory(
                    host_fd, self._app, sync_parent=True
                )
            finally:
                os.close(host_fd)
            try:
                self._scope_fd = _open_or_create_private_directory(
                    app_fd, self._environment, sync_parent=True
                )
            finally:
                os.close(app_fd)

            self._lock_fd = _open_private_regular(
                self._scope_fd, FENCE_LOCK_NAME, writable=True, create=True
            )
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            current = _read_record(self._scope_fd)
            _require_scope(current, self._host_id, self._app, self._environment)
            _authorize(current, self._fencing_token, self._operation_id)
            accepted = {
                "schema_version": 1,
                "host_id": self._host_id,
                "app": self._app,
                "environment": self._environment,
                "operation_id": self._operation_id,
                "owner_id": self._owner_id,
                "fencing_token": self._fencing_token,
            }
            _persist_record(self._scope_fd, accepted)
            self._accepted_record = accepted
            return self
        except BaseException:
            self._release()
            raise

    def __exit__(self, *args: Any) -> None:
        self._release()

    def _release(self) -> None:
        lock_fd = self._lock_fd
        self._lock_fd = None
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        scope_fd = self._scope_fd
        self._scope_fd = None
        if scope_fd is not None:
            os.close(scope_fd)
        root_fd = self._root_fd
        self._root_fd = None
        if root_fd is not None:
            os.close(root_fd)
        thread_lock = self._thread_lock
        self._thread_lock = None
        if thread_lock is not None:
            thread_lock.release()


def _validate_runtime_root(value: PathLike) -> Path:
    if isinstance(value, bytes):
        raise InvalidRuntimeFence("runtime_root must be a filesystem path")
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise InvalidRuntimeFence("runtime_root must be a filesystem path") from exc
    if not isinstance(text, str) or not text or "\x00" in text:
        raise InvalidRuntimeFence("runtime_root must be a non-empty filesystem path")
    return Path(text)


def _validate_path_identifier(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or _PATH_IDENTIFIER.fullmatch(value) is None
    ):
        raise InvalidRuntimeFence("%s must be a lowercase DNS-style identifier" % field)
    return value


def _validate_stored_identifier(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or _STORED_IDENTIFIER.fullmatch(value) is None
        or "/" in value
        or "\\" in value
    ):
        raise InvalidRuntimeFence("%s must be a safe identifier" % field)
    return value


def _thread_lock_for(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _base_flags() -> int:
    return getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_runtime_root(path: Path) -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise RuntimeFenceSafetyError("platform lacks required no-follow operations")
    flags = os.O_RDONLY | os.O_DIRECTORY | _base_flags()
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise RuntimeFenceSafetyError(
            "runtime_root must be an existing real directory"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        _require_owned_directory(metadata, private=False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_owned_directory(metadata: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeFenceSafetyError("managed fence paths must be directories")
    if metadata.st_uid != os.geteuid():
        raise RuntimeFenceSafetyError("managed fence paths must be owned by the current UID")
    if not private and stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RuntimeFenceSafetyError(
            "runtime_root must not be writable by group or other users"
        )


def _open_or_create_private_directory(
    parent_fd: int, name: str, *, sync_parent: bool
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | _base_flags()
    created = False
    try:
        try:
            os.mkdir(name, DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_owned_directory(before, private=True)
        # Validate without following first. chmodat then makes mode-000 entries
        # traversable; the inode check below detects replacement before use.
        os.chmod(name, DIRECTORY_MODE, dir_fd=parent_fd)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except RuntimeFenceError:
        raise
    except OSError as exc:
        raise RuntimeFenceSafetyError(
            "managed fence directory must be a real directory"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        _require_owned_directory(metadata, private=True)
        if (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeFenceSafetyError(
                "managed fence directory identity changed during validation"
            )
        os.fchmod(descriptor, DIRECTORY_MODE)
        os.fsync(descriptor)
        if created and sync_parent:
            os.fsync(parent_fd)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_regular(
    parent_fd: int, name: str, *, writable: bool, create: bool
) -> int:
    access = os.O_RDWR if writable else os.O_RDONLY
    flags = access | _base_flags() | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT
    before: Optional[os.stat_result]
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise RuntimeFenceSafetyError(
            "managed fence file could not be inspected"
        ) from exc
    if before is not None:
        _require_owned_regular(before)
        try:
            os.chmod(name, FILE_MODE, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeFenceSafetyError(
                "managed fence file permissions could not be repaired"
            ) from exc
    try:
        descriptor = os.open(name, flags, FILE_MODE, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeFenceSafetyError(
            "managed fence file must be a real regular file"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        _require_owned_regular(metadata)
        if before is not None and (metadata.st_dev, metadata.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise RuntimeFenceSafetyError(
                "managed fence file identity changed during validation"
            )
        os.fchmod(descriptor, FILE_MODE)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_owned_regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeFenceSafetyError(
            "managed fence files must be private regular files"
        )
    if metadata.st_uid != os.geteuid():
        raise RuntimeFenceSafetyError(
            "managed fence files must be owned by the current UID"
        )


def _read_record(scope_fd: int) -> Optional[Dict[str, Any]]:
    try:
        descriptor = _open_private_regular(
            scope_fd, FENCE_RECORD_NAME, writable=False, create=False
        )
    except RuntimeFenceSafetyError as exc:
        cause = exc.__cause__
        if isinstance(cause, FileNotFoundError):
            return None
        raise
    try:
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining:
            try:
                chunk = os.read(descriptor, min(4096, remaining))
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_RECORD_BYTES:
            raise RuntimeFenceRecordError("runtime fence record exceeds the size limit")
    finally:
        os.close(descriptor)
    try:
        decoded = raw.decode("utf-8")
        record = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeFenceRecordError("runtime fence record is malformed") from exc
    _validate_record(record)
    return record


def _unique_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate record field")
        result[key] = value
    return result


def _validate_record(record: Any) -> None:
    expected = {
        "schema_version",
        "host_id",
        "app",
        "environment",
        "operation_id",
        "owner_id",
        "fencing_token",
    }
    if not isinstance(record, dict) or set(record) != expected:
        raise RuntimeFenceRecordError("runtime fence record has an invalid schema")
    if record["schema_version"] != 1:
        raise RuntimeFenceRecordError("runtime fence record has an invalid schema")
    try:
        _validate_stored_identifier(record["host_id"], "host_id")
        _validate_path_identifier(record["app"], "app")
        _validate_path_identifier(record["environment"], "environment")
        _validate_stored_identifier(record["operation_id"], "operation_id")
        _validate_stored_identifier(record["owner_id"], "owner_id")
    except InvalidRuntimeFence as exc:
        raise RuntimeFenceRecordError("runtime fence record has an invalid schema") from exc
    token = record["fencing_token"]
    if isinstance(token, bool) or not isinstance(token, int) or token <= 0:
        raise RuntimeFenceRecordError("runtime fence record has an invalid schema")


def _require_scope(
    record: Optional[Dict[str, Any]], host_id: str, app: str, environment: str
) -> None:
    if record is None:
        return
    if (
        record["host_id"] != host_id
        or record["app"] != app
        or record["environment"] != environment
    ):
        raise RuntimeFenceRecordError("runtime fence record scope does not match its path")


def _authorize(
    current: Optional[Dict[str, Any]], fencing_token: int, operation_id: str
) -> None:
    if current is None:
        return
    current_token = current["fencing_token"]
    if fencing_token < current_token:
        raise RuntimeFenceRejected("runtime mutation rejected by a newer fence")
    if fencing_token == current_token and operation_id != current["operation_id"]:
        raise RuntimeFenceRejected(
            "runtime mutation fence is assigned to another operation"
        )


def _persist_record(scope_fd: int, record: Dict[str, Any]) -> None:
    encoded = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_RECORD_BYTES:
        raise RuntimeFenceRecordError("runtime fence record exceeds the size limit")
    temp_name = ".fence-%s.tmp" % secrets.token_hex(12)
    descriptor: Optional[int] = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _base_flags()
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(temp_name, flags, FILE_MODE, dir_fd=scope_fd)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise RuntimeFenceSafetyError(
                "temporary fence record must be a private regular file"
            )
        os.fchmod(descriptor, FILE_MODE)
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(descriptor, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise RuntimeFenceRecordError("runtime fence record could not be written")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        _reject_unsafe_existing_record(scope_fd)
        os.replace(
            temp_name,
            FENCE_RECORD_NAME,
            src_dir_fd=scope_fd,
            dst_dir_fd=scope_fd,
        )
        os.fsync(scope_fd)
    except RuntimeFenceError:
        raise
    except OSError as exc:
        raise RuntimeFenceRecordError(
            "runtime fence record could not be persisted durably"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=scope_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _reject_unsafe_existing_record(scope_fd: int) -> None:
    try:
        metadata = os.stat(
            FENCE_RECORD_NAME, dir_fd=scope_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeFenceSafetyError(
            "runtime fence record could not be inspected"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise RuntimeFenceSafetyError(
            "runtime fence record must be a private regular file"
        )
