from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List

from .manifest import Manifest, ManifestError, load_manifest
from .planning import bundle_diff
from .runtime import bundle_hash, render_bundle


def manifest_drift(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    app_root = runtime_root / "apps" / manifest.app
    diff = bundle_diff(manifest, runtime_root)
    release_metadata = _release_metadata(manifest, manifest_path, runtime_root)
    env_status = _env_status(app_root / "env.example", app_root / "env")
    drifted = not diff["clean"] or bool(release_metadata["mismatches"])
    return {
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "manifest_path": str(manifest_path),
        "runtime_path": str(app_root),
        "drift": drifted,
        "rendered": {
            "changed_files": diff["changed_files"],
            "removed_files": diff["removed_files"],
            "caddy_changes": diff["caddy_changes"],
            "compose_changes": diff["compose_changes"],
        },
        "release_metadata": release_metadata,
        "env": env_status,
        "summary": _summary(manifest.app, drifted, diff, release_metadata, env_status),
    }


def all_drift(manifest_dir: Path, runtime_root: Path) -> Dict[str, object]:
    reports: List[Dict[str, object]] = []
    errors: List[Dict[str, str]] = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            errors.append({"path": str(manifest_path), "error": str(exc)})
            continue
        reports.append(manifest_drift(manifest, manifest_path, runtime_root))
    return {
        "manifest_dir": str(manifest_dir),
        "runtime_root": str(runtime_root),
        "drift": any(report["drift"] for report in reports) or bool(errors),
        "reports": reports,
        "errors": errors,
    }


def _release_metadata(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    release_path = runtime_root / "apps" / manifest.app / "release.json"
    desired_manifest_hash = _file_hash(manifest_path)
    desired_bundle_hash = bundle_hash(render_bundle(manifest))
    if not release_path.exists():
        return {
            "present": False,
            "mismatches": ["release.json missing"],
            "desired_manifest_hash": desired_manifest_hash,
            "desired_bundle_hash": desired_bundle_hash,
        }

    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return {"present": True, "mismatches": ["release.json is not valid JSON"]}

    mismatches = []
    if payload.get("manifest_hash") != desired_manifest_hash:
        mismatches.append("manifest_hash")
    if payload.get("rendered_bundle_hash") != desired_bundle_hash:
        mismatches.append("rendered_bundle_hash")
    return {
        "present": True,
        "release_id": payload.get("release_id"),
        "manifest_hash": payload.get("manifest_hash"),
        "rendered_bundle_hash": payload.get("rendered_bundle_hash"),
        "desired_manifest_hash": desired_manifest_hash,
        "desired_bundle_hash": desired_bundle_hash,
        "mismatches": mismatches,
    }


def _env_status(env_example_path: Path, env_path: Path) -> Dict[str, object]:
    required = _env_keys(env_example_path)
    actual = _env_keys(env_path)
    placeholder = sorted(key for key, value in actual.items() if _is_placeholder(value))
    return {
        "source": str(env_path),
        "required_keys": sorted(required),
        "present_keys": sorted(key for key in required if key in actual),
        "missing_keys": sorted(key for key in required if key not in actual),
        "placeholder_keys": [key for key in placeholder if key in required],
    }


def _env_keys(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _summary(
    app: str,
    drifted: bool,
    diff: Dict[str, object],
    release_metadata: Dict[str, object],
    env_status: Dict[str, object],
) -> str:
    return (
        f"{app} {'has drift' if drifted else 'has no rendered drift'}: "
        f"{len(diff['changed_files'])} changed file(s), "
        f"{len(diff['removed_files'])} removed file(s), "
        f"{len(release_metadata['mismatches'])} release metadata mismatch(es), "
        f"{len(env_status['missing_keys'])} missing env key(s), "
        f"{len(env_status['placeholder_keys'])} placeholder env key(s)."
    )


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered
