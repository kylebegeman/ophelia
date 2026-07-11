"""Fail-closed, extraction-free inspection for tar-family artifacts."""

from __future__ import annotations

import os
import re
import stat
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Optional, Tuple, Union


_DIAGNOSTIC_LIMIT = 240
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ArchiveInspectionError(ValueError):
    """A bounded archive rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        clean = " ".join(str(message).split())
        self.message = clean[:_DIAGNOSTIC_LIMIT]
        super().__init__("%s: %s" % (code, self.message))


@dataclass(frozen=True)
class ArchiveLimits:
    max_source_bytes: int = 1024 * 1024 * 1024
    max_members: int = 100_000
    max_total_unpacked_bytes: int = 4 * 1024 * 1024 * 1024
    max_member_bytes: int = 1024 * 1024 * 1024
    max_path_bytes: int = 4096
    max_path_depth: int = 64
    max_expansion_ratio: int = 100


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    kind: str
    size: int


@dataclass(frozen=True)
class ArchiveInspection:
    source_bytes: int
    member_count: int
    total_unpacked_bytes: int
    members: Tuple[ArchiveMember, ...]


@dataclass(frozen=True)
class _SourceSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _reject(code: str, message: str) -> None:
    raise ArchiveInspectionError(code, message)


def _validate_limits(limits: ArchiveLimits) -> None:
    if not isinstance(limits, ArchiveLimits):
        _reject("invalid_archive_limits", "limits must be an ArchiveLimits value.")
    for field in (
        "max_source_bytes",
        "max_members",
        "max_total_unpacked_bytes",
        "max_member_bytes",
        "max_path_bytes",
        "max_path_depth",
        "max_expansion_ratio",
    ):
        value = getattr(limits, field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _reject("invalid_archive_limits", "%s must be a positive integer." % field)


def _snapshot(source: BinaryIO) -> _SourceSnapshot:
    try:
        stat = os.fstat(source.fileno())
    except (OSError, ValueError):
        _reject("archive_source_stat_failed", "archive source metadata could not be read.")
    return _SourceSnapshot(
        device=stat.st_dev,
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )


def _canonical_path(name: Any, *, is_directory: bool, limits: ArchiveLimits) -> str:
    if not isinstance(name, str) or not name:
        _reject("archive_member_path_invalid", "archive member path is empty or invalid.")
    if "\\" in name:
        _reject("archive_member_path_backslash", "archive member path contains a backslash.")
    if name.startswith("/") or name.startswith("//") or _DRIVE_PATH.match(name):
        _reject("archive_member_path_absolute", "archive member path is absolute.")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        _reject("archive_member_path_control", "archive member path contains a control character.")

    candidate = name[2:] if name.startswith("./") else name
    if candidate.startswith("./"):
        _reject("archive_member_path_invalid", "archive member path has repeated dot prefixes.")
    if is_directory and candidate.endswith("/"):
        candidate = candidate[:-1]
    candidate = unicodedata.normalize("NFC", candidate)
    segments = candidate.split("/")
    if not candidate or any(segment in ("", ".", "..") for segment in segments):
        _reject("archive_member_path_invalid", "archive member path has a non-canonical segment.")
    try:
        path_bytes = len(candidate.encode("utf-8"))
    except UnicodeEncodeError:
        _reject("archive_member_path_invalid", "archive member path is not valid UTF-8.")
    if path_bytes > limits.max_path_bytes:
        _reject("archive_member_path_too_long", "archive member path exceeds the configured limit.")
    if len(segments) > limits.max_path_depth:
        _reject("archive_member_path_too_deep", "archive member path exceeds the configured depth.")
    return candidate


def _unsupported_format(path: Path, source: BinaryIO) -> None:
    suffix = path.name.lower()
    try:
        source.seek(0)
        magic = source.read(4)
        source.seek(0)
    except OSError:
        _reject("archive_source_read_failed", "archive source header could not be read.")
    if suffix.endswith((".tar.zst", ".tar.zstd", ".tzst")) or magic == _ZSTD_MAGIC:
        _reject("archive_compression_unsupported", "zstd-compressed tar archives are unsupported.")


def _member_kind(info: tarfile.TarInfo) -> str:
    if info.type == tarfile.DIRTYPE:
        return "directory"
    if info.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
        return "file"
    if info.issym():
        _reject("archive_member_symlink", "archive contains a symbolic link.")
    if info.islnk():
        _reject("archive_member_hardlink", "archive contains a hard link.")
    if info.ischr() or info.isblk():
        _reject("archive_member_device", "archive contains a device member.")
    if info.isfifo():
        _reject("archive_member_fifo", "archive contains a FIFO member.")
    _reject("archive_member_special", "archive contains an unsupported special member.")
    raise AssertionError("unreachable")


def _inspect_open_source(
    path: Path,
    source: BinaryIO,
    source_bytes: int,
    limits: ArchiveLimits,
) -> ArchiveInspection:
    _unsupported_format(path, source)
    members = []
    destinations = set()
    file_destinations = set()
    required_directories = set()
    total = 0
    try:
        archive = tarfile.open(fileobj=source, mode="r:*")
    except (tarfile.TarError, EOFError, OSError):
        _reject("archive_malformed", "archive is malformed, truncated, or unsupported.")

    try:
        try:
            for info in archive:
                if len(members) >= limits.max_members:
                    _reject("archive_member_count_exceeded", "archive member count exceeds the configured limit.")
                kind = _member_kind(info)
                if (
                    getattr(info, "sparse", None) is not None
                    or any(str(key).startswith(("GNU.sparse", "SCHILY.realsize")) for key in info.pax_headers)
                ):
                    _reject("archive_member_sparse", "archive contains sparse member metadata.")
                size = info.size
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    _reject("archive_member_size_invalid", "archive member has an invalid declared size.")
                if kind == "directory" and size != 0:
                    _reject("archive_member_size_invalid", "archive directory has a non-zero declared size.")
                if size > limits.max_member_bytes:
                    _reject("archive_member_size_exceeded", "archive member exceeds the configured size limit.")
                canonical = _canonical_path(info.name, is_directory=kind == "directory", limits=limits)
                if canonical in destinations:
                    _reject("archive_member_duplicate", "archive has duplicate canonical destinations.")
                parts = canonical.split("/")
                ancestors = {
                    "/".join(parts[:index]) for index in range(1, len(parts))
                }
                if ancestors.intersection(file_destinations):
                    _reject(
                        "archive_member_path_conflict",
                        "archive member is nested beneath a file destination.",
                    )
                if kind == "file" and canonical in required_directories:
                    _reject(
                        "archive_member_path_conflict",
                        "archive file destination is already required as a directory.",
                    )
                destinations.add(canonical)
                required_directories.update(ancestors)
                if kind == "file":
                    file_destinations.add(canonical)
                total += size
                if total > limits.max_total_unpacked_bytes:
                    _reject("archive_total_size_exceeded", "archive declared size exceeds the configured limit.")
                if total > source_bytes * limits.max_expansion_ratio:
                    _reject("archive_expansion_ratio_exceeded", "archive declared expansion ratio exceeds the configured limit.")
                members.append(ArchiveMember(path=canonical, kind=kind, size=size))
        except (tarfile.TarError, EOFError, OSError):
            _reject("archive_malformed", "archive is malformed, truncated, or unsupported.")
    finally:
        archive.close()

    return ArchiveInspection(
        source_bytes=source_bytes,
        member_count=len(members),
        total_unpacked_bytes=total,
        members=tuple(members),
    )


def inspect_archive(
    archive_path: Union[str, Path],
    limits: Optional[ArchiveLimits] = None,
) -> ArchiveInspection:
    """Inspect tar headers and quotas without reading payloads or extracting members."""

    effective_limits = limits if limits is not None else ArchiveLimits()
    _validate_limits(effective_limits)
    path = Path(archive_path)
    if not hasattr(os, "O_NOFOLLOW"):
        _reject(
            "archive_source_platform_unsupported",
            "this platform cannot safely open archive sources without following links.",
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except (OSError, ValueError):
        _reject("archive_source_open_failed", "archive source could not be opened.")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _reject("archive_source_not_regular", "archive source must be a regular file.")
        source = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise

    with source:
        before = _snapshot(source)
        if before.size > effective_limits.max_source_bytes:
            _reject("archive_source_size_exceeded", "archive source exceeds the configured size limit.")
        if before.size <= 0:
            _reject("archive_malformed", "archive is empty.")
        pending_error = None
        result = None
        try:
            result = _inspect_open_source(path, source, before.size, effective_limits)
        except ArchiveInspectionError as error:
            pending_error = error
        after = _snapshot(source)
        if after != before:
            _reject("archive_source_mutated", "archive source changed during inspection.")
        if pending_error is not None:
            raise pending_error
        assert result is not None
        return result
