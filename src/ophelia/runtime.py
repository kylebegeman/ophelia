from __future__ import annotations

import json
import shutil
import subprocess
import os
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
    render_caddy_global,
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


def render_bundle(manifest: Manifest) -> Dict[Path, str]:
    bundle: Dict[Path, str] = {
        Path("caddy") / f"{manifest.app}.caddy": render_caddy(manifest),
        Path("env.example"): render_env_example(manifest),
        Path("manifest.lock.json"): json.dumps(manifest.to_lock_dict(), indent=2, sort_keys=True) + "\n",
    }

    caddy_global = render_caddy_global(manifest)
    if caddy_global is not None:
        bundle[Path("caddy") / "global.d" / f"{manifest.app}.caddy"] = caddy_global

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


def deploy_bundle(manifest: Manifest, manifest_path: Path, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Path:
    app_root = runtime_root / "apps" / manifest.app
    bundle = render_bundle(manifest)
    write_bundle(bundle, app_root)
    sync_bundle_support_files(manifest, manifest_path, app_root)

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
    write_bundle(bundle, app_root / "release-bundles" / release_id)
    (releases_root / f"{release_id}.json").write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n"
    )
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    return app_root


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

        caddy_global_source = app_root / "caddy" / "global.d" / f"{manifest.app}.caddy"
        caddy_global_target = runtime_root / "caddy" / "global.d" / f"{manifest.app}.caddy"
        caddy_global_target.parent.mkdir(parents=True, exist_ok=True)
        if caddy_global_source.exists():
            caddy_global_target.write_text(caddy_global_source.read_text())
        elif caddy_global_target.exists():
            caddy_global_target.unlink()
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

        if shared_compose is not None and shared_compose.exists():
            compose_args = ["docker", "compose"]
            if shared_env is not None and shared_env.exists():
                compose_args.extend(["--env-file", str(shared_env)])
            compose_args.extend(["-f", str(shared_compose)])

            result = _run(
                [*compose_args, "ps", "--status", "running", "caddy"],
                capture_output=True,
                allow_failure=True,
            )
            if result and "caddy" in result.stdout:
                if caddy_env_changed:
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
                        ],
                        "caddy_validate",
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
                    )
                    mark_phase("caddy_reload", "ok")

        update_current_release_apply(
            runtime_root,
            manifest.app,
            {"status": "applied", "ok": True, "applied": True, "phase": "complete", "phases": phases},
        )
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
        deployments.append(
            DeploymentRecord(
                app=payload["app"],
                kind=payload["kind"],
                environment=payload.get("environment"),
                runtime_path=Path(payload["runtime_path"]),
                deployed_at=payload["deployed_at"],
                source_manifest=payload["source_manifest"],
                release_id=payload.get("release_id"),
            )
        )
    return deployments


def current_release_id(runtime_root: Path, app: str) -> str | None:
    release_path = runtime_root / "apps" / app / "release.json"
    if not release_path.exists():
        return None
    try:
        payload = json.loads(release_path.read_text())
    except json.JSONDecodeError:
        return None
    release_id = payload.get("release_id")
    return release_id if isinstance(release_id, str) and release_id else None


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

    return sorted(records, key=lambda item: str(item.get("deployed_at", "")))


def load_release(runtime_root: Path, app: str, release_id: str) -> Dict[str, object]:
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
    _update_current_release(runtime_root, app, updates)


def update_current_release_apply(runtime_root: Path, app: str, apply_result: Dict[str, object]) -> None:
    updates = {
        "apply": apply_result,
        "applied": bool(apply_result.get("applied")),
    }
    _update_current_release(runtime_root, app, updates)


def _update_current_release(runtime_root: Path, app: str, updates: Dict[str, object]) -> None:
    app_root = runtime_root / "apps" / app
    release_path = app_root / "release.json"
    if not release_path.exists():
        return

    payload = json.loads(release_path.read_text())
    payload.update(updates)
    release_id = payload.get("release_id")
    if isinstance(release_id, str):
        historical_path = app_root / "releases" / f"{release_id}.json"
        if historical_path.exists():
            historical_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    release_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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


def placeholder_env_keys(path: Path) -> List[str]:
    keys: List[str] = []
    for key, value in _load_env_file(path).items():
        if _is_placeholder_value(value):
            keys.append(key)
    return sorted(keys)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _release_id(deployed_at: str, bundle: Dict[Path, str]) -> str:
    try:
        stamp = datetime.fromisoformat(deployed_at).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        stamp = deployed_at.replace("-", "").replace(":", "").replace("+", "").replace(" ", "T")
    return f"{stamp}-{bundle_hash(bundle)[:12]}"


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


def _run(command: List[str], capture_output: bool = False, allow_failure: bool = False):
    import subprocess

    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture_output,
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


def _run_apply_phase(command: List[str], phase: str, capture_output: bool = False):
    try:
        return _run(command, capture_output=capture_output)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or str(exc)
        raise ApplyPhaseError(phase, detail) from exc


def _resolve_support_path(manifest_dir: Path, source: str) -> Path:
    raw = Path(source).expanduser()
    return raw if raw.is_absolute() else (manifest_dir / raw)


def _copy_support_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return
    shutil.copy2(source, destination)


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
