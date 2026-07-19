"""Strict typed configuration for ``opheliad``."""

from __future__ import annotations

import ast
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ..execution.legacy_adapter import local_host_id


PROTOCOL_VERSION = 1
DEFAULT_CONFIG_PATH = Path("/etc/ophelia/agent.toml")
DEFAULT_RUNTIME_ROOT = Path("/var/lib/ophelia")
DEFAULT_SOCKET_PATH = Path("/run/ophelia/opheliad.sock")


class DaemonConfigError(ValueError):
    """Daemon configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class DaemonConfig:
    host_id: str
    runtime_root: Path
    socket_path: Path
    allowed_uids: Tuple[int, ...]
    allowed_manifest_roots: Tuple[Path, ...]
    max_request_bytes: int = 1024 * 1024
    max_workers: int = 4
    operation_poll_seconds: float = 0.5
    reconciliation_seconds: float = 15.0
    integrity_check_seconds: float = 300.0
    scheduler_seconds: float = 15.0
    require_edge_runtime: bool = True
    local_planning_enabled: bool = True
    local_apply_enabled: bool = True

    def redacted_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "runtime_root": str(self.runtime_root),
            "socket_path": str(self.socket_path),
            "allowed_uids": list(self.allowed_uids),
            "allowed_manifest_roots": [str(path) for path in self.allowed_manifest_roots],
            "max_request_bytes": self.max_request_bytes,
            "max_workers": self.max_workers,
            "operation_poll_seconds": self.operation_poll_seconds,
            "reconciliation_seconds": self.reconciliation_seconds,
            "integrity_check_seconds": self.integrity_check_seconds,
            "scheduler_seconds": self.scheduler_seconds,
            "require_edge_runtime": self.require_edge_runtime,
            "local_planning_enabled": self.local_planning_enabled,
            "local_apply_enabled": self.local_apply_enabled,
        }


_KEYS = {
    "host_id",
    "runtime_root",
    "socket_path",
    "allowed_uids",
    "allowed_manifest_roots",
    "max_request_bytes",
    "max_workers",
    "operation_poll_seconds",
    "reconciliation_seconds",
    "integrity_check_seconds",
    "scheduler_seconds",
    "require_edge_runtime",
    "local_planning_enabled",
    "local_apply_enabled",
}


def load_daemon_config(
    path: Optional[Path] = None,
    *,
    runtime_root: Optional[Path] = None,
    socket_path: Optional[Path] = None,
) -> DaemonConfig:
    """Load strict TOML, or construct secure defaults when no file is present."""

    config_path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    raw: Mapping[str, Any] = {}
    if config_path.exists() or config_path.is_symlink():
        _validate_config_file(config_path)
        try:
            text = config_path.read_text(encoding="utf-8")
            try:
                import tomllib
            except ImportError:  # Python 3.9/3.10 compatibility
                document = _parse_flat_toml(text)
            else:
                document = tomllib.loads(text)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DaemonConfigError("Daemon configuration is not valid TOML.") from exc
        if not isinstance(document, dict):
            raise DaemonConfigError("Daemon configuration root must be a table.")
        raw = document
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise DaemonConfigError("Unknown daemon configuration keys: %s" % ", ".join(unknown))

    effective_runtime = _directory_path(
        runtime_root or raw.get("runtime_root", DEFAULT_RUNTIME_ROOT),
        "runtime_root",
    )
    effective_socket = _absolute_path(
        socket_path or raw.get("socket_path", DEFAULT_SOCKET_PATH),
        "socket_path",
    )
    manifest_roots = tuple(
        _directory_path(value, "allowed_manifest_roots")
        for value in _list(raw.get("allowed_manifest_roots", []), "allowed_manifest_roots")
    )
    allowed_uids = tuple(
        sorted(
            set(
                _bounded_int(value, "allowed_uids", minimum=0, maximum=2**31 - 1)
                for value in _list(raw.get("allowed_uids", [os.geteuid()]), "allowed_uids")
            )
        )
    )
    if not allowed_uids:
        raise DaemonConfigError("allowed_uids must contain at least one UID.")
    host_id = raw.get("host_id", local_host_id())
    if not isinstance(host_id, str) or not host_id.startswith("host_") or len(host_id) > 255:
        raise DaemonConfigError("host_id must be a bounded Ophelia host identifier.")
    return DaemonConfig(
        host_id=host_id,
        runtime_root=effective_runtime,
        socket_path=effective_socket,
        allowed_uids=allowed_uids,
        allowed_manifest_roots=manifest_roots,
        max_request_bytes=_bounded_int(
            raw.get("max_request_bytes", 1024 * 1024),
            "max_request_bytes",
            minimum=1024,
            maximum=16 * 1024 * 1024,
        ),
        max_workers=_bounded_int(
            raw.get("max_workers", 4), "max_workers", minimum=1, maximum=64
        ),
        operation_poll_seconds=_bounded_number(
            raw.get("operation_poll_seconds", 0.5), "operation_poll_seconds", 0.05, 60
        ),
        reconciliation_seconds=_bounded_number(
            raw.get("reconciliation_seconds", 15), "reconciliation_seconds", 1, 3600
        ),
        integrity_check_seconds=_bounded_number(
            raw.get("integrity_check_seconds", 300),
            "integrity_check_seconds",
            10,
            86400,
        ),
        scheduler_seconds=_bounded_number(
            raw.get("scheduler_seconds", 15), "scheduler_seconds", 1, 60
        ),
        require_edge_runtime=_boolean(
            raw.get("require_edge_runtime", True), "require_edge_runtime"
        ),
        local_planning_enabled=_boolean(
            raw.get("local_planning_enabled", True), "local_planning_enabled"
        ),
        local_apply_enabled=_boolean(
            raw.get("local_apply_enabled", True), "local_apply_enabled"
        ),
    )


def _validate_config_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise DaemonConfigError("Daemon configuration must be a real regular file.")
    metadata = path.stat()
    if metadata.st_uid not in {0, os.geteuid()}:
        raise DaemonConfigError("Daemon configuration must be owned by root or the Ophelia UID.")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise DaemonConfigError("Daemon configuration may not be group- or world-writable.")


def _absolute_path(value: object, field: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise DaemonConfigError("%s must be a path." % field)
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise DaemonConfigError("%s must be a safe absolute path." % field)
    return path


def _directory_path(value: object, field: str) -> Path:
    return _absolute_path(value, field).resolve(strict=False)


def _list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise DaemonConfigError("%s must be a list." % field)
    return value


def _bounded_int(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DaemonConfigError("%s must be an integer from %d through %d." % (field, minimum, maximum))
    return value


def _bounded_number(value: object, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DaemonConfigError("%s must be numeric." % field)
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise DaemonConfigError("%s must be from %s through %s." % (field, minimum, maximum))
    return parsed


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise DaemonConfigError("%s must be boolean." % field)
    return value


def _parse_flat_toml(text: str) -> dict[str, Any]:
    """Parse Ophelia's deliberately flat Python-3.9 configuration subset."""

    result: dict[str, Any] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("["):
            raise ValueError("TOML tables are not supported in agent.toml.")
        if "=" not in line:
            raise ValueError("TOML assignment is missing on line %d." % line_number)
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) is None or key in result:
            raise ValueError("TOML key is invalid or duplicated on line %d." % line_number)
        value = raw_value.strip()
        if value == "true":
            parsed: Any = True
        elif value == "false":
            parsed = False
        else:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                try:
                    parsed = float(value) if "." in value else int(value)
                except ValueError:
                    raise ValueError("TOML value is unsupported on line %d." % line_number) from exc
        if not isinstance(parsed, (str, int, float, bool, list)):
            raise ValueError("TOML value type is unsupported on line %d." % line_number)
        result[key] = parsed
    return result


def _strip_comment(line: str) -> str:
    quote: Optional[str] = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else (character if quote is None else quote)
            continue
        if character == "#" and quote is None:
            return line[:index]
    if quote is not None:
        raise ValueError("Unterminated TOML string.")
    return line
