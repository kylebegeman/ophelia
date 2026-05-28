from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


def gc_plan(runtime_root: Path, keep_releases: int = 5) -> Dict[str, object]:
    candidates: List[Dict[str, object]] = []
    runtime_root = runtime_root.expanduser()
    apps_root = runtime_root / "apps"
    if apps_root.exists():
        for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
            protected_releases = _protected_release_ids(app_root)
            bundles_root = app_root / "release-bundles"
            bundles = sorted([path for path in bundles_root.iterdir() if path.is_dir()]) if bundles_root.exists() else []
            protected = protected_releases | {path.name for path in bundles[-keep_releases:]}
            for bundle in bundles:
                if bundle.name not in protected:
                    candidates.append({"path": str(bundle), "reason": "old release bundle"})
    static_root = runtime_root / "static"
    if static_root.exists():
        for app_root in sorted(path for path in static_root.iterdir() if path.is_dir()):
            versions_root = app_root / "versions"
            versions = sorted([path for path in versions_root.iterdir() if path.is_dir()]) if versions_root.exists() else []
            protected = {path.name for path in versions[-keep_releases:]}
            for version in versions:
                if version.name not in protected:
                    candidates.append({"path": str(version), "reason": "old static version"})
    temp_root = runtime_root / "tmp"
    if temp_root.exists():
        for child in temp_root.iterdir():
            candidates.append({"path": str(child), "reason": "runtime temp artifact"})
    plan = {
        "runtime_root": str(runtime_root),
        "candidates": candidates,
        "never_delete": ["current release", "rollback window", "env", "secrets", "backups"],
        "dry_run": True,
    }
    plan["confirmation_token"] = gc_token(plan)
    plan["summary"] = f"Runtime GC found {len(candidates)} safe cleanup candidate(s)."
    return plan


def apply_gc(runtime_root: Path, confirm: str) -> Dict[str, object]:
    runtime_root = runtime_root.expanduser()
    plan = gc_plan(runtime_root)
    if confirm != plan["confirmation_token"]:
        raise ValueError("GC confirmation token did not match the plan.")
    deleted = []
    for item in plan["candidates"]:
        path = Path(str(item["path"]))
        if path.exists() and _inside_runtime(runtime_root, path):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(str(path))
    report = {
        "runtime_root": str(runtime_root),
        "deleted": deleted,
        "deleted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": f"Runtime GC deleted {len(deleted)} item(s).",
    }
    return report


def gc_token(plan: Dict[str, object]) -> str:
    encoded = json.dumps(
        {"runtime_root": plan["runtime_root"], "candidates": plan["candidates"]},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _protected_release_ids(app_root: Path) -> set[str]:
    release_ids = set()
    for filename in ("release.json", "active_release.json"):
        release_id = _release_id_from_path(app_root / filename)
        if release_id is not None:
            release_ids.add(release_id)
    return release_ids


def _release_id_from_path(release_path: Path) -> str | None:
    if not release_path.exists():
        return None
    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    release_id = payload.get("release_id")
    return release_id if isinstance(release_id, str) else None


def _inside_runtime(runtime_root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(runtime_root.resolve(strict=False))
    except ValueError:
        return False
    return True
