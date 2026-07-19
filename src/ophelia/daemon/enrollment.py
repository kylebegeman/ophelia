"""Confirmation-bound one-time host enrollment and trust installation."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from ..domain import canonical_digest
from ..domain._contracts import parse_utc
from ..execution.subprocesses import SubprocessRunner
from ..version import package_version
from .config import DEFAULT_IDENTITY_ROOT, PROTOCOL_VERSION
from .install import _secure_write


class EnrollmentError(RuntimeError):
    pass


_HOST_ID = re.compile(r"^host_[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")


EnrollmentExchange = Callable[[str, str, str, str, Optional[Path]], Dict[str, Any]]


def enrollment_plan(
    *,
    host_id: str,
    control_plane_url: str,
    token_file: Path,
    trust_root: Path = Path("/etc/ophelia/trust"),
    identity_root: Path = DEFAULT_IDENTITY_ROOT,
    config_path: Path = Path("/etc/ophelia/agent.toml"),
    bootstrap_ca_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if not isinstance(host_id, str) or _HOST_ID.fullmatch(host_id) is None:
        raise EnrollmentError("Enrollment host id is invalid.")
    endpoint = _https_url(control_plane_url)
    token = _private_token_file(token_file)
    trust = _absolute_path(trust_root, "trust_root")
    identity = _absolute_path(identity_root, "identity_root")
    if (
        trust == identity
        or trust in identity.parents
        or identity in trust.parents
    ):
        raise EnrollmentError(
            "Trust and host-identity roots must be separate non-overlapping directories."
        )
    config = _absolute_path(config_path, "config_path")
    ca = None
    if bootstrap_ca_path is not None:
        ca = _trusted_file(bootstrap_ca_path, "bootstrap_ca_path")
    targets = {
        "host_key": identity / "host.key",
        "host_certificate": identity / "host.crt",
        "control_plane_ca": trust / "control-plane-ca.pem",
        "decision_public_key": trust / "decision-public.pem",
        "enrollment": identity / "enrollment.json",
    }
    observations = {
        "token_digest": _digest_file(token),
        "token_mode": oct(stat.S_IMODE(token.stat().st_mode)),
        "bootstrap_ca_digest": None if ca is None else _digest_file(ca),
        "config_digest": _digest_file(config),
        "existing_targets": {
            name: _digest_file(path) for name, path in targets.items()
        },
        "openssl": shutil.which("openssl"),
        "systemctl": shutil.which("systemctl"),
    }
    blockers = []
    if observations["openssl"] is None:
        blockers.append("openssl_missing")
    if observations["systemctl"] is None:
        blockers.append("systemctl_missing")
    if observations["config_digest"] is None:
        blockers.append("daemon_config_missing")
    if any(value is not None for value in observations["existing_targets"].values()):
        blockers.append("host_identity_already_present")
    exact = {
        "schema_version": 1,
        "kind": "ophelia.enrollment-plan",
        "host_id": host_id,
        "control_plane_url": endpoint,
        "token_file": str(token),
        "trust_root": str(trust),
        "identity_root": str(identity),
        "config_path": str(config),
        "bootstrap_ca_path": None if ca is None else str(ca),
        "targets": {name: str(path) for name, path in targets.items()},
        "observations": observations,
        "steps": [
            "generate-host-key-and-csr",
            "exchange-one-time-token",
            "verify-host-certificate-and-decision-key",
            "publish-trust-material",
            "enable-outbound-agent-config",
            "delete-enrollment-token",
            "restart-and-health-check-daemon",
        ],
        "blockers": blockers,
    }
    return {
        **exact,
        "can_apply": not blockers,
        "confirmation_token": None
        if blockers
        else canonical_digest(exact)[7:31],
    }


def apply_enrollment(
    plan: Dict[str, Any],
    confirmation: str,
    *,
    runner: Optional[SubprocessRunner] = None,
    exchange: Optional[EnrollmentExchange] = None,
) -> Dict[str, Any]:
    expected = dict(plan)
    expected.pop("can_apply", None)
    expected.pop("confirmation_token", None)
    if (
        not plan.get("can_apply")
        or confirmation != canonical_digest(expected)[7:31]
        or os.geteuid() != 0
    ):
        raise EnrollmentError("Enrollment requires root and the exact applicable plan.")
    refreshed = enrollment_plan(
        host_id=plan["host_id"],
        control_plane_url=plan["control_plane_url"],
        token_file=Path(plan["token_file"]),
        trust_root=Path(plan["trust_root"]),
        identity_root=Path(plan["identity_root"]),
        config_path=Path(plan["config_path"]),
        bootstrap_ca_path=None
        if plan["bootstrap_ca_path"] is None
        else Path(plan["bootstrap_ca_path"]),
    )
    if refreshed.get("confirmation_token") != confirmation:
        raise EnrollmentError("Enrollment inputs changed after planning.")
    command = runner or SubprocessRunner()
    original_config = Path(plan["config_path"]).read_bytes()
    original_config_mode = stat.S_IMODE(Path(plan["config_path"]).stat().st_mode)
    trust_root = Path(plan["trust_root"])
    identity_root = Path(plan["identity_root"])
    trust_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    if trust_root.is_symlink() or not trust_root.is_dir():
        raise EnrollmentError("Enrollment trust root is unsafe.")
    command.run(["chown", "root:ophelia", str(trust_root)], timeout_seconds=30)
    os.chmod(trust_root, 0o750)
    identity_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if identity_root.is_symlink() or not identity_root.is_dir():
        raise EnrollmentError("Enrollment identity root is unsafe.")
    command.run(["chown", "ophelia:ophelia", str(identity_root)], timeout_seconds=30)
    os.chmod(identity_root, 0o700)
    with tempfile.TemporaryDirectory(prefix=".enroll-", dir=identity_root) as directory:
        staging = Path(directory)
        private_key = staging / "host.key"
        csr = staging / "host.csr"
        command.run(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-subj",
                "/CN=" + plan["host_id"],
                "-keyout",
                str(private_key),
                "-out",
                str(csr),
            ],
            timeout_seconds=60,
        )
        os.chmod(private_key, 0o600)
        token = _read_token(Path(plan["token_file"]))
        exchange_fn = exchange or _exchange_enrollment
        response = exchange_fn(
            plan["control_plane_url"],
            token,
            plan["host_id"],
            csr.read_text(encoding="utf-8"),
            None
            if plan["bootstrap_ca_path"] is None
            else Path(plan["bootstrap_ca_path"]),
        )
        material = _validate_response(response, plan["host_id"], plan["control_plane_url"])
        certificate = staging / "host.crt"
        ca = staging / "control-plane-ca.pem"
        decision_key = staging / "decision-public.pem"
        _secure_write(certificate, material["host_certificate_pem"].encode("utf-8"), mode=0o644)
        _secure_write(ca, material["control_plane_ca_pem"].encode("utf-8"), mode=0o644)
        _secure_write(decision_key, material["decision_public_key_pem"].encode("utf-8"), mode=0o644)
        _verify_material(command, private_key, certificate, ca, decision_key)
        target_paths = {name: Path(path) for name, path in plan["targets"].items()}
        for source, name, mode in (
            (private_key, "host_key", 0o600),
            (certificate, "host_certificate", 0o644),
            (ca, "control_plane_ca", 0o644),
            (decision_key, "decision_public_key", 0o644),
        ):
            _secure_write(target_paths[name], source.read_bytes(), mode=mode)
        enrollment = {
            "schema_version": 1,
            "kind": "ophelia.host-enrollment",
            "host_id": plan["host_id"],
            "control_plane_url": plan["control_plane_url"],
            "protocol_version": PROTOCOL_VERSION,
            "agent_version": package_version(),
            "certificate_not_after": material["certificate_not_after"],
            "certificate_digest": _digest_file(target_paths["host_certificate"]),
            "ca_digest": _digest_file(target_paths["control_plane_ca"]),
            "decision_key_digest": _digest_file(target_paths["decision_public_key"]),
        }
        _secure_write(
            target_paths["enrollment"],
            (json.dumps(enrollment, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o644,
        )
    command.run(
        [
            "chown",
            "ophelia:ophelia",
            plan["targets"]["host_key"],
            plan["targets"]["host_certificate"],
            plan["targets"]["enrollment"],
        ],
        timeout_seconds=30,
    )
    _enable_agent_config(Path(plan["config_path"]), plan)
    command.run(
        ["chown", "root:ophelia", plan["config_path"]], timeout_seconds=30
    )
    try:
        command.run(["systemctl", "restart", "opheliad.service"], timeout_seconds=120)
        command.run(
            ["systemctl", "is-active", "--quiet", "opheliad.service"],
            timeout_seconds=60,
        )
    except Exception as exc:
        _secure_write(
            Path(plan["config_path"]), original_config, mode=original_config_mode
        )
        command.run(
            ["chown", "root:ophelia", plan["config_path"]], timeout_seconds=30
        )
        for target in plan["targets"].values():
            path = Path(target)
            if path.is_symlink():
                raise EnrollmentError(
                    "Enrollment rollback encountered an unsafe target."
                ) from exc
            if path.is_file():
                path.unlink()
        command.run(
            ["systemctl", "restart", "opheliad.service"],
            timeout_seconds=120,
            check=False,
        )
        raise EnrollmentError(
            "Enrolled identity did not pass the daemon health check; local changes were rolled back."
        ) from exc
    Path(plan["token_file"]).unlink()
    return {
        "schema_version": 1,
        "kind": "ophelia.enrollment-receipt",
        "status": "succeeded",
        "host_id": plan["host_id"],
        "control_plane_url": plan["control_plane_url"],
        "certificate_not_after": enrollment["certificate_not_after"],
        "certificate_digest": enrollment["certificate_digest"],
        "decision_key_digest": enrollment["decision_key_digest"],
        "token_deleted": True,
    }


def _exchange_enrollment(
    control_plane_url: str,
    token: str,
    host_id: str,
    csr_pem: str,
    ca_path: Optional[Path],
) -> Dict[str, Any]:
    parsed = urlparse(control_plane_url)
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=None if ca_path is None else str(ca_path),
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    body = json.dumps(
        {
            "schema_version": 1,
            "kind": "ophelia.enrollment-request",
            "protocol_version": PROTOCOL_VERSION,
            "agent_version": package_version(),
            "host_id": host_id,
            "csr_pem": csr_pem,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port,
        timeout=30,
        context=context,
    )
    try:
        connection.request(
            "POST",
            parsed.path.rstrip("/") + "/api/ophelia/v1/enrollment/exchange",
            body=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-Ophelia-Protocol-Version": str(PROTOCOL_VERSION),
            },
        )
        response = connection.getresponse()
        encoded = response.read(1024 * 1024 + 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise EnrollmentError("Enrollment exchange failed.") from exc
    finally:
        connection.close()
    if response.status != 200 or len(encoded) > 1024 * 1024:
        raise EnrollmentError("Enrollment exchange was rejected or oversized.")
    if response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
        raise EnrollmentError("Enrollment response is not JSON.")
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise EnrollmentError("Enrollment response is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise EnrollmentError("Enrollment response must be an object.")
    return value


def _validate_response(value: Dict[str, Any], host_id: str, endpoint: str) -> Dict[str, str]:
    fields = {
        "schema_version",
        "kind",
        "protocol_version",
        "host_id",
        "control_plane_url",
        "host_certificate_pem",
        "control_plane_ca_pem",
        "decision_public_key_pem",
        "certificate_not_after",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise EnrollmentError("Enrollment response fields are invalid.")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "ophelia.enrollment-response"
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("host_id") != host_id
        or value.get("control_plane_url") != endpoint
    ):
        raise EnrollmentError("Enrollment response identity is invalid.")
    for field in fields - {"schema_version", "protocol_version"}:
        if not isinstance(value.get(field), str) or not value[field] or len(value[field]) > 262144:
            raise EnrollmentError("Enrollment response %s is invalid." % field)
    if parse_utc(value["certificate_not_after"]) <= datetime.now(timezone.utc):
        raise EnrollmentError("Enrollment certificate is already expired.")
    return value


def _verify_material(runner, private_key, certificate, ca, decision_key) -> None:
    runner.run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "60"], timeout_seconds=30)
    runner.run(["openssl", "verify", "-CAfile", str(ca), str(certificate)], timeout_seconds=30)
    runner.run(["openssl", "pkey", "-pubin", "-in", str(decision_key), "-noout"], timeout_seconds=30)
    private_public = runner.run(["openssl", "pkey", "-in", str(private_key), "-pubout"], timeout_seconds=30)
    certificate_public = runner.run(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"], timeout_seconds=30)
    if private_public.stdout.strip() != certificate_public.stdout.strip():
        raise EnrollmentError("Enrolled certificate does not match the generated host key.")


def _enable_agent_config(path: Path, plan: Dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise EnrollmentError("Daemon configuration is unavailable or unsafe.")
    text = path.read_text(encoding="utf-8")
    replacements = {
        "host_id": json.dumps(plan["host_id"]),
        "agent_enabled": "true",
        "control_plane_url": json.dumps(plan["control_plane_url"]),
        "control_plane_ca_path": json.dumps(plan["targets"]["control_plane_ca"]),
        "host_certificate_path": json.dumps(plan["targets"]["host_certificate"]),
        "host_private_key_path": json.dumps(plan["targets"]["host_key"]),
        "decision_public_key_path": json.dumps(plan["targets"]["decision_public_key"]),
    }
    lines = []
    found = set()
    assignment = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=")
    for line in text.splitlines():
        match = assignment.match(line)
        if match and match.group(1) in replacements:
            key = match.group(1)
            lines.append("%s = %s" % (key, replacements[key]))
            found.add(key)
        else:
            lines.append(line)
    for key in sorted(set(replacements) - found):
        lines.append("%s = %s" % (key, replacements[key]))
    _secure_write(path, ("\n".join(lines) + "\n").encode("utf-8"), mode=0o640)


def _read_token(path: Path) -> str:
    value = _private_token_file(path).read_text(encoding="utf-8").strip()
    if not value or len(value) > 4096 or any(character.isspace() for character in value):
        raise EnrollmentError("Enrollment token file content is invalid.")
    return value


def _private_token_file(value: Path) -> Path:
    path = _trusted_file(value, "token_file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise EnrollmentError("Enrollment token file must use mode 0600 or stricter.")
    if path.stat().st_size > 4096:
        raise EnrollmentError("Enrollment token file is too large.")
    return path


def _trusted_file(value: Path, field: str) -> Path:
    path = _absolute_path(value, field)
    if path.is_symlink() or not path.is_file():
        raise EnrollmentError("%s must be a trusted regular file." % field)
    metadata = path.stat()
    if metadata.st_uid not in {0, os.geteuid()}:
        raise EnrollmentError("%s has an untrusted owner." % field)
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise EnrollmentError("%s may not be group- or world-writable." % field)
    return path


def _absolute_path(value: Path, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise EnrollmentError("%s must be a safe absolute path." % field)
    return path


def _https_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EnrollmentError("Control-plane URL must be safe HTTPS.")
    return value.rstrip("/")


def _digest_file(path: Path) -> Optional[str]:
    if path.is_symlink() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
