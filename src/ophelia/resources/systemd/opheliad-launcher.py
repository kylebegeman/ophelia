#!/usr/bin/env python3
"""Stable systemd launcher with failed-start rollback for staged upgrades."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
from pathlib import Path


RESTART_FOR_STAGED_STATE = 75


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    install_root = arguments.install_root.resolve(strict=True)
    executable = _current_executable(install_root)
    process = subprocess.Popen([str(executable), "--config", str(arguments.config)])

    def forward(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return_code = process.wait()
    if return_code == RESTART_FOR_STAGED_STATE:
        return return_code
    _rollback_unconfirmed_upgrade(install_root)
    return return_code


def _current_executable(install_root: Path) -> Path:
    current = install_root / "current"
    if not current.is_symlink():
        raise RuntimeError("Ophelia current release pointer is unavailable.")
    release = current.resolve(strict=True)
    release.relative_to((install_root / "releases").resolve(strict=True))
    executable = release / "bin" / "opheliad"
    if executable.is_symlink() or not executable.is_file():
        raise RuntimeError("Current Ophelia daemon executable is unsafe.")
    return executable


def _rollback_unconfirmed_upgrade(install_root: Path) -> None:
    marker = install_root / "upgrade-pending.json"
    if marker.is_symlink() or not marker.is_file():
        return
    metadata = marker.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
        raise RuntimeError("Pending Ophelia upgrade marker is unsafe.")
    value = json.loads(marker.read_text(encoding="utf-8"))
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
    if not isinstance(value, dict) or set(value) != expected:
        raise RuntimeError("Pending Ophelia upgrade marker is invalid.")
    releases = (install_root / "releases").resolve(strict=True)
    previous = Path(value["previous_release"]).resolve(strict=True)
    target = Path(value["target_release"]).resolve(strict=True)
    previous.relative_to(releases)
    target.relative_to(releases)
    current = (install_root / "current").resolve(strict=True)
    if current == previous:
        marker.unlink()
        _sync(install_root)
        return
    if current != target:
        raise RuntimeError("Pending Ophelia upgrade target is not current.")
    temporary = install_root / (".current.rollback-" + os.urandom(6).hex())
    temporary.symlink_to(previous)
    os.replace(temporary, install_root / "current")
    receipt = {
        **value,
        "kind": "ophelia.agent-upgrade-rollback",
        "status": "rolled_back",
    }
    _write_atomic(
        install_root / "upgrade-rollback.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    marker.unlink()
    _sync(install_root)


def _write_atomic(path: Path, value: bytes) -> None:
    temporary = path.parent / ("." + path.name + ".tmp-" + os.urandom(6).hex())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
