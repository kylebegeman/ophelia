from __future__ import annotations

import difflib
import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .execution.staging import (
    OperationStaging,
    StagingError,
    confirmation_token,
)
from .manifest import Manifest
from .operation_schema import attach_digest, diff_artifact, operation_id
from .policy import policy_check_entry
from .redaction import redact_url, redacted_compose_text
from .runtime import (
    DeployMetadata,
    _release_metadata_env,
    active_release,
    bundle_hash,
    image_digests,
    image_references,
    render_bundle,
    static_asset_plan,
)
from .verify import verification_checks


def deploy_plan(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
    artifacts_dir: Optional[Path] = None,
    *,
    plan_operation_id: Optional[str] = None,
    deploy_metadata: DeployMetadata | None = None,
) -> Dict[str, object]:
    uploaded_operation_id = plan_operation_id
    plan_operation_id = plan_operation_id or operation_id(
        "deploy.plan", manifest.app, getattr(manifest, "environment", None)
    )
    effective_metadata = _planned_deploy_metadata(
        manifest,
        runtime_root,
        deploy_metadata,
        plan_operation_id,
    )
    release_metadata = _release_metadata_env(
        manifest,
        release_id=effective_metadata["release_id"],
        commit_sha=effective_metadata["commit_sha"],
        build_time=effective_metadata["build_time"],
    )
    bundle = render_bundle(manifest, release_metadata=release_metadata)
    candidate_bundle = {
        **bundle,
        Path("deploy-metadata.json"): json.dumps(
            effective_metadata, indent=2, sort_keys=True
        )
        + "\n",
    }
    diff = bundle_diff(manifest, runtime_root, desired_bundle=bundle)
    static_assets = static_asset_plan(manifest, manifest_path, runtime_root)
    staging = (
        OperationStaging.open_uploaded(runtime_root, plan_operation_id)
        if uploaded_operation_id is not None
        else OperationStaging.create(runtime_root, plan_operation_id)
    )
    _validate_artifacts_dir(artifacts_dir, runtime_root, staging)
    if uploaded_operation_id is not None:
        if manifest_path.parent.resolve(strict=False) != staging.candidate.resolve(strict=False):
            raise StagingError("Uploaded plan manifest must be inside its candidate staging directory.")
        candidate_digest = staging.finalize_uploaded_candidate(candidate_bundle)
    else:
        candidate_digest = staging.stage_candidate(
            manifest,
            manifest_path,
            candidate_bundle,
            stage_static=static_assets.get("change") != "blocked",
        )

    env_requirements = _env_requirements(bundle.get(Path("env.example"), ""))
    checks = verification_checks(manifest)
    verify_policy = manifest.verify_policy
    images = image_references(manifest)
    digests = image_digests(manifest)
    routes = [
        {
            "domain": route.domain,
            "service": route.service,
            "upstream": route.upstream,
            "path": route.path,
            "path_prefix": route.path_prefix,
        }
        for route in manifest.routes
    ]
    risk_notes = _risk_notes(manifest, env_requirements, diff, static_assets)

    artifacts: List[Dict[str, object]] = []
    evidence_bindings: List[Dict[str, str]] = []
    if diff["compose_changes"]:
        compose_artifact, compose_binding = _write_compose_diff_artifact(
            manifest,
            bundle,
            runtime_root,
            staging,
        )
        artifacts.append(compose_artifact)
        evidence_bindings.append(compose_binding)

    evidence_payload = {
        "operation_id": plan_operation_id,
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "candidate_digest": candidate_digest,
        "deploy_metadata": effective_metadata,
        "rendered_bundle_hash": bundle_hash(bundle),
        "changed_files": diff["changed_files"],
        "removed_files": diff["removed_files"],
        "static_assets": static_assets,
        "artifact_digests": sorted(item["sha256"] for item in evidence_bindings),
    }
    evidence_path, evidence_digest = staging.write_evidence(
        "plan-evidence.json",
        json.dumps(evidence_payload, indent=2, sort_keys=True) + "\n",
    )
    evidence_bindings.append({"name": evidence_path.name, "sha256": evidence_digest})
    artifacts.append(
        {
            "name": "plan-evidence",
            "kind": "ophelia.artifact.plan-evidence",
            "media_type": "application/json",
            "path": str(evidence_path),
            "sha256": evidence_digest,
            "redacted": True,
        }
    )
    evidence_digests = sorted(item["sha256"] for item in evidence_bindings)

    plan = {
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "operation_id": plan_operation_id,
        "staging": {
            "root": str(staging.root),
            "candidate": str(staging.candidate),
            "evidence": str(staging.evidence),
        },
        "candidate_digest": candidate_digest,
        "evidence_digests": evidence_digests,
        "deploy_metadata": effective_metadata,
        "kind": manifest.kind,
        "profile": manifest.profile,
        "manifest_path": str(manifest_path),
        "rendered_bundle_hash": bundle_hash(bundle),
        "services_affected": sorted(manifest.services.keys()),
        "images": images,
        "image_digests": digests,
        "routes": routes,
        "domains": sorted({route.domain for route in manifest.routes}),
        "env_requirements": env_requirements,
        "addons": {
            "postgres": manifest.addons.postgres,
            "redis": manifest.addons.redis,
        },
        "generated_files": [str(path) for path in sorted(bundle)],
        "static_assets": static_assets,
        "changed_files": diff["changed_files"],
        "removed_files": diff["removed_files"],
        "caddy_changes": diff["caddy_changes"],
        "compose_changes": diff["compose_changes"],
        "verification_checks": [
            {
                "name": check.name or redact_url(check.url),
                "url": redact_url(check.url),
                "expect_status": check.expect_status,
                "contains_required": check.contains is not None,
            }
            for check in checks
        ],
        "verification_policy": {
            "attempts": verify_policy.attempts,
            "interval": verify_policy.interval,
            "timeout": verify_policy.timeout,
            "failure_mode": verify_policy.failure_mode,
        },
        "risk_notes": risk_notes,
        "changes": _deploy_changes(diff, static_assets),
        "artifacts": artifacts,
        "summary": _summary(manifest, images, diff, env_requirements, checks, static_assets),
    }
    plan["confirmation_required"] = plan["environment"] == "production"

    image_digest_pinned = bool(images) and all("@sha256:" in image for image in images.values())
    policy_context = {
        "confirmation_required": bool(plan["confirmation_required"]),
        "plan_exists": True,
        "image_digest_pinned": image_digest_pinned,
        "json_receipts": True,
    }
    plan["checks"] = [
        policy_check_entry(
            "deploy.apply",
            plan["app"],
            plan["environment"],
            policy_context,
            runtime_root=runtime_root,
        )
    ]
    plan["confirmation_token"] = (
        deploy_confirmation_token(plan) if plan["confirmation_required"] else None
    )
    finalized = attach_digest(
        plan,
        operation="deploy.plan",
        risk="high" if plan["confirmation_required"] else "medium",
    )
    if finalized["confirmation_required"]:
        staging.write_binding(
            {
                "schema_version": 1,
                "kind": "ophelia.deploy-plan-binding",
                "operation_id": plan_operation_id,
                "app": manifest.app,
                "environment": getattr(manifest, "environment", None),
                "candidate_digest": candidate_digest,
                "evidence": evidence_bindings,
                "deploy_metadata": effective_metadata,
                "confirmation_payload": deploy_confirmation_payload(finalized),
                "confirmation_token": finalized["confirmation_token"],
            }
        )
    return finalized


def _classify_target(path: str) -> str:
    if path == "compose.yml":
        return "compose"
    if path.startswith("caddy/"):
        return "caddy"
    if path == "env.example":
        return "env"
    if path == "manifest.lock.json":
        return "manifest"
    return "other"


def _deploy_changes(diff: Dict[str, object], static_assets: Dict[str, object]) -> List[Dict[str, object]]:
    """Structured, redaction-safe before/after summaries from ``bundle_diff``.

    Only paths and change types are surfaced, never file content, so the list is
    always safe to serialize alongside the plan.
    """
    changes: List[Dict[str, object]] = []
    for item in [*diff["changed_files"], *diff["removed_files"]]:
        path = str(item["path"])
        changes.append(
            {
                "path": path,
                "change": item["change"],
                "target": _classify_target(path),
            }
        )
    if static_assets_change := _static_asset_change(diff.get("app"), static_assets):
        changes.append(static_assets_change)
    return changes


def _static_asset_change(app: object, static_assets: Dict[str, object]) -> Dict[str, object] | None:
    if not static_assets.get("managed"):
        return None
    change = str(static_assets.get("change") or "")
    if change == "none":
        return None
    return {
        "path": f"static/{app}/current",
        "change": change,
        "target": "static-assets",
        "source": static_assets.get("source"),
        "source_exists": static_assets.get("source_exists"),
        "source_digest": static_assets.get("source_digest"),
    }


def _write_compose_diff_artifact(
    manifest: Manifest,
    bundle: Dict[Path, str],
    runtime_root: Path,
    staging: OperationStaging,
) -> tuple[Dict[str, object], Dict[str, str]]:
    """Write a redacted unified diff of current vs desired ``compose.yml``.

    Both sides are routed through :func:`redacted_compose_text` *before* the diff
    is computed, so the artifact file can never contain a secret value. Evidence
    writes are operation-scoped and failures abort planning explicitly.
    """
    desired_compose = bundle.get(Path("compose.yml"), "")
    current_path = runtime_root / "apps" / manifest.app / "compose.yml"
    try:
        current_compose = current_path.read_text() if current_path.exists() else ""
    except OSError:
        current_compose = ""

    desired_redacted = redacted_compose_text(desired_compose)
    current_redacted = redacted_compose_text(current_compose) if current_compose else ""
    diff_lines = difflib.unified_diff(
        current_redacted.splitlines(keepends=True),
        desired_redacted.splitlines(keepends=True),
        fromfile="current/compose.yml",
        tofile="desired/compose.yml",
    )
    diff_text = "".join(diff_lines)

    diff_path, digest = staging.write_evidence("compose-diff.diff", diff_text)
    artifact = diff_artifact(
        "compose-diff",
        diff_path,
        "Rendered Compose diff with env values redacted.",
    )
    artifact["sha256"] = digest
    return artifact, {"name": diff_path.name, "sha256": digest}


def bundle_diff(
    manifest: Manifest,
    runtime_root: Path,
    desired_bundle: Dict[Path, str] | None = None,
) -> Dict[str, object]:
    bundle = desired_bundle or _render_bundle_for_runtime(manifest, runtime_root)
    app_root = runtime_root / "apps" / manifest.app
    changed_files = []
    for relative_path, desired_content in sorted(bundle.items()):
        target = app_root / relative_path
        current_hash = _file_hash(target)
        desired_hash = hashlib.sha256(desired_content.encode("utf-8")).hexdigest()
        if current_hash != desired_hash:
            changed_files.append(
                {
                    "path": str(relative_path),
                    "exists": target.exists(),
                    "change": "modify" if target.exists() else "create",
                }
            )

    desired_paths = {path for path in bundle}
    current_generated = _current_generated_files(app_root)
    removed_files = [
        {"path": str(path), "change": "remove"}
        for path in sorted(current_generated - desired_paths)
    ]

    caddy_changes = [
        item for item in [*changed_files, *removed_files] if str(item["path"]).startswith("caddy/")
    ]
    compose_changes = [
        item for item in [*changed_files, *removed_files] if str(item["path"]) == "compose.yml"
    ]
    return {
        "app": manifest.app,
        "runtime_path": str(app_root),
        "changed_files": changed_files,
        "removed_files": removed_files,
        "caddy_changes": caddy_changes,
        "compose_changes": compose_changes,
        "clean": not changed_files and not removed_files,
    }


def _planned_deploy_metadata(
    manifest: Manifest,
    runtime_root: Path,
    requested: DeployMetadata | None,
    plan_operation_id: str,
) -> Dict[str, str]:
    requested = requested or DeployMetadata()
    release = active_release(runtime_root, manifest.app) or _latest_release(runtime_root, manifest.app)
    runtime_env = release.get("runtime_env") if isinstance(release, dict) else None
    if not isinstance(runtime_env, dict):
        runtime_env = {}

    def first(*values: object) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    release_id = first(
        requested.release_id,
        os.environ.get("OPHELIA_DEPLOY_RELEASE_ID"),
        os.environ.get("OPHELIA_RELEASE_ID"),
        runtime_env.get("release_id"),
        "plan-" + hashlib.sha256(plan_operation_id.encode("utf-8")).hexdigest()[:16],
    )
    commit_sha = first(
        requested.commit_sha,
        os.environ.get("OPHELIA_DEPLOY_COMMIT_SHA"),
        os.environ.get("OPHELIA_COMMIT_SHA"),
        os.environ.get("GITHUB_SHA"),
        runtime_env.get("commit_sha"),
    )
    build_time = first(
        requested.build_time,
        os.environ.get("OPHELIA_DEPLOY_BUILD_TIME"),
        os.environ.get("OPHELIA_BUILD_TIME"),
        runtime_env.get("build_time"),
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    metadata = {
        "release_id": release_id,
        "commit_sha": commit_sha,
        "build_time": build_time,
    }
    for field, value, maximum in (
        ("release_id", release_id, 255),
        ("commit_sha", commit_sha, 255),
        ("build_time", build_time, 128),
    ):
        if len(value) > maximum or any(
            unicodedata.category(character) == "Cc" for character in value
        ):
            raise StagingError(
                f"Deploy metadata {field} contains controls or exceeds {maximum} characters."
            )
    return metadata


def _render_bundle_for_runtime(manifest: Manifest, runtime_root: Path) -> Dict[Path, str]:
    release = active_release(runtime_root, manifest.app) or _latest_release(runtime_root, manifest.app)
    release_metadata = release.get("runtime_env") if isinstance(release, dict) else None
    if not isinstance(release_metadata, dict):
        release_metadata = None
    return render_bundle(manifest, release_metadata=release_metadata)


def _latest_release(runtime_root: Path, app: str) -> Dict[str, object]:
    release_path = runtime_root / "apps" / app / "release.json"
    if not release_path.exists():
        return {}
    try:
        payload = json.loads(release_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def deploy_confirmation_payload(plan: Dict[str, object]) -> Dict[str, object]:
    return {
        "action": "deploy.apply",
        "operation_id": plan.get("operation_id"),
        "app": plan.get("app"),
        "environment": plan.get("environment"),
        "rendered_bundle_hash": plan.get("rendered_bundle_hash"),
        "changed_files": plan.get("changed_files"),
        "removed_files": plan.get("removed_files"),
        "static_assets": plan.get("static_assets"),
        "candidate_digest": plan.get("candidate_digest"),
        "evidence_digests": plan.get("evidence_digests"),
        "deploy_metadata": plan.get("deploy_metadata"),
    }


def deploy_confirmation_token(plan: Dict[str, object]) -> str:
    return confirmation_token(deploy_confirmation_payload(plan))


def _current_generated_files(app_root: Path) -> set[Path]:
    if not app_root.exists():
        return set()
    files = {Path("compose.yml"), Path("env.example"), Path("manifest.lock.json")}
    for caddy_file in (app_root / "caddy").rglob("*.caddy") if (app_root / "caddy").exists() else []:
        files.add(caddy_file.relative_to(app_root))
    return {path for path in files if (app_root / path).exists()}


def _env_requirements(env_example: str) -> List[Dict[str, object]]:
    by_key: Dict[str, Dict[str, object]] = {}
    for raw_line in env_example.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        placeholder = _is_placeholder(value)
        existing = by_key.get(key)
        if existing is None or not placeholder:
            by_key[key] = {
                "key": key,
                "placeholder": placeholder,
                "required_for_apply": placeholder,
            }
    return [by_key[key] for key in sorted(by_key)]


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered


def _risk_notes(
    manifest: Manifest,
    env_requirements: List[Dict[str, object]],
    diff: Dict[str, object],
    static_assets: Dict[str, object],
) -> List[str]:
    notes: List[str] = []
    if any(item["required_for_apply"] for item in env_requirements):
        notes.append("One or more env keys still use placeholder values in env.example.")
    if manifest.addons.postgres:
        notes.append("Deploy may depend on shared Postgres availability.")
    if manifest.addons.redis:
        notes.append("Deploy may depend on shared Redis availability.")
    if diff["removed_files"]:
        notes.append("Desired bundle omits previously generated files; apply will not delete runtime secrets.")
    if static_assets.get("managed") and not static_assets.get("source_exists"):
        notes.append("Static asset source is missing; apply will fail until the source directory exists.")
    if static_assets.get("managed") and static_assets.get("source_exists") and not static_assets.get("source_is_dir"):
        notes.append("Static asset source is not a directory; apply requires a directory root.")
    if static_assets.get("managed") and static_assets.get("unsafe_symlinks"):
        notes.append("Static asset source contains unsafe symlinks; apply requires symlinks to stay inside the static root.")
    if not verification_checks(manifest):
        notes.append("No verification checks are configured or inferred.")
    return notes


def _summary(
    manifest: Manifest,
    images: Dict[str, str],
    diff: Dict[str, object],
    env_requirements: List[Dict[str, object]],
    checks: object,
    static_assets: Dict[str, object],
) -> str:
    secret_count = sum(1 for item in env_requirements if item["required_for_apply"])
    environment = getattr(manifest, "environment", None)
    static_detail = ""
    if static_assets.get("managed"):
        static_detail = f", static assets {static_assets.get('change')}"
    return (
        f"This deploy updates {manifest.app}"
        f"{' ' + environment if environment else ''} "
        f"with {len(images)} image reference(s), {len(diff['caddy_changes'])} Caddy change(s), "
        f"{secret_count} placeholder env key(s), {len(checks)} verification check(s){static_detail}."
    )


def _validate_artifacts_dir(
    artifacts_dir: Optional[Path],
    runtime_root: Path,
    staging: OperationStaging,
) -> None:
    if artifacts_dir is None:
        return
    requested = artifacts_dir.resolve(strict=False)
    staging_base = (runtime_root / "staging").resolve(strict=False)
    operation_root = staging.root.resolve(strict=False)
    if requested not in {staging_base, operation_root, staging.evidence.resolve(strict=False)}:
        raise StagingError(
            "--artifacts-dir is restricted to the operation staging tree; "
            f"use {staging.evidence}"
        )


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
