"""Host certificate status and authenticated in-place certificate rotation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain._contracts import parse_utc
from ..execution.subprocesses import SubprocessRunner
from .agent_transport import AgentTransport
from .config import DaemonConfig, PROTOCOL_VERSION
from .install import _secure_write


class HostIdentityError(RuntimeError):
    pass


def identity_status(config: DaemonConfig) -> Dict[str, Any]:
    """Return bounded non-secret certificate lifecycle evidence."""

    if config.host_certificate_path is None:
        return {"state": "unenrolled"}
    certificate = config.host_certificate_path
    enrollment_path = certificate.parent / "enrollment.json"
    enrollment: Dict[str, Any] = {}
    if (
        enrollment_path.is_file()
        and not enrollment_path.is_symlink()
        and enrollment_path.stat().st_size <= 65536
    ):
        try:
            loaded = json.loads(enrollment_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                enrollment = loaded
        except (OSError, UnicodeError, ValueError):
            enrollment = {}
    not_after = enrollment.get("certificate_not_after")
    state = "enrolled"
    if isinstance(not_after, str):
        try:
            expiry = parse_utc(not_after)
        except ValueError:
            state = "invalid_metadata"
        else:
            now = datetime.now(timezone.utc)
            if expiry <= now:
                state = "expired"
            elif expiry <= now + timedelta(days=30):
                state = "expiring"
    return {
        "state": state,
        "certificate_digest": _digest_file(certificate),
        "certificate_not_after": not_after,
        "rotated_at": enrollment.get("rotated_at"),
    }


def rotate_host_certificate(
    config: DaemonConfig,
    transport: AgentTransport,
    *,
    command_id: str,
    runner: Optional[SubprocessRunner] = None,
) -> Dict[str, Any]:
    """Renew the certificate over the authenticated channel using the host key."""

    key = config.host_private_key_path
    certificate = config.host_certificate_path
    ca = config.control_plane_ca_path
    if key is None or certificate is None or ca is None:
        raise HostIdentityError("Host identity is incomplete and cannot rotate.")
    if key.parent != certificate.parent:
        raise HostIdentityError("Host key and certificate must share the identity root.")
    identity_root = key.parent
    runtime_identity = config.runtime_root / "run" / "identity"
    runtime_identity.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime_identity, 0o700)
    command = runner or SubprocessRunner()
    with tempfile.TemporaryDirectory(prefix="rotate-", dir=runtime_identity) as directory:
        staging = Path(directory)
        csr = staging / "host.csr"
        command.run(
            [
                "openssl",
                "req",
                "-new",
                "-key",
                str(key),
                "-subj",
                "/CN=" + config.host_id,
                "-out",
                str(csr),
            ],
            timeout_seconds=60,
        )
        response = transport.rotate_identity(
            {
                "schema_version": 1,
                "kind": "ophelia.certificate-rotation-request",
                "protocol_version": PROTOCOL_VERSION,
                "host_id": config.host_id,
                "command_id": command_id,
                "csr_pem": csr.read_text(encoding="utf-8"),
            }
        )
        material = _validate_rotation_response(response, config.host_id, command_id)
        candidate = staging / "host.crt"
        _secure_write(
            candidate,
            material["host_certificate_pem"].encode("utf-8"),
            mode=0o600,
        )
        command.run(
            ["openssl", "x509", "-in", str(candidate), "-noout", "-checkend", "3600"],
            timeout_seconds=30,
        )
        command.run(
            ["openssl", "verify", "-CAfile", str(ca), str(candidate)],
            timeout_seconds=30,
        )
        private_public = command.run(
            ["openssl", "pkey", "-in", str(key), "-pubout"], timeout_seconds=30
        )
        certificate_public = command.run(
            ["openssl", "x509", "-in", str(candidate), "-pubkey", "-noout"],
            timeout_seconds=30,
        )
        if private_public.stdout.strip() != certificate_public.stdout.strip():
            raise HostIdentityError("Rotated certificate does not match the host key.")
        _secure_write(certificate, candidate.read_bytes(), mode=0o600)
    enrollment_path = identity_root / "enrollment.json"
    enrollment = _load_enrollment(enrollment_path)
    enrollment.update(
        {
            "schema_version": 1,
            "kind": "ophelia.host-enrollment",
            "host_id": config.host_id,
            "control_plane_url": config.control_plane_url,
            "protocol_version": PROTOCOL_VERSION,
            "certificate_not_after": material["certificate_not_after"],
            "certificate_digest": _digest_file(certificate),
            "rotated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    _secure_write(
        enrollment_path,
        (json.dumps(enrollment, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )
    return {
        "host_id": config.host_id,
        "certificate_digest": enrollment["certificate_digest"],
        "certificate_not_after": material["certificate_not_after"],
        "restart_required": True,
    }


def _validate_rotation_response(
    value: object, host_id: str, command_id: str
) -> Dict[str, str]:
    fields = {
        "schema_version",
        "kind",
        "protocol_version",
        "host_id",
        "command_id",
        "host_certificate_pem",
        "certificate_not_after",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise HostIdentityError("Certificate rotation response fields are invalid.")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "ophelia.certificate-rotation-response"
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("host_id") != host_id
        or value.get("command_id") != command_id
    ):
        raise HostIdentityError("Certificate rotation response identity is invalid.")
    for field in ("host_certificate_pem", "certificate_not_after"):
        item = value.get(field)
        if not isinstance(item, str) or not item or len(item) > 262144:
            raise HostIdentityError("Certificate rotation response is invalid.")
    if parse_utc(value["certificate_not_after"]) <= datetime.now(timezone.utc):
        raise HostIdentityError("Rotated host certificate is already expired.")
    return value


def _load_enrollment(path: Path) -> Dict[str, Any]:
    if path.is_symlink():
        raise HostIdentityError("Host enrollment metadata path is unsafe.")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HostIdentityError("Host enrollment metadata is invalid.") from exc
    if not isinstance(value, dict) or len(json.dumps(value)) > 65536:
        raise HostIdentityError("Host enrollment metadata is invalid.")
    return value


def _digest_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise HostIdentityError("Host certificate is unavailable.")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
