"""Idempotent, confirmation-bound Linux installer for ``opheliad``."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain import canonical_digest
from ..execution.subprocesses import SubprocessRunner
from ..version import package_version


class DaemonInstallError(RuntimeError):
    pass


def daemon_install_plan(
    *,
    source_root: Path,
    install_root: Path = Path("/opt/ophelia"),
    config_path: Path = Path("/etc/ophelia/agent.toml"),
    unit_path: Path = Path("/etc/systemd/system/opheliad.service"),
    launcher_path: Path = Path("/usr/local/libexec/opheliad-launcher"),
    activate: bool = True,
) -> Dict[str, Any]:
    if not isinstance(activate, bool):
        raise DaemonInstallError("Daemon activation choice must be boolean.")
    source_root = Path(source_root).expanduser().resolve(strict=True)
    if not (source_root / "pyproject.toml").is_file():
        raise DaemonInstallError("Ophelia source root does not contain pyproject.toml.")
    release_root = Path(install_root) / "releases" / package_version()
    source_digest = _source_digest(source_root)
    release_present = release_root.is_dir() and not release_root.is_symlink()
    observations = {
        "source_digest": source_digest,
        "unit_digest": _file_digest(unit_path),
        "config_digest": _file_digest(config_path),
        "launcher_digest": _file_digest(launcher_path),
        "release_present": release_present,
        "release_valid": release_present
        and _release_valid(release_root, source_digest=source_digest),
        "python3": shutil.which("python3"),
        "systemctl": shutil.which("systemctl"),
        "docker": shutil.which("docker"),
        "openssl": shutil.which("openssl"),
    }
    blockers = [
        name + "_missing"
        for name in ("python3", "systemctl", "docker", "openssl")
        if observations[name] is None
    ]
    exact = {
        "schema_version": 1,
        "kind": "ophelia.daemon-install-plan",
        "version": package_version(),
        "source_root": str(source_root),
        "install_root": str(Path(install_root)),
        "release_root": str(release_root),
        "config_path": str(Path(config_path)),
        "unit_path": str(Path(unit_path)),
        "launcher_path": str(Path(launcher_path)),
        "activate": activate,
        "observations": observations,
        "steps": [
            "create-system-identity",
            "install-versioned-venv",
            "write-strict-config",
            "write-hardened-systemd-unit",
            "write-stable-upgrade-launcher",
            "promote-current-release",
            (
                "enable-and-health-check-service"
                if activate
                else "enable-service-with-start-deferred-for-recovery"
            ),
        ],
        "blockers": blockers,
    }
    token = canonical_digest(exact)[7:31]
    return {
        **exact,
        "can_apply": not blockers,
        "confirmation_token": None if blockers else token,
    }


def apply_daemon_install(
    plan: Dict[str, Any],
    confirmation: str,
    *,
    runner: Optional[SubprocessRunner] = None,
) -> Dict[str, Any]:
    expected = dict(plan)
    expected.pop("can_apply", None)
    expected.pop("confirmation_token", None)
    token = canonical_digest(expected)[7:31]
    if not plan.get("can_apply") or confirmation != token:
        raise DaemonInstallError("Daemon install confirmation does not match the exact plan.")
    if os.geteuid() != 0:
        raise DaemonInstallError("Installing opheliad requires root authority.")
    refreshed = daemon_install_plan(
        source_root=Path(plan["source_root"]),
        install_root=Path(plan["install_root"]),
        config_path=Path(plan["config_path"]),
        unit_path=Path(plan["unit_path"]),
        launcher_path=Path(plan["launcher_path"]),
        activate=plan["activate"],
    )
    if refreshed["confirmation_token"] != confirmation:
        raise DaemonInstallError("Host state changed after the daemon install plan.")
    command = runner or SubprocessRunner()
    _ensure_identity(command)
    release_root = Path(plan["release_root"])
    source_digest = str(plan["observations"]["source_digest"])
    if not _release_valid(release_root, source_digest=source_digest):
        _install_release(
            command,
            release_root,
            Path(plan["source_root"]),
            source_digest=source_digest,
        )
    config_path = Path(plan["config_path"])
    if not config_path.exists():
        _secure_write(
            config_path,
            _default_config().encode("utf-8"),
            mode=0o640,
        )
    command.run(["chown", "root:ophelia", str(config_path)], timeout_seconds=30)
    unit_path = Path(plan["unit_path"])
    unit_text = resources.files("ophelia.resources").joinpath(
        "systemd/opheliad.service"
    ).read_text(encoding="utf-8")
    unit_text = unit_text.replace("/opt/ophelia", plan["install_root"]).replace(
        "/etc/ophelia/agent.toml", plan["config_path"]
    ).replace("/usr/local/libexec/opheliad-launcher", plan["launcher_path"])
    _secure_write(unit_path, unit_text.encode("utf-8"), mode=0o644)
    launcher_text = resources.files("ophelia.resources").joinpath(
        "systemd/opheliad-launcher.py"
    ).read_text(encoding="utf-8")
    _secure_write(
        Path(plan["launcher_path"]), launcher_text.encode("utf-8"), mode=0o755
    )
    _promote_release(Path(plan["install_root"]), release_root)
    command.run(
        ["chown", "-R", "ophelia:ophelia", plan["install_root"]],
        timeout_seconds=120,
    )
    command.run(["systemctl", "daemon-reload"], timeout_seconds=60)
    if plan["activate"]:
        command.run(
            ["systemctl", "enable", "--now", "opheliad.service"],
            timeout_seconds=120,
        )
        command.run(
            ["systemctl", "is-active", "--quiet", "opheliad.service"],
            timeout_seconds=60,
        )
    else:
        command.run(["systemctl", "enable", "opheliad.service"], timeout_seconds=60)
    return {
        "schema_version": 1,
        "kind": "ophelia.daemon-install-receipt",
        "status": "succeeded",
        "version": plan["version"],
        "release_root": plan["release_root"],
        "unit_digest": _file_digest(unit_path),
        "launcher_digest": _file_digest(Path(plan["launcher_path"])),
        "config_digest": _file_digest(config_path),
        "service_state": "active" if plan["activate"] else "start_deferred",
    }


def _install_release(
    runner: SubprocessRunner,
    release_root: Path,
    source_root: Path,
    *,
    source_digest: str,
) -> None:
    release_root.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if release_root.exists() or release_root.is_symlink():
        if release_root.is_symlink() or not release_root.is_dir():
            raise DaemonInstallError("Versioned daemon release path is unsafe.")
        quarantine = release_root.parent / (
            ".invalid-%s-%s" % (release_root.name, os.urandom(6).hex())
        )
        os.replace(release_root, quarantine)
        _sync_directory(release_root.parent)
    staging = release_root.parent / (
        ".install-%s-%s" % (release_root.name, os.urandom(6).hex())
    )
    runner.run(["python3", "-m", "venv", str(staging)], timeout_seconds=120)
    runner.run(
        [
            str(staging / "bin" / "python"),
            "-m",
            "pip",
            "install",
            str(source_root),
        ],
        timeout_seconds=600,
    )
    _secure_write(
        staging / "ophelia-install.json",
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "ophelia.daemon-install",
                    "version": package_version(),
                    "source_digest": source_digest,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        mode=0o644,
    )
    if not _release_valid(staging, source_digest=source_digest):
        raise DaemonInstallError("Installed daemon release failed executable validation.")
    os.replace(staging, release_root)
    _sync_directory(release_root.parent)


def _release_valid(root: Path, *, source_digest: str) -> bool:
    executable = root / "bin" / "opheliad"
    metadata_path = root / "ophelia-install.json"
    if not (
        root.is_dir()
        and not root.is_symlink()
        and executable.is_file()
        and not executable.is_symlink()
        and os.access(executable, os.X_OK)
        and metadata_path.is_file()
        and not metadata_path.is_symlink()
    ):
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return metadata == {
        "schema_version": 1,
        "kind": "ophelia.daemon-install",
        "version": package_version(),
        "source_digest": source_digest,
    }


def _ensure_identity(runner: SubprocessRunner) -> None:
    user = runner.run(["id", "-u", "ophelia"], timeout_seconds=30, check=False)
    if user.exit_reason != "success":
        runner.run(
            [
                "useradd",
                "--system",
                "--home-dir",
                "/var/lib/ophelia",
                "--shell",
                "/usr/sbin/nologin",
                "ophelia",
            ],
            timeout_seconds=30,
        )
    runner.run(["usermod", "-aG", "docker", "ophelia"], timeout_seconds=30)
    for path, mode in (
        (Path("/var/lib/ophelia"), 0o700),
        (Path("/var/lib/ophelia-identity"), 0o700),
        (Path("/etc/ophelia"), 0o750),
    ):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        os.chmod(path, mode)
    runner.run(["chown", "-R", "ophelia:ophelia", "/var/lib/ophelia"], timeout_seconds=60)
    runner.run(
        ["chown", "-R", "ophelia:ophelia", "/var/lib/ophelia-identity"],
        timeout_seconds=60,
    )
    runner.run(["chown", "root:ophelia", "/etc/ophelia"], timeout_seconds=30)


def _default_config() -> str:
    return """runtime_root = "/var/lib/ophelia"
socket_path = "/run/ophelia/opheliad.sock"
install_root = "/opt/ophelia"
allowed_uids = [0]
allowed_manifest_roots = ["/srv"]
max_request_bytes = 1048576
max_workers = 4
operation_poll_seconds = 0.5
reconciliation_seconds = 15
integrity_check_seconds = 300
scheduler_seconds = 15
require_edge_runtime = true
local_planning_enabled = true
local_apply_enabled = true
agent_enabled = false
agent_poll_seconds = 5
agent_exchange_bytes = 8388608
agent_event_batch = 250
agent_command_batch = 100
recovery_backup_roots = []
recovery_max_bytes = 68719476736
recovery_freshness_seconds = 86400
observation_seconds = 15
observation_retention = 1000
disk_warning_percent = 85
disk_critical_percent = 95
"""


def _promote_release(install_root: Path, release_root: Path) -> None:
    current = install_root / "current"
    temporary = install_root / (".current.tmp-" + os.urandom(6).hex())
    temporary.symlink_to(release_root)
    os.replace(temporary, current)
    _sync_directory(install_root)


def _secure_write(path: Path, value: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise DaemonInstallError("Installer target may not be a symbolic link.")
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, mode)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> Optional[str]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    excluded = {".git", ".venv", "build", "dist", "__pycache__"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts):
            continue
        if path.is_symlink():
            raise DaemonInstallError("Ophelia install source may not contain symbolic links.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DaemonInstallError("Ophelia install source contains a special file.")
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()
