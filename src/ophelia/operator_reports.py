from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, List

from .backup import backup_plan
from .conflicts import scan_conflicts
from .config import REPO_ROOT
from .explain import explain_manifest
from .host_inventory import legacy_host_inventory
from .inspection import status_report
from .manifest import Manifest, ManifestError, load_manifest
from .planning import bundle_diff
from .redaction import redact_url
from .runtime import active_release_id, list_releases
from .templates import render_env_example
from .verify import verification_checks


def host_inventory(runtime_root: Path, ophelia_root: Path = REPO_ROOT) -> Dict[str, object]:
    return legacy_host_inventory(runtime_root, ophelia_root, ophelia_root / "manifests")


def manifest_registry(manifest_dir: Path, runtime_root: Path) -> Dict[str, object]:
    entries = []
    errors = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})
            continue
        active = active_release_id(runtime_root, manifest.app)
        current = _current_release(runtime_root, manifest.app)
        entries.append(
            {
                "app": manifest.app,
                "environment": getattr(manifest, "environment", None),
                "manifest_path": str(manifest_path),
                "domains": sorted({route.domain for route in manifest.routes}),
                "kind": manifest.kind,
                "profile": manifest.profile,
                "current_deployed_release": active,
                "latest_release": current.get("release_id"),
                "last_validation": {"ok": True},
            }
        )
    return {"manifest_dir": str(manifest_dir), "manifests": entries, "errors": errors}


def release_registry(runtime_root: Path) -> Dict[str, object]:
    releases = []
    apps_root = runtime_root / "apps"
    if apps_root.exists():
        for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
            for release in list_releases(runtime_root, app_root.name):
                releases.append(
                    {
                        "app": release.get("app", app_root.name),
                        "environment": release.get("environment"),
                        "release_id": release.get("release_id"),
                        "git_sha": release.get("git_sha"),
                        "images": release.get("images", {}),
                        "image_digests": release.get("image_digests", {}),
                        "manifest_hash": release.get("manifest_hash"),
                        "deployed_at": release.get("deployed_at"),
                        "active": release.get("active"),
                        "latest": release.get("latest"),
                        "applied": release.get("applied"),
                        "verified": release.get("verified"),
                        "verification_status": (release.get("verification") or {}).get("status")
                        if isinstance(release.get("verification"), dict)
                        else None,
                        "rollback_eligible": bool(release.get("bundle_path")),
                    }
                )
    return {"runtime_root": str(runtime_root), "releases": releases}


def preflight_report(manifest_path: Path, runtime_root: Path, manifest_dir: Path) -> Dict[str, object]:
    warnings: List[str] = []
    blockers: List[str] = []
    try:
        manifest = load_manifest(manifest_path)
        validation = {"ok": True}
    except ManifestError as exc:
        return {
            "ok": False,
            "warnings": [],
            "blockers": [str(exc)],
            "recommended_next_command": f"./cli/ship validate {manifest_path}",
        }

    explanation = explain_manifest(manifest, manifest_path)
    diff = bundle_diff(manifest, runtime_root)
    conflicts = scan_conflicts(manifest_dir)
    secrets = secrets_required(manifest, manifest_path, runtime_root)
    backup = backup_plan(runtime_root, manifest.app)
    checks = verification_checks(manifest)
    status = status_report(runtime_root, REPO_ROOT, manifest_dir)

    if conflicts["conflicts"]:
        blockers.append("Cross-manifest conflicts detected.")
    if secrets["blocking_keys"]:
        warnings.append("Required env keys are missing or placeholder in runtime env.")
    if not checks:
        warnings.append("No verification checks are configured or inferred.")
    if not backup["can_apply"]:
        warnings.append("Backup readiness is incomplete for current runtime state.")

    return {
        "ok": not blockers,
        "validation": validation,
        "explain": explanation,
        "diff": diff,
        "conflicts": conflicts,
        "secrets": secrets,
        "caddy_validation": {"status": "rendered", "external_validation": "skipped"},
        "docker_image_pull_check": {"status": "skipped", "reason": "preflight does not pull images by default"},
        "disk_check": status["disk_usage"],
        "backup_readiness": backup,
        "verification_checks": [
            {"name": check.name or redact_url(check.url), "url": redact_url(check.url), "expect_status": check.expect_status}
            for check in checks
        ],
        "warnings": warnings,
        "blockers": blockers,
        "recommended_next_command": f"./cli/ship deploy {manifest_path} --plan",
        "summary": explanation["summary"],
    }


def secrets_required(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    env_path = runtime_root / "apps" / manifest.app / "env"
    actual = _env_values(env_path)
    requirements = []
    for key, value in _env_values_from_text(render_env_example(manifest)).items():
        placeholder = _is_placeholder(value)
        present = key in actual
        runtime_placeholder = _is_placeholder(actual.get(key, "")) if present else False
        if placeholder or runtime_placeholder:
            blocking = placeholder and (not present or runtime_placeholder)
            requirements.append(
                {
                    "key": key,
                    "source_env_file": str(env_path),
                    "present": present,
                    "missing": placeholder and not present,
                    "placeholder_detected": placeholder or runtime_placeholder,
                    "used_by": _used_by(manifest, key),
                    "blocking_for_apply": blocking,
                }
            )
    return {
        "app": manifest.app,
        "manifest_path": str(manifest_path),
        "requirements": sorted(requirements, key=lambda item: item["key"]),
        "blocking_keys": sorted(item["key"] for item in requirements if item["blocking_for_apply"]),
    }


def runtime_ownership(runtime_root: Path, app: str) -> Dict[str, object]:
    app_root = runtime_root / "apps" / app
    return {
        "app": app,
        "runtime_path": str(app_root),
        "ophelia_generated_files": _existing(app_root, ["compose.yml", "env.example", "manifest.lock.json", "release.json", "active_release.json", "caddy", "releases", "release-bundles"]),
        "operator_managed_files": _existing(app_root, ["env", "notes"]),
        "secret_runtime_files": _existing(app_root, ["env", "addons.json"]),
        "static_assets": _existing(runtime_root / "static", [app]),
        "volumes": ["Docker volumes are external to this runtime root report."],
        "backups": _existing(runtime_root / "backups" / "apps", [app]),
        "safe_to_regenerate": ["compose.yml", "env.example", "manifest.lock.json", "caddy/"],
        "never_overwrite_or_delete_automatically": ["env", "addons.json", "backups/", "Docker volumes", "static assets"],
    }


def _current_release(runtime_root: Path, app: str) -> Dict[str, object]:
    path = runtime_root / "apps" / app / "release.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _env_values(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return _env_values_from_text(path.read_text())


def _env_values_from_text(text: str) -> Dict[str, str]:
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered


def _used_by(manifest: Manifest, key: str) -> List[str]:
    uses = []
    if key in {"DATABASE_URL", "REDIS_URL"}:
        uses.append("addon")
    for service in manifest.services.values():
        if key in service.env:
            uses.append(f"service:{service.name}")
    if manifest.profile == "prism" and key.startswith("PRISM_"):
        uses.append("prism")
    return uses or ["app"]


def _existing(root: Path, names: List[str]) -> List[str]:
    return [str(root / name) for name in names if (root / name).exists()]


def _git_sha(root: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None
