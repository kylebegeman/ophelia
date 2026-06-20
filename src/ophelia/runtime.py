from __future__ import annotations

import json
import shutil
import subprocess
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .addons import ensure_addons
from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest
from .templates import (
    bundle_env_file_path,
    bundle_mount_path,
    caddy_env_keys,
    render_caddy,
    render_compose,
    render_env_example,
)


class ApplyPhaseError(RuntimeError):
    def __init__(self, phase: str, message: str):
        super().__init__(message)
        self.phase = phase


@dataclass
class DeploymentRecord:
    app: str
    kind: str
    environment: str | None
    runtime_path: Path
    deployed_at: str
    source_manifest: str
    release_id: str | None = None
    active_release_id: str | None = None
    applied: bool | None = None
    verified: bool | None = None


HOST_ON_DEMAND_TLS_GLOBAL = Path("caddy") / "global.d" / "ophelia-on-demand-tls.caddy"


def render_bundle(manifest: Manifest) -> Dict[Path, str]:
    bundle: Dict[Path, str] = {
        Path("caddy") / f"{manifest.app}.caddy": render_caddy(manifest),
        Path("env.example"): render_env_example(manifest),
        Path("manifest.lock.json"): json.dumps(manifest.to_lock_dict(), indent=2, sort_keys=True) + "\n",
    }

    compose = render_compose(manifest)
    if compose is not None:
        bundle[Path("compose.yml")] = compose + "\n"

    return bundle


def write_bundle(bundle: Dict[Path, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in bundle.items():
        target = output_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def materialize_bundle(manifest: Manifest, manifest_path: Path, output_dir: Path) -> None:
    write_bundle(render_bundle(manifest), output_dir)
    sync_bundle_support_files(manifest, manifest_path, output_dir)


def sync_bundle_support_files(manifest: Manifest, manifest_path: Path, output_dir: Path) -> None:
    manifest_dir = manifest_path.parent

    for index, source in enumerate(manifest.env_files):
        _copy_support_path(
            _resolve_support_path(manifest_dir, source),
            output_dir / bundle_env_file_path(source, index=index),
        )

    for service in manifest.services.values():
        for index, source in enumerate(service.env_files):
            _copy_support_path(
                _resolve_support_path(manifest_dir, source),
                output_dir / bundle_env_file_path(source, service_name=service.name, index=index),
            )
        for index, mount in enumerate(service.mounts):
            if mount.bind:
                continue
            _copy_support_path(
                _resolve_support_path(manifest_dir, mount.source),
                output_dir / bundle_mount_path(service.name, mount.source, index),
            )


def bundle_support_paths(manifest: Manifest) -> set[Path]:
    paths: set[Path] = set()
    for index, source in enumerate(manifest.env_files):
        paths.add(Path(bundle_env_file_path(source, index=index)))
    for service in manifest.services.values():
        for index, source in enumerate(service.env_files):
            paths.add(Path(bundle_env_file_path(source, service_name=service.name, index=index)))
        for index, mount in enumerate(service.mounts):
            if not mount.bind:
                paths.add(Path(bundle_mount_path(service.name, mount.source, index)))
    return paths


def deploy_bundle(manifest: Manifest, manifest_path: Path, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Path:
    app_root = runtime_root / "apps" / manifest.app
    bundle = render_bundle(manifest)
    support_paths = bundle_support_paths(manifest)
    write_bundle(bundle, app_root)
    remove_stale_generated_files(app_root, bundle)
    if manifest_path.suffix != ".json":
        sync_bundle_support_files(manifest, manifest_path, app_root)
    copy_staged_support_files(app_root, support_paths, app_root)
    remove_stale_support_files(
        app_root,
        preserve_paths=support_paths | active_support_paths(runtime_root, manifest.app),
    )

    env_path = app_root / "env"
    env_example_path = app_root / "env.example"
    if not env_path.exists():
        env_path.write_text(env_example_path.read_text())

    previous_release_id = current_release_id(runtime_root, manifest.app)
    deployed_at = _utc_now()
    release_id = _release_id(deployed_at, bundle)
    release = {
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "kind": manifest.kind,
        "release_id": release_id,
        "previous_release_id": previous_release_id,
        "active_release_id_before": previous_release_id,
        "source_manifest": str(manifest_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_hash": _sha256_bytes(manifest_path.read_bytes()) if manifest_path.exists() else None,
        "rendered_bundle_hash": bundle_hash(bundle),
        "git_sha": _git_sha(),
        "images": image_references(manifest),
        "image_digests": image_digests(manifest),
        "generated_files": [str(path) for path in sorted(bundle)],
        "bundle_path": str((app_root / "release-bundles" / release_id).resolve()),
        "runtime_path": str(app_root.resolve()),
        "deployed_at": deployed_at,
        "deployed_by": _deployed_by(),
        "source": _deploy_source(),
        "applied": False,
        "verified": None,
        "apply": {"status": "staged", "ok": None, "applied": False, "phase": "bundle_rendered", "phases": []},
        "verification": {"status": "not_run", "ok": None, "results": []},
    }
    releases_root = app_root / "releases"
    releases_root.mkdir(parents=True, exist_ok=True)
    release_bundle_root = app_root / "release-bundles" / release_id
    write_bundle(bundle, release_bundle_root)
    copy_staged_support_files(app_root, support_paths, release_bundle_root)
    (releases_root / f"{release_id}.json").write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n"
    )
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    return app_root


def remove_stale_generated_files(app_root: Path, desired_bundle: Dict[Path, str]) -> List[Path]:
    desired_paths = set(desired_bundle)
    removed: List[Path] = []
    for relative_path in _current_generated_files(app_root):
        if relative_path in desired_paths:
            continue
        target = app_root / relative_path
        if target.exists() and target.is_file():
            target.unlink()
            removed.append(relative_path)
    _prune_empty_dirs(app_root / "caddy", stop=app_root)
    return removed


def copy_staged_support_files(source_root: Path, support_paths: set[Path], output_dir: Path) -> None:
    for relative_path in sorted(support_paths):
        source = source_root / relative_path
        if not source.exists():
            raise FileNotFoundError(f"Support file missing from staged bundle: {source}")
        _copy_support_path(source, output_dir / relative_path)


def active_support_paths(runtime_root: Path, app: str) -> set[Path]:
    release = active_release(runtime_root, app)
    bundle_root = _release_bundle_root(runtime_root / "apps" / app, release) if release else None
    if bundle_root is not None and bundle_root.exists():
        return _support_files_under(bundle_root)
    return set()


def remove_stale_support_files(app_root: Path, preserve_paths: set[Path]) -> List[Path]:
    removed: List[Path] = []
    for relative_path in sorted(_support_files_under(app_root)):
        if _preserves_support_path(relative_path, preserve_paths):
            continue
        target = app_root / relative_path
        if target.exists() and target.is_file():
            target.unlink()
            removed.append(relative_path)
    _prune_empty_dirs(app_root / "env.d", stop=app_root)
    _prune_empty_dirs(app_root / "artifacts", stop=app_root)
    return removed


def cleanup_inactive_support_files(runtime_root: Path, app: str) -> List[Path]:
    app_root = runtime_root / "apps" / app
    release = active_release(runtime_root, app)
    preserve_paths = active_support_paths(runtime_root, app)
    if release:
        bundle_root = _release_bundle_root(app_root, release)
        manifest = _load_manifest_from_bundle(bundle_root)
        if manifest is not None:
            preserve_paths |= bundle_support_paths(manifest)
    return remove_stale_support_files(app_root, preserve_paths)


def apply_local_bundle(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path | None = None,
) -> Path:
    app_root = deploy_bundle(manifest, manifest_path, runtime_root)
    phases: List[Dict[str, str]] = []

    def mark_phase(phase: str, status: str) -> None:
        phases.append({"phase": phase, "status": status, "at": _utc_now()})

    try:
        shared_compose = None
        shared_env = None
        if ophelia_root is not None:
            shared_compose = ophelia_root / "platform" / "shared" / "compose.yml"
            shared_env = ophelia_root / "platform" / "shared" / ".env"
            if manifest.addons.postgres or manifest.addons.redis:
                mark_phase("addon_provision", "running")
                ensure_addons(manifest, app_root, ophelia_root)
                mark_phase("addon_provision", "ok")

        mark_phase("env_validation", "running")
        placeholder_keys = placeholder_env_keys(app_root / "env")
        if placeholder_keys:
            joined = ", ".join(placeholder_keys)
            raise ApplyPhaseError(
                "env_validation",
                f"Refusing to apply with placeholder env values in {app_root / 'env'}: {joined}",
            )
        mark_phase("env_validation", "ok")

        mark_phase("caddy_env_sync", "running")
        try:
            caddy_env_changed = sync_caddy_env(manifest, app_root, runtime_root)
        except RuntimeError as exc:
            raise ApplyPhaseError("caddy_env_sync", str(exc)) from exc
        mark_phase("caddy_env_sync", "ok")

        mark_phase("caddy_stage", "running")
        caddy_source = app_root / "caddy" / f"{manifest.app}.caddy"
        caddy_target = runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy"
        caddy_target.parent.mkdir(parents=True, exist_ok=True)
        caddy_target.write_text(caddy_source.read_text())
        mark_phase("caddy_stage", "ok")

        if manifest.kind in {"service", "multi-service"}:
            compose_path = app_root / "compose.yml"
            if compose_path.exists():
                # Image pulls are release-critical. Continuing after a failed pull can
                # leave a host serving a stale local tag while the deploy appears done.
                mark_phase("image_pull", "running")
                _run_apply_phase(["docker", "compose", "-f", str(compose_path), "pull"], "image_pull")
                mark_phase("image_pull", "ok")

                mark_phase("container_health", "running")
                _run_compose_up(compose_path)
                mark_phase("container_health", "ok")

        mark_phase("caddy_global_sync", "running")
        try:
            sync_caddy_global_config(runtime_root, candidate_manifest=manifest)
        except RuntimeError as exc:
            raise ApplyPhaseError("caddy_global_sync", str(exc)) from exc
        mark_phase("caddy_global_sync", "ok")

        if shared_compose is not None and shared_compose.exists():
            compose_args = ["docker", "compose"]
            if shared_env is not None and shared_env.exists():
                compose_args.extend(["--env-file", str(shared_env)])
            compose_args.extend(["-f", str(shared_compose)])

            result = _run(
                [*compose_args, "ps", "--status", "running", "caddy"],
                capture_output=True,
                allow_failure=True,
                env=_runtime_env(runtime_root),
            )
            if result and "caddy" in result.stdout:
                if caddy_env_changed:
                    mark_phase("caddy_validate", "running")
                    _run_apply_phase(
                        [
                            *compose_args,
                            "exec",
                            "-T",
                            "caddy",
                            "caddy",
                            "validate",
                            "--config",
                            "/etc/caddy/Caddyfile",
                            "--envfile",
                            "/etc/caddy/env",
                        ],
                        "caddy_validate",
                        env=_runtime_env(runtime_root),
                    )
                    mark_phase("caddy_validate", "ok")

                    mark_phase("caddy_reload", "running")
                    _run_apply_phase(
                        [
                            *compose_args,
                            "--profile",
                            "edge",
                            "up",
                            "-d",
                            "--force-recreate",
                            "caddy",
                        ],
                        "caddy_reload",
                        env=_runtime_env(runtime_root),
                    )
                    mark_phase("caddy_reload", "ok")
                else:
                    mark_phase("caddy_validate", "running")
                    _run_apply_phase(
                        [
                            *compose_args,
                            "exec",
                            "-T",
                            "caddy",
                            "caddy",
                            "validate",
                            "--config",
                            "/etc/caddy/Caddyfile",
                            "--envfile",
                            "/etc/caddy/env",
                        ],
                        "caddy_validate",
                        env=_runtime_env(runtime_root),
                    )
                    mark_phase("caddy_validate", "ok")

                    mark_phase("caddy_reload", "running")
                    _run_apply_phase(
                        [
                            *compose_args,
                            "exec",
                            "-T",
                            "caddy",
                            "caddy",
                            "reload",
                            "--config",
                            "/etc/caddy/Caddyfile",
                        ],
                        "caddy_reload",
                        env=_runtime_env(runtime_root),
                    )
                    mark_phase("caddy_reload", "ok")

        update_current_release_apply(
            runtime_root,
            manifest.app,
            {"status": "applied", "ok": True, "applied": True, "phase": "complete", "phases": phases},
        )
        cleanup_inactive_support_files(runtime_root, manifest.app)
        return app_root
    except Exception as exc:
        phase = getattr(exc, "phase", phases[-1]["phase"] if phases else "apply")
        phases.append({"phase": phase, "status": "failed", "at": _utc_now(), "error": str(exc)})
        update_current_release_apply(
            runtime_root,
            manifest.app,
            {"status": "failed", "ok": False, "applied": False, "phase": phase, "phases": phases, "error": str(exc)},
        )
        raise


def sync_caddy_env(manifest: Manifest, app_root: Path, runtime_root: Path) -> bool:
    keys = caddy_env_keys(manifest)
    caddy_env_path = runtime_root / "caddy" / "env"
    caddy_env_path.parent.mkdir(parents=True, exist_ok=True)

    if caddy_env_path.exists() and not caddy_env_path.is_file():
        raise RuntimeError(f"Expected Caddy env path to be a file: {caddy_env_path}")
    if not caddy_env_path.exists():
        caddy_env_path.write_text("")

    if not keys:
        return False

    app_env = _load_env_file(app_root / "env")
    updates: Dict[str, str] = {}
    missing: List[str] = []
    for key in keys:
        value = app_env.get(key, "").strip()
        if not value or value == "replace-me":
            missing.append(key)
        else:
            updates[key] = value

    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Caddy on-demand TLS requires {joined} in {app_root / 'env'} before apply."
        )

    existing = _load_env_file(caddy_env_path)
    changed = any(existing.get(key) != value for key, value in updates.items())
    if changed:
        _update_key_value_file(caddy_env_path, updates)
    return changed


def sync_caddy_global_config(runtime_root: Path, candidate_manifest: Manifest | None = None) -> bool:
    global_path = runtime_root / HOST_ON_DEMAND_TLS_GLOBAL
    desired = render_host_caddy_global(runtime_root, candidate_manifest=candidate_manifest)
    changed = False
    if desired is None:
        if global_path.exists():
            global_path.unlink()
            changed = True
    else:
        global_path.parent.mkdir(parents=True, exist_ok=True)
        if not global_path.exists() or global_path.read_text() != desired:
            global_path.write_text(desired)
            changed = True
    return remove_legacy_caddy_global_files(runtime_root) or changed


def render_host_caddy_global(runtime_root: Path, candidate_manifest: Manifest | None = None) -> str | None:
    manifests = _active_manifests(runtime_root, exclude_app=candidate_manifest.app if candidate_manifest else None)
    if candidate_manifest is not None:
        manifests.append(candidate_manifest)
    ask_urls = sorted({manifest.edge.on_demand_tls.ask for manifest in manifests if manifest.edge.on_demand_tls is not None})
    if not ask_urls:
        return None
    if len(ask_urls) > 1:
        joined = ", ".join(ask_urls)
        raise RuntimeError(f"Caddy on-demand TLS requires one shared ask endpoint across active apps. Found: {joined}")
    return "\n".join(["on_demand_tls {", f"    ask {ask_urls[0]}", "}", ""])


def remove_legacy_caddy_global_files(runtime_root: Path) -> bool:
    changed = False
    global_dir = runtime_root / "caddy" / "global.d"
    apps_root = runtime_root / "apps"
    if not global_dir.exists() or not apps_root.exists():
        return False
    for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
        legacy = global_dir / f"{app_root.name}.caddy"
        if legacy.exists():
            legacy.unlink()
            changed = True
    return changed


def list_deployments(runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> List[DeploymentRecord]:
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return []

    deployments: List[DeploymentRecord] = []
    for child in sorted(apps_root.iterdir()):
        release_path = child / "release.json"
        if not release_path.exists():
            continue

        payload = json.loads(release_path.read_text())
        active = active_release(runtime_root, payload["app"])
        deployments.append(
            DeploymentRecord(
                app=payload["app"],
                kind=payload["kind"],
                environment=payload.get("environment"),
                runtime_path=Path(payload["runtime_path"]),
                deployed_at=payload["deployed_at"],
                source_manifest=payload["source_manifest"],
                release_id=payload.get("release_id"),
                active_release_id=active.get("release_id") if active else None,
                applied=payload.get("applied") if isinstance(payload.get("applied"), bool) else None,
                verified=payload.get("verified") if isinstance(payload.get("verified"), bool) else None,
            )
        )
    return deployments


def current_release_id(runtime_root: Path, app: str) -> str | None:
    return active_release_id(runtime_root, app) or latest_release_id(runtime_root, app)


def latest_release_id(runtime_root: Path, app: str) -> str | None:
    release_path = runtime_root / "apps" / app / "release.json"
    if not release_path.exists():
        return None
    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return None
    release_id = payload.get("release_id")
    return release_id if isinstance(release_id, str) and release_id else None


def active_release_id(runtime_root: Path, app: str) -> str | None:
    release = active_release(runtime_root, app)
    release_id = release.get("release_id") if release else None
    return release_id if isinstance(release_id, str) and release_id else None


def active_release(runtime_root: Path, app: str) -> Dict[str, object]:
    app_root = runtime_root / "apps" / app
    active_path = app_root / "active_release.json"
    if active_path.exists():
        try:
            payload = json.loads(active_path.read_text())
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload:
            return payload

    legacy_path = app_root / "release.json"
    if legacy_path.exists():
        try:
            payload = json.loads(legacy_path.read_text())
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("applied") is True:
            return payload
    return {}


def list_releases(runtime_root: Path, app: str) -> List[Dict[str, object]]:
    releases_root = runtime_root / "apps" / app / "releases"
    records: List[Dict[str, object]] = []

    if releases_root.exists():
        for release_path in sorted(releases_root.glob("*.json")):
            try:
                payload = json.loads(release_path.read_text())
            except json.JSONDecodeError:
                continue
            payload.setdefault("release_id", release_path.stem)
            records.append(payload)

    if not records:
        legacy_path = runtime_root / "apps" / app / "release.json"
        if legacy_path.exists():
            try:
                payload = json.loads(legacy_path.read_text())
            except json.JSONDecodeError:
                payload = {}
            if payload:
                payload.setdefault("release_id", "legacy-current")
                records.append(payload)

    active_id = active_release_id(runtime_root, app)
    latest_id = latest_release_id(runtime_root, app)
    for record in records:
        release_id = record.get("release_id")
        record["active"] = bool(active_id and release_id == active_id)
        record["latest"] = bool(latest_id and release_id == latest_id)
    return sorted(records, key=lambda item: str(item.get("deployed_at", "")))


def load_release(runtime_root: Path, app: str, release_id: str) -> Dict[str, object]:
    active_path = runtime_root / "apps" / app / "active_release.json"
    if release_id == "active" and active_path.exists():
        return json.loads(active_path.read_text())

    release_path = runtime_root / "apps" / app / "releases" / f"{release_id}.json"
    if release_path.exists():
        return json.loads(release_path.read_text())

    legacy_path = runtime_root / "apps" / app / "release.json"
    if release_id == "current" and legacy_path.exists():
        return json.loads(legacy_path.read_text())
    if release_id == "legacy-current" and legacy_path.exists():
        payload = json.loads(legacy_path.read_text())
        payload.setdefault("release_id", "legacy-current")
        return payload

    raise FileNotFoundError(f"Release not found: {app}/{release_id}")


def update_current_release_verification(runtime_root: Path, app: str, verification: Dict[str, object]) -> None:
    verification.setdefault("verified", bool(verification.get("ok")))
    verification.setdefault("status", "passed" if verification.get("ok") else "failed")
    updates = {"verification": verification, "verified": bool(verification.get("ok"))}
    payload = _update_current_release(runtime_root, app, updates)
    if payload and payload.get("release_id") == active_release_id(runtime_root, app):
        _write_active_release(runtime_root, app, payload)


def update_current_release_apply(runtime_root: Path, app: str, apply_result: Dict[str, object]) -> None:
    updates = {
        "apply": apply_result,
        "applied": bool(apply_result.get("applied")),
    }
    payload = _update_current_release(runtime_root, app, updates)
    if payload and apply_result.get("applied") is True:
        activate_release(runtime_root, app, payload)


def activate_release(runtime_root: Path, app: str, release: Dict[str, object]) -> None:
    payload = dict(release)
    payload["active"] = True
    payload["activated_at"] = _utc_now()
    _write_active_release(runtime_root, app, payload)


def _write_active_release(runtime_root: Path, app: str, release: Dict[str, object]) -> None:
    app_root = runtime_root / "apps" / app
    active_path = app_root / "active_release.json"
    active_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    release_id = release.get("release_id")
    if isinstance(release_id, str):
        historical_path = app_root / "releases" / f"{release_id}.json"
        if historical_path.exists():
            historical = json.loads(historical_path.read_text())
            historical.update(release)
            historical_path.write_text(json.dumps(historical, indent=2, sort_keys=True) + "\n")
    latest_path = app_root / "release.json"
    if latest_path.exists():
        latest = json.loads(latest_path.read_text())
        if latest.get("release_id") == release_id:
            latest.update(release)
            latest_path.write_text(json.dumps(latest, indent=2, sort_keys=True) + "\n")


def _update_current_release(runtime_root: Path, app: str, updates: Dict[str, object]) -> Dict[str, object] | None:
    app_root = runtime_root / "apps" / app
    release_path = app_root / "release.json"
    if not release_path.exists():
        return None

    payload = json.loads(release_path.read_text())
    payload.update(updates)
    release_id = payload.get("release_id")
    if isinstance(release_id, str):
        historical_path = app_root / "releases" / f"{release_id}.json"
        if historical_path.exists():
            historical_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    release_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def bundle_hash(bundle: Dict[Path, str]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for relative_path in sorted(bundle):
        digest.update(str(relative_path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(bundle[relative_path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def image_references(manifest: Manifest) -> Dict[str, str]:
    images: Dict[str, str] = {}
    if manifest.image:
        images["default"] = manifest.image
    for service_name, service in sorted(manifest.services.items()):
        image = service.image or manifest.image
        if image:
            images[service_name] = image
    return images


def image_digests(manifest: Manifest) -> Dict[str, str]:
    digests: Dict[str, str] = {}
    for name, image in image_references(manifest).items():
        if "@sha256:" in image:
            digests[name] = image.split("@", 1)[1]
    return digests


def _current_generated_files(app_root: Path) -> set[Path]:
    if not app_root.exists():
        return set()
    files = {Path("compose.yml"), Path("env.example"), Path("manifest.lock.json")}
    caddy_root = app_root / "caddy"
    if caddy_root.exists():
        for caddy_file in caddy_root.rglob("*.caddy"):
            files.add(caddy_file.relative_to(app_root))
    return {path for path in files if (app_root / path).exists()}


def _support_files_under(root: Path) -> set[Path]:
    paths: set[Path] = set()
    for dirname in ("env.d", "artifacts"):
        directory = root / dirname
        if directory.exists():
            paths.update(path.relative_to(root) for path in directory.rglob("*") if path.is_file())
    return paths


def _active_manifests(runtime_root: Path, exclude_app: str | None = None) -> List[Manifest]:
    manifests: List[Manifest] = []
    apps_root = runtime_root / "apps"
    if not apps_root.exists():
        return manifests
    for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
        if app_root.name == exclude_app:
            continue
        release = active_release(runtime_root, app_root.name)
        manifest = _load_manifest_from_bundle(_release_bundle_root(app_root, release)) if release else None
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _release_bundle_root(app_root: Path, release: Dict[str, object]) -> Path:
    bundle_path = release.get("bundle_path")
    if isinstance(bundle_path, str) and bundle_path:
        return Path(bundle_path)
    release_id = str(release.get("release_id", ""))
    return app_root / "release-bundles" / release_id


def _load_manifest_from_bundle(bundle_root: Path) -> Manifest | None:
    lock_path = bundle_root / "manifest.lock.json"
    if not lock_path.exists():
        return None
    try:
        from .manifest import load_manifest

        return load_manifest(lock_path)
    except Exception:
        return None


def _prune_empty_dirs(path: Path, stop: Path) -> None:
    current = path
    while current != stop and current.exists() and current.is_dir():
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def placeholder_env_keys(path: Path) -> List[str]:
    keys: List[str] = []
    for key, value in _load_env_file(path).items():
        if _is_placeholder_value(value):
            keys.append(key)
    return sorted(keys)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _release_id(deployed_at: str, bundle: Dict[Path, str]) -> str:
    try:
        stamp = datetime.fromisoformat(deployed_at).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    except ValueError:
        stamp = deployed_at.replace("-", "").replace(":", "").replace("+", "").replace(" ", "T")
    unique = _sha256_bytes(f"{deployed_at}:{bundle_hash(bundle)}:{time.time_ns()}".encode("utf-8"))[:12]
    return f"{stamp}-{unique}"


def _sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _git_sha() -> str | None:
    from .config import REPO_ROOT

    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _deployed_by() -> str:
    return os.environ.get("OPHELIA_DEPLOYED_BY") or os.environ.get("GITHUB_ACTOR") or os.environ.get("USER") or "unknown"


def _deploy_source() -> str:
    return os.environ.get("OPHELIA_DEPLOY_SOURCE") or ("github-actions" if os.environ.get("GITHUB_ACTIONS") else "local")


def _is_placeholder_value(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"", "replace-me", "changeme", "todo"} or "replace-me" in lowered


def _run(
    command: List[str],
    capture_output: bool = False,
    allow_failure: bool = False,
    env: Dict[str, str] | None = None,
):
    import subprocess

    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture_output,
            env=env,
        )
    except subprocess.CalledProcessError:
        if allow_failure:
            return None
        raise


def _run_compose_up(compose_path: Path):
    command = ["docker", "compose", "-f", str(compose_path), "up", "-d"]
    try:
        return _run_apply_phase([*command, "--wait"], "container_health", capture_output=True)
    except ApplyPhaseError as exc:
        if _compose_wait_unsupported(str(exc)):
            return _run_apply_phase(command, "container_health")
        raise


def _compose_wait_unsupported(message: str) -> bool:
    lowered = message.lower()
    return "--wait" in lowered and ("unknown" in lowered or "no such option" in lowered)


def _runtime_env(runtime_root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    env["OPHELIA_RUNTIME_ROOT"] = str(runtime_root.expanduser())
    return env


def _run_apply_phase(
    command: List[str],
    phase: str,
    capture_output: bool = False,
    env: Dict[str, str] | None = None,
):
    try:
        return _run(command, capture_output=capture_output, env=env)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or str(exc)
        raise ApplyPhaseError(phase, detail) from exc


def _resolve_support_path(manifest_dir: Path, source: str) -> Path:
    raw = Path(source).expanduser()
    return raw if raw.is_absolute() else (manifest_dir / raw)


def _copy_support_path(source: Path, destination: Path) -> None:
    if source.resolve(strict=False) == destination.resolve(strict=False):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return
    shutil.copy2(source, destination)


def _preserves_support_path(relative_path: Path, preserve_paths: set[Path]) -> bool:
    if relative_path in preserve_paths:
        return True
    return any(parent in preserve_paths for parent in relative_path.parents)


def _load_env_file(path: Path) -> Dict[str, str]:
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


def _update_key_value_file(path: Path, updates: Dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    rendered: List[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered.append(line)
            continue

        key, _ = line.split("=", 1)
        if key in remaining:
            rendered.append(f"{key}={remaining.pop(key)}")
        else:
            rendered.append(line)

    for key, value in remaining.items():
        rendered.append(f"{key}={value}")

    path.write_text("\n".join(rendered) + "\n")
