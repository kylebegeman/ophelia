"""Verification and durable redaction of Lumen decision claims."""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..domain import (
    Actor,
    ApprovedPlanRef,
    AuthorizationKind,
    OperationPlan,
    canonical_digest,
)
from ..domain._contracts import canonical_json, parse_utc
from ..execution.subprocesses import SubprocessRunner


_CLAIM_FIELDS = {
    "schema_version",
    "kind",
    "plan_id",
    "plan_digest",
    "request_digest",
    "manifest_digest",
    "artifact_digests",
    "observed_state_digest",
    "policy_digest",
    "host_id",
    "app",
    "environment",
    "revision_id",
    "revision_digest",
    "actor_id",
    "decision_id",
    "issuer",
    "audience",
    "approved_at",
    "expires_at",
    "nonce",
    "signature",
}


def verify_signed_lumen_document(
    document: Dict[str, Any],
    *,
    public_key_path: Path,
    runtime_root: Path,
    runner: Optional[SubprocessRunner] = None,
) -> str:
    """Verify a canonical detached signature and return the envelope digest."""

    signature_text = document.get("signature") if isinstance(document, dict) else None
    if not isinstance(signature_text, str) or not signature_text or len(signature_text) > 16384:
        raise ValueError("Lumen document signature is invalid.")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except ValueError as exc:
        raise ValueError("Lumen document signature is not valid base64.") from exc
    if not signature:
        raise ValueError("Lumen document signature is empty.")
    signed = dict(document)
    signed.pop("signature")
    _verify_signature(
        canonical_json(signed).encode("utf-8"),
        signature,
        public_key_path=public_key_path,
        runtime_root=runtime_root,
        runner=runner,
    )
    return canonical_digest(document)


def verify_lumen_decision(
    claim: Dict[str, Any],
    *,
    plan: OperationPlan,
    actor: Actor,
    public_key_path: Path,
    runtime_root: Path,
    runner: Optional[SubprocessRunner] = None,
    now: Optional[datetime] = None,
) -> ApprovedPlanRef:
    """Verify a detached signature and return only non-reusable approval evidence."""

    if not isinstance(claim, dict) or set(claim) != _CLAIM_FIELDS:
        raise ValueError("Lumen decision claim fields are invalid.")
    if claim.get("schema_version") != 1 or claim.get("kind") != "lumen.ophelia-decision":
        raise ValueError("Lumen decision claim version or kind is unsupported.")
    verify_signed_lumen_document(
        claim,
        public_key_path=public_key_path,
        runtime_root=runtime_root,
        runner=runner,
    )
    expected = {
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest(),
        "request_digest": plan.request_digest,
        "manifest_digest": plan.manifest_digest,
        "artifact_digests": list(plan.artifact_digests),
        "observed_state_digest": plan.observed_state_digest,
        "policy_digest": plan.policy_digest,
        "host_id": plan.host_id,
        "app": plan.app,
        "environment": plan.environment,
        "revision_id": plan.revision_id,
        "revision_digest": plan.revision_digest,
        "actor_id": actor.actor_id,
        "audience": plan.host_id,
    }
    if any(claim.get(key) != value for key, value in expected.items()):
        raise ValueError("Lumen decision does not bind the exact local plan and actor.")
    if claim.get("issuer") != "lumen-control-plane":
        raise ValueError("Lumen decision issuer is not trusted.")
    approved_at = _text(claim.get("approved_at"), "approved_at")
    expires_at = _text(claim.get("expires_at"), "expires_at")
    observed_now = now or datetime.now(timezone.utc)
    approved = parse_utc(approved_at)
    expires = parse_utc(expires_at)
    if approved > observed_now + timedelta(minutes=2):
        raise ValueError("Lumen decision approval time exceeds allowed clock skew.")
    if expires <= observed_now:
        raise ValueError("Lumen decision has expired.")
    if expires > approved + timedelta(minutes=15):
        raise ValueError("Lumen decision lifetime exceeds the allowed maximum.")
    return ApprovedPlanRef.bind(
        plan,
        actor_id=actor.actor_id,
        decision_id=_text(claim.get("decision_id"), "decision_id"),
        authorization_kind=AuthorizationKind.LUMEN_DECISION,
        issuer="lumen-control-plane",
        audience=plan.host_id,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_nonce=_text(claim.get("nonce"), "nonce"),
    )


def approval_from_dict(value: Dict[str, Any]) -> ApprovedPlanRef:
    """Rehydrate already verified, nonce-free evidence after an agent restart."""

    return ApprovedPlanRef(
        plan_id=value["plan_id"],
        plan_digest=value["plan_digest"],
        request_digest=value["request_digest"],
        manifest_digest=value["manifest_digest"],
        artifact_digests=tuple(value["artifact_digests"]),
        observed_state_digest=value["observed_state_digest"],
        policy_digest=value["policy_digest"],
        host_id=value["host_id"],
        app=value["app"],
        environment=value["environment"],
        revision_id=value["revision_id"],
        revision_digest=value["revision_digest"],
        actor_id=value["actor_id"],
        decision_id=value["decision_id"],
        authorization_kind=AuthorizationKind(value["authorization_kind"]),
        issuer=value["issuer"],
        audience=value["audience"],
        approved_at=value["approved_at"],
        expires_at=value["expires_at"],
        nonce_digest=value["nonce_digest"],
        approval_digest=value["approval_digest"],
    )


def _verify_signature(
    payload: bytes,
    signature: bytes,
    *,
    public_key_path: Path,
    runtime_root: Path,
    runner: Optional[SubprocessRunner],
) -> None:
    key = Path(public_key_path)
    if key.is_symlink() or not key.is_file():
        raise ValueError("Lumen decision public key is unavailable.")
    temporary_root = Path(runtime_root) / "run" / "decision-verification"
    temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(temporary_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="verify-", dir=temporary_root) as directory:
        root = Path(directory)
        payload_path = root / "claim.json"
        signature_path = root / "claim.sig"
        _write_private(payload_path, payload)
        _write_private(signature_path, signature)
        result = (runner or SubprocessRunner()).run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(key),
                "-signature",
                str(signature_path),
                str(payload_path),
            ],
            timeout_seconds=30,
            check=False,
        )
        if result.exit_reason != "success":
            raise ValueError("Lumen decision signature verification failed.")


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise ValueError("Lumen decision %s is invalid." % field)
    return value
