"""Encrypted host-control backups and clean-root recovery."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..domain import canonical_digest
from ..execution import SQLiteOperationJournal
from ..execution.subprocesses import SubprocessRunner
from ..validation.artifacts import ArchiveLimits, inspect_archive
from ..version import package_version
from .config import DaemonConfig
from .install import _secure_write, _sync_directory


_BACKUP_ID = re.compile(r"^backup_[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_HOST_ID = re.compile(r"^host_[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXCLUDED_TOP_LEVEL = {"identity", "inbox", "recovery-staging", "run"}
_EXCLUDED_DATABASE_FILES = {
    "host-state/operations.db",
    "host-state/operations.db-shm",
    "host-state/operations.db-wal",
}
_ARCHIVE_OVERHEAD_BYTES = 64 * 1024 * 1024


class HostRecoveryError(RuntimeError):
    pass


def host_backup_plan(
    config: DaemonConfig,
    journal: SQLiteOperationJournal,
    *,
    backup_id: str,
    destination_root: Path,
) -> Dict[str, Any]:
    _backup_id(backup_id)
    destination = _allowed_destination(config, destination_root)
    blockers: List[str] = []
    age = shutil.which("age")
    if age is None:
        blockers.append("age_binary_missing")
    if config.recovery_age_recipient is None:
        blockers.append("recovery_age_recipient_missing")
    try:
        host = _host_state(journal, config.host_id)
    except KeyError:
        host = None
        blockers.append("daemon_host_state_missing")
    if host is not None and not (host["maintenance_mode"] and host["drained"]):
        blockers.append("host_must_be_drained_and_in_maintenance")
    if journal.list_recoverable(limit=1):
        blockers.append("recoverable_operation_present")
    active_runs = _active_workload_runs(journal)
    if active_runs:
        blockers.append("active_workload_run_present")
    inventory = _inventory(config.runtime_root, config.recovery_max_bytes)
    target = destination / config.host_id / backup_id
    if _paths_overlap(target, config.runtime_root):
        blockers.append("backup_target_overlaps_runtime_root")
    observations = {
        "age_binary": age,
        "runtime_inventory_digest": inventory["digest"],
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "database_integrity": _database_integrity(journal.database_path),
        "target_present": target.exists() or target.is_symlink(),
    }
    if observations["target_present"]:
        try:
            existing = _load_external_manifest(target / "manifest.json")
        except HostRecoveryError:
            blockers.append("backup_target_conflicts")
        else:
            if (
                existing.get("backup_id") != backup_id
                or existing.get("host_id") != config.host_id
                or existing.get("recipient_digest")
                != _text_digest(config.recovery_age_recipient)
            ):
                blockers.append("backup_target_conflicts")
    if target.parent.exists() and not _private_directory(target.parent):
        blockers.append("backup_host_root_unsafe")
    exact = {
        "schema_version": 1,
        "kind": "ophelia.host-backup-plan",
        "host_id": config.host_id,
        "backup_id": backup_id,
        "runtime_root": str(config.runtime_root),
        "destination_root": str(destination),
        "target": str(target),
        "recipient_digest": _text_digest(config.recovery_age_recipient),
        "observations": observations,
        "blockers": blockers,
    }
    return {
        **exact,
        "can_apply": not blockers,
        "confirmation_token": None if blockers else canonical_digest(exact)[7:31],
    }


def create_host_backup(
    config: DaemonConfig,
    journal: SQLiteOperationJournal,
    *,
    backup_id: str,
    destination_root: Path,
    confirmation: Optional[str] = None,
    runner: Optional[SubprocessRunner] = None,
    trusted_remote: bool = False,
) -> Dict[str, Any]:
    plan = host_backup_plan(
        config,
        journal,
        backup_id=backup_id,
        destination_root=destination_root,
    )
    if not plan["can_apply"]:
        raise HostRecoveryError("Host backup plan is blocked: %s." % ", ".join(plan["blockers"]))
    if not trusted_remote and confirmation != plan["confirmation_token"]:
        raise HostRecoveryError("Host backup confirmation does not match the exact plan.")
    target = Path(plan["target"])
    if target.is_dir() and not target.is_symlink():
        manifest = _load_external_manifest(target / "manifest.json")
        _verify_external_object(target, manifest)
        return _backup_receipt(manifest, target, replayed=True)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _private_directory(target.parent):
        raise HostRecoveryError("Backup host root is unavailable or unsafe.")
    staging_root = config.runtime_root / "recovery-staging"
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(staging_root, 0o700)
    command = runner or SubprocessRunner()
    with tempfile.TemporaryDirectory(prefix="backup-", dir=staging_root) as directory:
        staging = Path(directory)
        payload = staging / "payload"
        payload.mkdir(mode=0o700)
        entries = _snapshot_runtime(
            config.runtime_root,
            payload,
            journal.database_path,
            maximum_bytes=config.recovery_max_bytes,
        )
        _validate_entries(entries)
        internal = {
            "schema_version": 1,
            "kind": "ophelia.host-recovery-manifest",
            "host_id": config.host_id,
            "backup_id": backup_id,
            "created_at": _utc_now(),
            "ophelia_version": package_version(),
            "entries": entries,
            "entries_digest": canonical_digest(entries),
        }
        _secure_write(
            staging / "recovery-manifest.json",
            (json.dumps(internal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )
        archive = staging / "recovery.tar.gz"
        with tarfile.open(
            archive, "w:gz", format=tarfile.PAX_FORMAT, dereference=True
        ) as bundle:
            bundle.add(staging / "recovery-manifest.json", arcname="recovery-manifest.json")
            bundle.add(payload, arcname="payload", recursive=True)
        encrypted = staging / "recovery.tar.gz.age"
        command.run(
            [
                shutil.which("age") or "age",
                "--encrypt",
                "--recipient",
                str(config.recovery_age_recipient),
                "--output",
                str(encrypted),
                str(archive),
            ],
            timeout_seconds=86400,
        )
        if encrypted.is_symlink() or not encrypted.is_file() or encrypted.stat().st_size == 0:
            raise HostRecoveryError("Age did not produce a valid encrypted backup object.")
        if encrypted.stat().st_size > config.recovery_max_bytes + _ARCHIVE_OVERHEAD_BYTES:
            raise HostRecoveryError("Encrypted backup object exceeds its bounded overhead.")
        external = {
            "schema_version": 1,
            "kind": "ophelia.host-backup-object",
            "host_id": config.host_id,
            "backup_id": backup_id,
            "created_at": internal["created_at"],
            "ophelia_version": package_version(),
            "encryption": "age",
            "recipient_digest": _text_digest(config.recovery_age_recipient),
            "object_name": "recovery.tar.gz.age",
            "object_digest": _digest_file(encrypted),
            "object_bytes": encrypted.stat().st_size,
            "entries_digest": internal["entries_digest"],
        }
        publish = target.parent / ("." + backup_id + ".tmp-" + os.urandom(6).hex())
        publish.mkdir(mode=0o700)
        try:
            _copy_regular(encrypted, publish / external["object_name"], mode=0o600)
            _secure_write(
                publish / "manifest.json",
                (json.dumps(external, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                mode=0o600,
            )
            os.replace(publish, target)
            _sync_directory(target.parent)
        finally:
            if publish.exists():
                shutil.rmtree(publish, ignore_errors=True)
    return _backup_receipt(external, target, replayed=False)


def clean_host_restore_plan(
    *,
    backup_root: Path,
    identity_file: Path,
    target_runtime_root: Path,
    maximum_bytes: int = 64 * 1024 * 1024 * 1024,
) -> Dict[str, Any]:
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 1024 * 1024 <= maximum_bytes <= 1024 * 1024 * 1024 * 1024
    ):
        raise HostRecoveryError("maximum_bytes must be from 1 MiB through 1 TiB.")
    root = _real_directory(backup_root, "backup_root")
    identity = _private_identity(identity_file)
    requested_target = _absolute_unresolved(target_runtime_root, "target_runtime_root")
    target_parent = _trusted_directory(
        requested_target.parent, "target_runtime_root parent"
    )
    target = target_parent / requested_target.name
    manifest = _load_external_manifest(root / "manifest.json")
    blockers = []
    if shutil.which("age") is None:
        blockers.append("age_binary_missing")
    target_state = _restore_target_state(target)
    if target_state == "occupied":
        blockers.append("target_runtime_root_must_be_absent_or_empty")
    if int(manifest["object_bytes"]) > maximum_bytes + _ARCHIVE_OVERHEAD_BYTES:
        blockers.append("encrypted_object_exceeds_restore_limit")
    try:
        _verify_external_object(root, manifest)
    except HostRecoveryError:
        blockers.append("encrypted_object_integrity_failed")
    observations = {
        "manifest_digest": _digest_file(root / "manifest.json"),
        "identity_digest": _digest_file(identity),
        "object_digest": manifest["object_digest"],
        "object_bytes": manifest["object_bytes"],
        "target_state": target_state,
    }
    exact = {
        "schema_version": 1,
        "kind": "ophelia.clean-host-restore-plan",
        "backup_root": str(root),
        "identity_file": str(identity),
        "target_runtime_root": str(target),
        "backup_id": manifest["backup_id"],
        "host_id": manifest["host_id"],
        "maximum_bytes": maximum_bytes,
        "observations": observations,
        "blockers": blockers,
    }
    return {
        **exact,
        "can_apply": not blockers,
        "confirmation_token": None if blockers else canonical_digest(exact)[7:31],
    }


def apply_clean_host_restore(
    plan: Dict[str, Any],
    confirmation: str,
    *,
    runner: Optional[SubprocessRunner] = None,
    owner_name: Optional[str] = "ophelia",
    require_root: bool = True,
) -> Dict[str, Any]:
    if require_root and os.geteuid() != 0:
        raise HostRecoveryError("Clean-host restore requires root authority.")
    refreshed = clean_host_restore_plan(
        backup_root=Path(plan["backup_root"]),
        identity_file=Path(plan["identity_file"]),
        target_runtime_root=Path(plan["target_runtime_root"]),
        maximum_bytes=int(plan["maximum_bytes"]),
    )
    if not refreshed["can_apply"] or confirmation != refreshed["confirmation_token"]:
        raise HostRecoveryError("Clean-host restore inputs changed or confirmation is invalid.")
    backup_root = Path(refreshed["backup_root"])
    target = Path(refreshed["target_runtime_root"])
    manifest = _load_external_manifest(backup_root / "manifest.json")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = target.parent / ("." + target.name + ".restore-" + os.urandom(6).hex())
    staging.mkdir(mode=0o700)
    command = runner or SubprocessRunner()
    try:
        archive = staging / "recovery.tar.gz"
        command.run(
            [
                shutil.which("age") or "age",
                "--decrypt",
                "--identity",
                refreshed["identity_file"],
                "--output",
                str(archive),
                str(backup_root / manifest["object_name"]),
            ],
            timeout_seconds=86400,
        )
        if (
            archive.is_symlink()
            or not archive.is_file()
            or archive.stat().st_size == 0
            or archive.stat().st_size > int(plan["maximum_bytes"]) + _ARCHIVE_OVERHEAD_BYTES
        ):
            raise HostRecoveryError("Decrypted recovery archive is unavailable or too large.")
        inspect_archive(
            archive,
            ArchiveLimits(
                max_source_bytes=int(plan["maximum_bytes"]) + _ARCHIVE_OVERHEAD_BYTES,
                max_members=100010,
                max_total_unpacked_bytes=int(plan["maximum_bytes"])
                + _ARCHIVE_OVERHEAD_BYTES,
                max_member_bytes=int(plan["maximum_bytes"]),
                max_path_bytes=4112,
                max_path_depth=66,
                max_expansion_ratio=int(plan["maximum_bytes"]),
            ),
        )
        extracted = staging / "extracted"
        extracted.mkdir(mode=0o700)
        _extract_regular_archive(archive, extracted)
        internal = _load_internal_manifest(extracted / "recovery-manifest.json")
        if (
            internal["backup_id"] != manifest["backup_id"]
            or internal["host_id"] != manifest["host_id"]
            or internal["entries_digest"] != manifest["entries_digest"]
        ):
            raise HostRecoveryError("Encrypted recovery manifest conflicts with object evidence.")
        if _entries_total_bytes(internal["entries"]) > int(plan["maximum_bytes"]):
            raise HostRecoveryError("Encrypted recovery payload exceeds its restore limit.")
        payload = extracted / "payload"
        _verify_payload(payload, internal["entries"])
        _restore_symlinks(payload, internal["entries"])
        _validate_restored_database(
            payload / "host-state" / "operations.db", manifest["host_id"]
        )
        receipt = {
            "schema_version": 1,
            "kind": "ophelia.clean-host-restore-receipt",
            "status": "succeeded",
            "backup_id": manifest["backup_id"],
            "host_id": manifest["host_id"],
            "target_runtime_root": str(target),
            "entries_digest": internal["entries_digest"],
            "restored_at": _utc_now(),
            "identity_restored": False,
        }
        _secure_write(
            payload / "host-state" / "clean-host-restore.json",
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )
        if owner_name is not None:
            identity = pwd.getpwnam(owner_name)
            _chown_tree(payload, identity.pw_uid, identity.pw_gid)
        current_target_state = _restore_target_state(target)
        if (
            current_target_state == "occupied"
            or current_target_state != refreshed["observations"]["target_state"]
        ):
            raise HostRecoveryError("Target runtime root changed after restore planning.")
        if current_target_state == "empty":
            target.rmdir()
        os.replace(payload, target)
        _sync_directory(target.parent)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def latest_backup_status(config: DaemonConfig) -> Dict[str, Any]:
    latest = None
    for root in config.recovery_backup_roots:
        host_root = root / config.host_id
        if host_root.is_symlink() or not host_root.is_dir():
            continue
        for path in host_root.glob("backup_*/manifest.json"):
            try:
                value = _load_external_manifest(path)
                _verify_external_object_presence(path.parent, value)
            except HostRecoveryError:
                continue
            if (
                value["host_id"] != config.host_id
                or value["recipient_digest"]
                != _text_digest(config.recovery_age_recipient)
            ):
                continue
            if latest is None or value["created_at"] > latest["created_at"]:
                latest = value
    if latest is None:
        return {"status": "missing", "latest": None}
    created = datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))
    age = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    return {
        "status": "stale" if age > config.recovery_freshness_seconds else "current",
        "age_seconds": age,
        "latest": {
            "backup_id": latest["backup_id"],
            "created_at": latest["created_at"],
            "object_digest": latest["object_digest"],
            "object_bytes": latest["object_bytes"],
        },
    }


def _snapshot_runtime(
    runtime_root: Path,
    payload: Path,
    database_path: Path,
    *,
    maximum_bytes: int,
) -> List[Dict[str, Any]]:
    entries = []
    total = 0
    for source, relative, kind in _walk_runtime(runtime_root):
        destination = payload / relative
        mode = stat.S_IMODE(source.lstat().st_mode) & 0o777
        if kind == "directory":
            destination.mkdir(mode=mode or 0o700, parents=True, exist_ok=True)
            entries.append({"path": relative.as_posix(), "kind": kind, "mode": mode})
        elif kind == "file":
            size = source.lstat().st_size
            total += size
            if total > maximum_bytes:
                raise HostRecoveryError("Host backup exceeds its configured byte limit.")
            _copy_regular(source, destination, mode=mode or 0o600)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "kind": kind,
                    "mode": mode,
                    "size": size,
                    "sha256": _digest_file(destination),
                }
            )
        else:
            resolved = source.resolve(strict=True)
            try:
                target_relative = resolved.relative_to(runtime_root.resolve(strict=True))
            except ValueError as exc:
                raise HostRecoveryError("Runtime symlink escapes the managed root.") from exc
            if (
                target_relative.parts[0] in _EXCLUDED_TOP_LEVEL
                or target_relative.as_posix()
                in _EXCLUDED_DATABASE_FILES - {"host-state/operations.db"}
            ):
                raise HostRecoveryError(
                    "Runtime symlink targets data excluded from host recovery."
                )
            entries.append(
                {
                    "path": relative.as_posix(),
                    "kind": "symlink",
                    "target": target_relative.as_posix(),
                }
            )
    database_target = payload / "host-state" / "operations.db"
    database_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_connection = sqlite3.connect(
        database_path.resolve(strict=True).as_uri() + "?mode=ro", uri=True
    )
    destination_connection = sqlite3.connect(str(database_target))
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(database_target, 0o600)
    total += database_target.stat().st_size
    if total > maximum_bytes:
        raise HostRecoveryError("Host backup exceeds its configured byte limit.")
    database_entry = {
        "path": "host-state/operations.db",
        "kind": "file",
        "mode": 0o600,
        "size": database_target.stat().st_size,
        "sha256": _digest_file(database_target),
    }
    entries.append(database_entry)
    return sorted(entries, key=lambda item: item["path"])


def _walk_runtime(runtime_root: Path) -> Iterable[Tuple[Path, Path, str]]:
    root = runtime_root.resolve(strict=True)
    for current_text, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        if current == root:
            directory_names[:] = [
                name for name in directory_names if name not in _EXCLUDED_TOP_LEVEL
            ]
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            source = current / name
            relative = source.relative_to(root)
            metadata = source.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                yield source, relative, "symlink"
            elif stat.S_ISDIR(metadata.st_mode):
                yield source, relative, "directory"
            else:
                raise HostRecoveryError("Runtime contains an unsupported special file.")
        for name in file_names:
            source = current / name
            relative = source.relative_to(root)
            if relative.as_posix() in _EXCLUDED_DATABASE_FILES:
                continue
            metadata = source.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                yield source, relative, "symlink"
            elif stat.S_ISREG(metadata.st_mode):
                yield source, relative, "file"
            else:
                raise HostRecoveryError("Runtime contains an unsupported special file.")


def _inventory(runtime_root: Path, maximum_bytes: int) -> Dict[str, Any]:
    values = []
    total = 0
    files = 0
    for source, relative, kind in _walk_runtime(runtime_root):
        item = {"path": relative.as_posix(), "kind": kind}
        if kind == "file":
            size = source.lstat().st_size
            total += size
            files += 1
            item.update({"size": size, "sha256": _digest_file(source)})
        elif kind == "symlink":
            item["target"] = os.readlink(source)
        values.append(item)
    if total > maximum_bytes:
        raise HostRecoveryError("Host recovery inventory exceeds its configured byte limit.")
    return {
        "digest": canonical_digest(values),
        "file_count": files,
        "total_bytes": total,
    }


def _extract_regular_archive(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            relative = Path(member.name)
            destination = target / relative
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise HostRecoveryError("Recovery archive contains a non-regular member.")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise HostRecoveryError("Recovery archive member is unavailable.")
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _verify_payload(payload: Path, entries: List[Dict[str, Any]]) -> None:
    expected_paths = {"recovery-manifest.json"}
    for entry in entries:
        relative = _safe_relative(entry.get("path"))
        target = payload / relative
        if entry.get("kind") == "directory":
            if target.is_symlink() or not target.is_dir():
                raise HostRecoveryError("Restored recovery directory is missing.")
            os.chmod(target, _safe_mode(entry.get("mode"), directory=True))
        elif entry.get("kind") == "file":
            if target.is_symlink() or not target.is_file():
                raise HostRecoveryError("Restored recovery file is missing.")
            if (
                target.stat().st_size != entry.get("size")
                or _digest_file(target) != entry.get("sha256")
            ):
                raise HostRecoveryError("Restored recovery file failed digest validation.")
            os.chmod(target, _safe_mode(entry.get("mode"), directory=False))
        elif entry.get("kind") == "symlink":
            if target.exists() or target.is_symlink():
                raise HostRecoveryError("Restored symlink destination is unexpectedly occupied.")
            _safe_relative(entry.get("target"))
        else:
            raise HostRecoveryError("Recovery manifest entry kind is invalid.")
        expected_paths.add("payload/" + relative.as_posix())
    actual = {
        path.relative_to(payload.parent).as_posix()
        for path in payload.parent.rglob("*")
        if path != payload
    }
    allowed_directories = {
        value
        for path in expected_paths
        for value in _parent_paths(path)
    }
    if actual - expected_paths - allowed_directories:
        raise HostRecoveryError("Recovery archive contains undeclared payload paths.")


def _restore_symlinks(payload: Path, entries: List[Dict[str, Any]]) -> None:
    for entry in entries:
        if entry.get("kind") != "symlink":
            continue
        link = payload / _safe_relative(entry["path"])
        target = payload / _safe_relative(entry["target"])
        link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        relative_target = os.path.relpath(target, start=link.parent)
        link.symlink_to(relative_target)


def _load_internal_manifest(path: Path) -> Dict[str, Any]:
    value = _read_json(path, maximum_bytes=16 * 1024 * 1024)
    expected = {
        "schema_version",
        "kind",
        "host_id",
        "backup_id",
        "created_at",
        "ophelia_version",
        "entries",
        "entries_digest",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != "ophelia.host-recovery-manifest"
        or not isinstance(value.get("entries"), list)
        or canonical_digest(value["entries"]) != value.get("entries_digest")
        or not _valid_host_id(value.get("host_id"))
        or not _valid_utc(value.get("created_at"))
        or not _valid_version(value.get("ophelia_version"))
        or _DIGEST.fullmatch(str(value.get("entries_digest"))) is None
    ):
        raise HostRecoveryError("Encrypted host recovery manifest is invalid.")
    _backup_id(value.get("backup_id"))
    _validate_entries(value["entries"])
    return value


def _load_external_manifest(path: Path) -> Dict[str, Any]:
    value = _read_json(path)
    expected = {
        "schema_version",
        "kind",
        "host_id",
        "backup_id",
        "created_at",
        "ophelia_version",
        "encryption",
        "recipient_digest",
        "object_name",
        "object_digest",
        "object_bytes",
        "entries_digest",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != "ophelia.host-backup-object"
        or value.get("encryption") != "age"
        or value.get("object_name") != "recovery.tar.gz.age"
        or isinstance(value.get("object_bytes"), bool)
        or not isinstance(value.get("object_bytes"), int)
        or value["object_bytes"] <= 0
        or value["object_bytes"] > 1024 * 1024 * 1024 * 1024 + _ARCHIVE_OVERHEAD_BYTES
        or _DIGEST.fullmatch(str(value.get("object_digest"))) is None
        or _DIGEST.fullmatch(str(value.get("entries_digest"))) is None
        or _DIGEST.fullmatch(str(value.get("recipient_digest"))) is None
        or not _valid_host_id(value.get("host_id"))
        or not _valid_utc(value.get("created_at"))
        or not _valid_version(value.get("ophelia_version"))
    ):
        raise HostRecoveryError("Host backup object manifest is invalid.")
    _backup_id(value.get("backup_id"))
    return value


def _verify_external_object(root: Path, manifest: Dict[str, Any]) -> None:
    object_path = _verify_external_object_presence(root, manifest)
    if _digest_file(object_path) != manifest["object_digest"]:
        raise HostRecoveryError("Encrypted host backup object digest does not match evidence.")


def _verify_external_object_presence(
    root: Path, manifest: Dict[str, Any]
) -> Path:
    object_path = root / manifest["object_name"]
    if object_path.is_symlink() or not object_path.is_file():
        raise HostRecoveryError("Encrypted host backup object is missing or unsafe.")
    if object_path.stat().st_size != manifest["object_bytes"]:
        raise HostRecoveryError("Encrypted host backup object size does not match evidence.")
    return object_path


def _allowed_destination(config: DaemonConfig, value: Path) -> Path:
    lexical = _absolute_unresolved(value, "destination_root")
    requested = _trusted_directory(lexical, "destination_root")
    for root in config.recovery_backup_roots:
        allowed = _trusted_directory(root, "recovery_backup_roots")
        try:
            requested.relative_to(allowed)
        except ValueError:
            continue
        return requested
    raise HostRecoveryError("Backup destination is outside configured recovery roots.")


def _host_state(journal: SQLiteOperationJournal, host_id: str) -> Dict[str, Any]:
    connection = journal._connect()
    try:
        row = connection.execute(
            "SELECT maintenance_mode, drained FROM host_state WHERE host_id = ?",
            (host_id,),
        ).fetchone()
    finally:
        connection.close()
        journal._repair_permissions()
    if row is None:
        raise KeyError(host_id)
    return {
        "maintenance_mode": bool(row["maintenance_mode"]),
        "drained": bool(row["drained"]),
    }


def _active_workload_runs(journal: SQLiteOperationJournal) -> int:
    connection = journal._connect()
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM workload_runs WHERE state IN ('accepted', 'running')"
            ).fetchone()[0]
        )
    finally:
        connection.close()
        journal._repair_permissions()


def _database_integrity(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + suffix, uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchone()
    finally:
        connection.close()
    if result != "ok" or foreign is not None:
        raise HostRecoveryError("Operation journal failed backup integrity checks.")
    return "ok"


def _validate_restored_database(path: Path, host_id: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise HostRecoveryError("Restored operation journal is missing.")
    _database_integrity(path, immutable=True)
    connection = sqlite3.connect(
        path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        mismatches = 0
        for table in (
            "host_state",
            "operations",
            "workload_runs",
            "agent_state",
            "host_observations",
        ):
            mismatches += int(
                connection.execute(
                    "SELECT COUNT(*) FROM %s WHERE host_id != ?" % table,
                    (host_id,),
                ).fetchone()[0]
            )
        expected_host = int(
            connection.execute(
                "SELECT COUNT(*) FROM host_state WHERE host_id = ?", (host_id,)
            ).fetchone()[0]
        )
    finally:
        connection.close()
    if mismatches or expected_host != 1:
        raise HostRecoveryError(
            "Restored operation journal does not match the backup host identity."
        )


def _copy_regular(source: Path, target: Path, *, mode: int) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_descriptor = _open_regular(source)
    before = os.fstat(source_descriptor)
    try:
        target_descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        try:
            with os.fdopen(source_descriptor, "rb", closefd=False) as value:
                while True:
                    chunk = value.read(1024 * 1024)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(target_descriptor, chunk[offset:])
            os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        after = os.fstat(source_descriptor)
    finally:
        os.close(source_descriptor)
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        target.unlink()
        raise HostRecoveryError("Recovery source changed during snapshot copy.")


def _read_json(path: Path, *, maximum_bytes: int = 1024 * 1024) -> Dict[str, Any]:
    descriptor = _open_regular(path)
    try:
        if os.fstat(descriptor).st_size > maximum_bytes:
            raise HostRecoveryError("Recovery JSON evidence is unavailable or unsafe.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            raw = source.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise HostRecoveryError("Recovery JSON evidence is unavailable or unsafe.")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostRecoveryError("Recovery JSON evidence is invalid.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise HostRecoveryError("Recovery JSON evidence must be an object.")
    return value


def _private_identity(path: Path) -> Path:
    value = _absolute_unresolved(path, "identity_file")
    if value.is_symlink() or not value.is_file():
        raise HostRecoveryError("Age identity must be a trusted regular file.")
    if stat.S_IMODE(value.stat().st_mode) & 0o077:
        raise HostRecoveryError("Age identity must use mode 0600 or stricter.")
    return value.resolve(strict=True)


def _real_directory(path: Path, field: str) -> Path:
    return _trusted_directory(path, field)


def _absolute_unresolved(path: Path, field: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() or ".." in value.parts:
        raise HostRecoveryError("%s must be a safe absolute path." % field)
    return value


def _restore_target_state(path: Path) -> str:
    if path.is_symlink():
        return "occupied"
    if not path.exists():
        return "absent"
    if not path.is_dir():
        return "occupied"
    try:
        next(path.iterdir())
    except StopIteration:
        return "empty"
    except OSError as exc:
        raise HostRecoveryError("Target runtime root could not be inspected safely.") from exc
    return "occupied"


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str):
        raise HostRecoveryError("Recovery manifest path is invalid.")
    path = Path(value)
    if path.is_absolute() or not path.parts or path == Path(".") or ".." in path.parts:
        raise HostRecoveryError("Recovery manifest path is unsafe.")
    return path


def _safe_mode(value: object, *, directory: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0o777:
        raise HostRecoveryError("Recovery manifest mode is invalid.")
    return value or (0o700 if directory else 0o600)


def _validate_entries(entries: List[Dict[str, Any]]) -> None:
    if len(entries) > 100000:
        raise HostRecoveryError("Recovery manifest contains too many entries.")
    paths = set()
    symlink_targets = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HostRecoveryError("Recovery manifest entry must be an object.")
        kind = entry.get("kind")
        expected = {
            "directory": {"path", "kind", "mode"},
            "file": {"path", "kind", "mode", "size", "sha256"},
            "symlink": {"path", "kind", "target"},
        }.get(kind)
        if expected is None or set(entry) != expected:
            raise HostRecoveryError("Recovery manifest entry fields are invalid.")
        path = _safe_relative(entry.get("path"))
        if len(path.parts) > 64 or len(path.as_posix()) > 4096 or path in paths:
            raise HostRecoveryError("Recovery manifest entry path is duplicated or too large.")
        paths.add(path)
        if kind in {"directory", "file"}:
            _safe_mode(entry.get("mode"), directory=kind == "directory")
        if kind == "file":
            size = entry.get("size")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or _DIGEST.fullmatch(str(entry.get("sha256"))) is None
            ):
                raise HostRecoveryError("Recovery manifest file evidence is invalid.")
        elif kind == "symlink":
            symlink_targets.append(_safe_relative(entry.get("target")))
    if any(target not in paths for target in symlink_targets):
        raise HostRecoveryError("Recovery symlink target is not present in the backup.")


def _entries_total_bytes(entries: List[Dict[str, Any]]) -> int:
    return sum(
        int(entry["size"]) for entry in entries if entry.get("kind") == "file"
    )


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    for path in [root] + sorted(root.rglob("*")):
        os.chown(path, uid, gid, follow_symlinks=False)


def _parent_paths(path: str) -> Iterable[str]:
    parts = path.split("/")
    for index in range(1, len(parts)):
        yield "/".join(parts[:index])


def _backup_id(value: object) -> str:
    if not isinstance(value, str) or _BACKUP_ID.fullmatch(value) is None:
        raise HostRecoveryError("Host backup id is invalid.")
    return value


def _text_digest(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = _open_regular(path)
    with os.fdopen(descriptor, "rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _open_regular(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise HostRecoveryError("Recovery source must be a trusted regular file.") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise HostRecoveryError("Recovery source must be a trusted regular file.")
    return descriptor


def _trusted_directory(path: Path, field: str) -> Path:
    absolute = _absolute_unresolved(path, field)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise HostRecoveryError("%s must be an existing trusted directory." % field) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HostRecoveryError("%s may not traverse symbolic links." % field)
    if not absolute.is_dir():
        raise HostRecoveryError("%s must be an existing trusted directory." % field)
    if stat.S_IMODE(absolute.stat().st_mode) & 0o022:
        raise HostRecoveryError("%s may not be group- or world-writable." % field)
    return absolute.resolve(strict=True)


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve(strict=False)
    second = second.resolve(strict=False)
    try:
        first.relative_to(second)
        return True
    except ValueError:
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False


def _valid_host_id(value: object) -> bool:
    return isinstance(value, str) and _HOST_ID.fullmatch(value) is not None


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and "\x00" not in value
        and not any(character.isspace() for character in value)
    )


def _backup_receipt(
    manifest: Dict[str, Any], target: Path, *, replayed: bool
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "ophelia.host-backup-receipt",
        "status": "succeeded",
        "host_id": manifest["host_id"],
        "backup_id": manifest["backup_id"],
        "created_at": manifest["created_at"],
        "backup_root": str(target),
        "object_digest": manifest["object_digest"],
        "object_bytes": manifest["object_bytes"],
        "entries_digest": manifest["entries_digest"],
        "encrypted": True,
        "replayed": replayed,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
