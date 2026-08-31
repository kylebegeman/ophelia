"""Signed, staged daemon self-upgrades with startup confirmation and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

from ..execution.subprocesses import SubprocessRunner
from ..version import package_version
from .config import DaemonConfig
from .install import _secure_write, _sync_directory


_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]{0,64})?$")


class AgentUpgradeError(RuntimeError):
    pass


def stage_agent_upgrade(
    config: DaemonConfig,
    *,
    command_id: str,
    envelope_digest: str,
    payload: Dict[str, Any],
    runner: Optional[SubprocessRunner] = None,
) -> Dict[str, Any]:
    """Install a verified source archive and atomically stage it as current."""

    if set(payload) != {"version", "archive_path", "source_digest"}:
        raise AgentUpgradeError("Prepared agent-upgrade payload is invalid.")
    version = payload.get("version")
    source_digest = payload.get("source_digest")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise AgentUpgradeError("Agent-upgrade version is invalid.")
    if not isinstance(source_digest, str) or re.fullmatch(
        r"sha256:[0-9a-f]{64}", source_digest
    ) is None:
        raise AgentUpgradeError("Agent-upgrade source digest is invalid.")
    archive = Path(payload.get("archive_path", "")).resolve(strict=True)
    inbox = (config.runtime_root / "inbox" / command_id).resolve(strict=True)
    try:
        archive.relative_to(inbox)
    except ValueError as exc:
        raise AgentUpgradeError("Agent-upgrade archive is outside its trusted inbox.") from exc
    if archive.is_symlink() or not archive.is_file():
        raise AgentUpgradeError("Agent-upgrade archive is unsafe.")
    if _digest_file(archive) != source_digest:
        raise AgentUpgradeError("Agent-upgrade archive digest changed after acceptance.")

    install_root = config.install_root
    releases = install_root / "releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    if install_root.is_symlink() or releases.is_symlink():
        raise AgentUpgradeError("Agent install root is unsafe.")
    suffix = source_digest[7:19]
    target = releases / (version + "-" + suffix)
    metadata = {
        "schema_version": 1,
        "kind": "ophelia.agent-release",
        "version": version,
        "source_digest": source_digest,
    }
    command = runner or SubprocessRunner()
    if not _release_valid(target, metadata):
        if target.exists() or target.is_symlink():
            raise AgentUpgradeError("Existing agent release is incomplete or does not match.")
        staging = releases / (".upgrade-" + version + "-" + os.urandom(6).hex())
        source_root = staging / "source"
        source_root.mkdir(mode=0o700, parents=True)
        try:
            _extract_source_archive(
                archive,
                source_root,
                maximum_bytes=max(config.max_request_bytes * 32, 64 * 1024 * 1024),
            )
            project_root = _project_root(source_root)
            if _project_version(project_root / "pyproject.toml") != version:
                raise AgentUpgradeError(
                    "Agent-upgrade archive version does not match its signed request."
                )
            release_staging = staging / "release"
            command.run(
                ["python3", "-m", "venv", str(release_staging)],
                timeout_seconds=120,
            )
            command.run(
                [
                    str(release_staging / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    str(project_root),
                ],
                timeout_seconds=600,
            )
            _secure_write(
                release_staging / "ophelia-install.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                mode=0o600,
            )
            if not _release_valid(release_staging, metadata):
                raise AgentUpgradeError("Staged agent release failed executable validation.")
            os.replace(release_staging, target)
            _sync_directory(releases)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    current = install_root / "current"
    if not current.is_symlink():
        raise AgentUpgradeError("Current agent release pointer is unavailable.")
    existing_receipt = _upgrade_receipt(install_root, command_id)
    if existing_receipt is not None:
        if (
            existing_receipt.get("status") != "succeeded"
            or existing_receipt.get("version") != version
            or existing_receipt.get("source_digest") != source_digest
            or existing_receipt.get("envelope_digest") != envelope_digest
            or current.resolve(strict=True) != target.resolve(strict=True)
        ):
            raise AgentUpgradeError("Existing agent-upgrade receipt conflicts with the command.")
        return _staged_result(existing_receipt)
    marker_path = install_root / "upgrade-pending.json"
    marker = None
    if marker_path.exists() or marker_path.is_symlink():
        if marker_path.is_symlink() or not marker_path.is_file():
            raise AgentUpgradeError("Pending agent-upgrade marker is unsafe.")
        marker = _load_marker(marker_path)
        if (
            marker["command_id"] != command_id
            or marker["version"] != version
            or marker["source_digest"] != source_digest
            or marker["envelope_digest"] != envelope_digest
            or Path(marker["target_release"]).resolve(strict=True)
            != target.resolve(strict=True)
        ):
            raise AgentUpgradeError("Another agent upgrade is already pending.")
        current_release = current.resolve(strict=True)
        previous = Path(marker["previous_release"]).resolve(strict=True)
        if current_release == target.resolve(strict=True):
            return _staged_result(marker)
        if current_release != previous:
            raise AgentUpgradeError("Pending agent-upgrade release pointers disagree.")
    else:
        previous = current.resolve(strict=True)
    try:
        previous.relative_to(releases.resolve(strict=True))
    except ValueError as exc:
        raise AgentUpgradeError("Current agent release is outside the install root.") from exc
    if marker is None:
        marker = {
            "schema_version": 1,
            "kind": "ophelia.agent-upgrade-pending",
            "command_id": command_id,
            "version": version,
            "source_digest": source_digest,
            "envelope_digest": envelope_digest,
            "previous_release": str(previous),
            "target_release": str(target),
        }
        _secure_write(
            marker_path,
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )
    temporary = install_root / (".current.upgrade-" + os.urandom(6).hex())
    temporary.symlink_to(target)
    os.replace(temporary, current)
    _sync_directory(install_root)
    return _staged_result(marker)


def confirm_running_upgrade(config: DaemonConfig) -> Optional[Dict[str, Any]]:
    """Confirm the new current release after its daemon has remained healthy."""

    marker_path = config.install_root / "upgrade-pending.json"
    if marker_path.is_symlink() or not marker_path.is_file():
        return None
    marker = _load_marker(marker_path)
    current = (config.install_root / "current").resolve(strict=True)
    releases = (config.install_root / "releases").resolve(strict=True)
    target = Path(marker["target_release"]).resolve(strict=True)
    previous = Path(marker["previous_release"]).resolve(strict=True)
    try:
        target.relative_to(releases)
        previous.relative_to(releases)
    except ValueError as exc:
        raise AgentUpgradeError(
            "Pending agent-upgrade releases are outside the install root."
        ) from exc
    if current == target and package_version() == marker["version"]:
        status = "succeeded"
        kind = "ophelia.agent-upgrade-receipt"
    elif current == previous:
        status = "aborted_before_activation"
        kind = "ophelia.agent-upgrade-abort"
    else:
        raise AgentUpgradeError("Running release does not match pending upgrade evidence.")
    receipt = {
        **marker,
        "kind": kind,
        "status": status,
        "observed_version": package_version(),
        "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    receipts = config.install_root / "upgrade-receipts"
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_write(
        receipts / (marker["command_id"] + ".json"),
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )
    marker_path.unlink()
    _sync_directory(config.install_root)
    return receipt


def _load_marker(path: Path) -> Dict[str, Any]:
    expected = {
        "schema_version",
        "kind",
        "command_id",
        "version",
        "source_digest",
        "envelope_digest",
        "previous_release",
        "target_release",
    }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpgradeError("Pending agent upgrade marker is invalid.") from exc
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != "ophelia.agent-upgrade-pending"
    ):
        raise AgentUpgradeError("Pending agent upgrade marker is invalid.")
    return value


def _upgrade_receipt(install_root: Path, command_id: str) -> Optional[Dict[str, Any]]:
    path = install_root / "upgrade-receipts" / (command_id + ".json")
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpgradeError("Existing agent-upgrade receipt is invalid.") from exc
    if not isinstance(value, dict):
        raise AgentUpgradeError("Existing agent-upgrade receipt is invalid.")
    return value


def _staged_result(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": value["version"],
        "source_digest": value["source_digest"],
        "previous_release": value["previous_release"],
        "target_release": value["target_release"],
        "restart_required": True,
    }


def _extract_source_archive(
    archive: Path, target: Path, *, maximum_bytes: int
) -> None:
    total = 0
    seen: Set[Path] = set()
    try:
        source = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise AgentUpgradeError("Agent-upgrade source archive is invalid.") from exc
    with source:
        member_count = 0
        for member in source:
            member_count += 1
            if member_count > 10000:
                raise AgentUpgradeError("Agent-upgrade archive member count is invalid.")
            if "\\" in member.name or member.name.startswith("/"):
                raise AgentUpgradeError("Agent-upgrade archive path is unsafe.")
            relative = Path(member.name)
            if not relative.parts or relative == Path(".") or ".." in relative.parts:
                raise AgentUpgradeError("Agent-upgrade archive path is unsafe.")
            if relative in seen or len(relative.parts) > 32:
                raise AgentUpgradeError("Agent-upgrade archive contains an invalid path.")
            seen.add(relative)
            destination = target / relative
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size < 0:
                raise AgentUpgradeError("Agent-upgrade archive contains a special file.")
            total += member.size
            if total > maximum_bytes:
                raise AgentUpgradeError("Agent-upgrade archive exceeds its extraction quota.")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise AgentUpgradeError("Agent-upgrade archive member is unreadable.")
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700 if member.mode & stat.S_IXUSR else 0o600,
            )
            try:
                remaining = member.size
                while remaining:
                    chunk = extracted.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise AgentUpgradeError("Agent-upgrade archive member is truncated.")
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
                    remaining -= len(chunk)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if member_count == 0:
            raise AgentUpgradeError("Agent-upgrade archive member count is invalid.")


def _project_root(root: Path) -> Path:
    candidates = [path.parent for path in root.glob("pyproject.toml")]
    candidates.extend(path.parent for path in root.glob("*/pyproject.toml"))
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise AgentUpgradeError("Agent-upgrade archive must contain one project root.")
    return unique[0]


def _project_version(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise AgentUpgradeError("Agent-upgrade pyproject.toml is unavailable or unsafe.")
    text = path.read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    match = None if project is None else re.search(
        r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']\s*$', project.group(1)
    )
    if match is None or _VERSION.fullmatch(match.group(1)) is None:
        raise AgentUpgradeError("Agent-upgrade project version is invalid.")
    return match.group(1)


def _release_valid(root: Path, expected: Dict[str, Any]) -> bool:
    executable = root / "bin" / "opheliad"
    metadata = root / "ophelia-install.json"
    if (
        root.is_symlink()
        or not root.is_dir()
        or executable.is_symlink()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
        or metadata.is_symlink()
        or not metadata.is_file()
    ):
        return False
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return value == expected


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
