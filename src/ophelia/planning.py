from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
from typing import Dict, List, Optional

from .manifest import Manifest
from .operation_schema import attach_digest, diff_artifact, operation_id
from .policy import policy_check_entry
from .redaction import redact_url, redacted_compose_text
from .runtime import bundle_hash, image_digests, image_references, render_bundle
from .verify import verification_checks


def deploy_plan(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path,
    artifacts_dir: Optional[Path] = None,
) -> Dict[str, object]:
    bundle = render_bundle(manifest)
    diff = bundle_diff(manifest, runtime_root)
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
    risk_notes = _risk_notes(manifest, env_requirements, diff)

    plan = {
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
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
        "changes": _deploy_changes(diff),
        "artifacts": [],
        "summary": _summary(manifest, images, diff, env_requirements, checks),
    }
    plan["confirmation_required"] = plan["environment"] == "production"
    plan["confirmation_token"] = deploy_confirmation_token(plan) if plan["confirmation_required"] else None

    # Additive, best-effort policy evaluation surfaced under `checks` only. The
    # deploy plan does not otherwise carry a `checks` list, so this seeds it; the
    # policy result never feeds the plan's top-level blockers/status.
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

    if diff["compose_changes"]:
        artifact_entry = _write_compose_diff_artifact(
            manifest,
            bundle,
            runtime_root,
            artifacts_dir,
        )
        if artifact_entry is not None:
            plan["artifacts"] = [artifact_entry]
    return attach_digest(plan, operation="deploy.plan", risk="high" if plan["confirmation_required"] else "medium")


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


def _deploy_changes(diff: Dict[str, object]) -> List[Dict[str, object]]:
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
    return changes


def _write_compose_diff_artifact(
    manifest: Manifest,
    bundle: Dict[Path, str],
    runtime_root: Path,
    artifacts_dir: Optional[Path],
) -> Optional[Dict[str, object]]:
    """Write a redacted unified diff of current vs desired ``compose.yml``.

    Both sides are routed through :func:`redacted_compose_text` *before* the diff
    is computed, so the artifact file can never contain a secret value. Writing
    is best-effort: any OSError skips the artifact rather than failing the plan.
    """
    desired_compose = bundle.get(Path("compose.yml"))
    if desired_compose is None:
        return None
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

    target_dir = artifacts_dir or (
        runtime_root / "plans" / operation_id("deploy.plan", manifest.app, getattr(manifest, "environment", None))
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        diff_path = target_dir / "compose-diff.diff"
        diff_path.write_text(diff_text)
    except OSError:
        return None
    return diff_artifact(
        "compose-diff",
        diff_path,
        "Rendered Compose diff with env values redacted.",
    )


def bundle_diff(manifest: Manifest, runtime_root: Path) -> Dict[str, object]:
    bundle = render_bundle(manifest)
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


def deploy_confirmation_token(plan: Dict[str, object]) -> str:
    import json

    payload = {
        "action": "deploy.apply",
        "app": plan.get("app"),
        "environment": plan.get("environment"),
        "rendered_bundle_hash": plan.get("rendered_bundle_hash"),
        "changed_files": plan.get("changed_files"),
        "removed_files": plan.get("removed_files"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


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
    if not verification_checks(manifest):
        notes.append("No verification checks are configured or inferred.")
    return notes


def _summary(
    manifest: Manifest,
    images: Dict[str, str],
    diff: Dict[str, object],
    env_requirements: List[Dict[str, object]],
    checks: object,
) -> str:
    secret_count = sum(1 for item in env_requirements if item["required_for_apply"])
    environment = getattr(manifest, "environment", None)
    return (
        f"This deploy updates {manifest.app}"
        f"{' ' + environment if environment else ''} "
        f"with {len(images)} image reference(s), {len(diff['caddy_changes'])} Caddy change(s), "
        f"{secret_count} placeholder env key(s), and {len(checks)} verification check(s)."
    )


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
