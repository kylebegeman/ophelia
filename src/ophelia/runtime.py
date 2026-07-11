from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .addons import ensure_addons
from .caddy_manager import CADDY_ENVFILE_RELOAD_SCRIPT
from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest, ManifestError
from .path_safety import assert_no_external_symlinks
from .templates import (
    bundle_env_file_path,
    bundle_mount_path,
    caddy_env_keys,
    render_caddy,
    render_compose,
    render_env_example,
    static_caddy_root,
    static_runtime_app_name,
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


@dataclass(frozen=True)
class DeployMetadata:
    release_id: str | None = None
    commit_sha: str | None = None
    build_time: str | None = None
    locked: bool = False


HOST_ON_DEMAND_TLS_GLOBAL = Path("caddy") / "global.d" / "ophelia-on-demand-tls.caddy"


def render_bundle(manifest: Manifest, release_metadata: Dict[str, Any] | None = None) -> Dict[Path, str]:
    bundle: Dict[Path, str] = {
        Path("caddy") / f"{manifest.app}.caddy": render_caddy(manifest),
        Path("env.example"): render_env_example(manifest),
        Path("manifest.lock.json"): json.dumps(manifest.to_lock_dict(), indent=2, sort_keys=True) + "\n",
    }

    compose = render_compose(manifest, release_metadata=release_metadata)
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
    sync_bundle_static_source(manifest, manifest_path, output_dir)


def sync_bundle_support_files(manifest: Manifest, manifest_path: Path, output_dir: Path) -> None:
    manifest_dir = manifest_path.parent

    for source, target, required in _bundle_support_file_map(manifest):
        resolved = _support_path_candidate(manifest_dir, source)
        if not resolved.exists() and not required:
            continue
        _copy_support_path(
            resolved,
            output_dir / target,
            allowed_root=manifest_dir,
        )


def bundle_support_paths(manifest: Manifest, *, required_only: bool = False) -> set[Path]:
    return {target for _, target, required in _bundle_support_file_map(manifest) if required or not required_only}


def sync_bundle_static_source(manifest: Manifest, manifest_path: Path, output_dir: Path) -> None:
    relative_path = static_source_bundle_path(manifest)
    if relative_path is None:
        return

    manifest_dir = manifest_path.parent.resolve(strict=False)
    source = manifest_dir / relative_path
    if not source.exists():
        raise FileNotFoundError(f"Static asset source not found: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Static asset source must be a directory: {source}")
    _copy_static_source_tree(source, output_dir / relative_path)


def copy_staged_static_source(manifest: Manifest, source_root: Path, output_dir: Path) -> None:
    relative_path = static_source_bundle_path(manifest)
    if relative_path is None:
        return
    _copy_static_source_tree(source_root / relative_path, output_dir / relative_path)


def static_source_bundle_path(manifest: Manifest) -> Path | None:
    if manifest.kind != "static" or not manifest.static_root:
        return None
    path = Path(manifest.static_root)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _bundle_support_file_map(manifest: Manifest) -> List[tuple[str, Path, bool]]:
    files: List[tuple[str, Path, bool]] = []
    seen_targets: set[Path] = set()

    def add(source: str, target: Path, *, required: bool) -> None:
        if target in seen_targets:
            return
        seen_targets.add(target)
        files.append((source, target, required))

    for index, source in enumerate(manifest.env_files):
        add(source, Path(bundle_env_file_path(source, index=index)), required=True)

    for service in manifest.services.values():
        for index, source in enumerate(service.env_files):
            add(source, Path(bundle_env_file_path(source, service_name=service.name, index=index)), required=True)
        for index, mount in enumerate(service.mounts):
            if not mount.bind:
                add(mount.source, Path(bundle_mount_path(service.name, mount.source, index)), required=True)

    for source in _manifest_relative_hook_paths(manifest):
        add(source, Path(source), required=True)
    for source in _manifest_relative_data_command_paths(manifest):
        add(source, Path(source), required=False)
    for source in _manifest_relative_verify_command_paths(manifest):
        add(source, Path(source), required=False)

    return files


def _manifest_relative_hook_paths(manifest: Manifest) -> List[str]:
    paths: List[str] = []
    for source in (
        manifest.hooks.pre_export,
        manifest.hooks.freeze,
        manifest.hooks.unfreeze,
        manifest.hooks.post_import,
        manifest.hooks.post_cutover,
    ):
        if source and _is_relative_support_path(source):
            paths.append(source)
    return paths


def _manifest_relative_data_command_paths(manifest: Manifest) -> List[str]:
    paths: List[str] = []
    for service in (manifest.data.postgres, manifest.data.redis):
        if service is None:
            continue
        for config in (service.export, service.import_config, service.verify):
            path = _command_support_path(config.get("command") if isinstance(config, dict) else None)
            if path:
                paths.append(path)

    for volume in manifest.data.volumes:
        for config in (volume.export, volume.import_config, volume.verify):
            path = _command_support_path(config.get("command") if isinstance(config, dict) else None)
            if path:
                paths.append(path)

    return paths


def _manifest_relative_verify_command_paths(manifest: Manifest) -> List[str]:
    paths: List[str] = []
    for check in manifest.verify:
        command = check.command or []
        if not command:
            continue
        candidate = command[0]
        if _is_relative_support_path(candidate):
            paths.append(candidate)
    return paths


def _command_support_path(command: object) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    candidate = parts[0]
    return candidate if _is_relative_support_path(candidate) else None


def _is_relative_support_path(source: str) -> bool:
    if not source or source.startswith("-"):
        return False
    path = Path(source)
    if path.is_absolute():
        return False
    return "/" in source or source.startswith(".")


def deploy_bundle(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    deploy_metadata: DeployMetadata | None = None,
) -> Path:
    app_root = runtime_root / "apps" / manifest.app
    support_paths = bundle_support_paths(manifest)
    required_support_paths = bundle_support_paths(manifest, required_only=True)
    previous_release_id = current_release_id(runtime_root, manifest.app)
    deployed_at = _utc_now()
    base_bundle = render_bundle(manifest)
    generated_release_id = _release_id(deployed_at, base_bundle)
    resolved_metadata = _resolve_deploy_metadata(
        manifest,
        deployed_at=deployed_at,
        generated_release_id=generated_release_id,
        deploy_metadata=deploy_metadata,
    )
    release_id = resolved_metadata["release_id"]
    release_metadata = _release_metadata_env(
        manifest,
        release_id=release_id,
        commit_sha=resolved_metadata["commit_sha"],
        build_time=resolved_metadata["build_time"],
    )
    bundle = render_bundle(manifest, release_metadata=release_metadata)
    write_bundle(bundle, app_root)
    remove_stale_generated_files(app_root, bundle)
    if manifest_path.suffix != ".json":
        sync_bundle_support_files(manifest, manifest_path, app_root)
        sync_bundle_static_source(manifest, manifest_path, app_root)
    else:
        copy_staged_static_source(manifest, manifest_path.parent, app_root)
    copy_staged_support_files(app_root, support_paths, app_root, required_paths=required_support_paths)
    copy_staged_static_source(manifest, app_root, app_root)
    remove_stale_support_files(
        app_root,
        preserve_paths=support_paths | active_support_paths(runtime_root, manifest.app),
    )

    env_path = app_root / "env"
    env_example_path = app_root / "env.example"
    if not env_path.exists():
        env_path.write_text(env_example_path.read_text())

    static_assets = static_asset_plan(manifest, manifest_path, runtime_root)

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
        "commit_sha": resolved_metadata["commit_sha"],
        "build_time": resolved_metadata["build_time"],
        "git_sha": resolved_metadata["commit_sha"],
        "images": image_references(manifest),
        "image_digests": image_digests(manifest),
        "runtime_env": release_metadata,
        "static_assets": static_assets,
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
    copy_staged_support_files(app_root, support_paths, release_bundle_root, required_paths=required_support_paths)
    copy_staged_static_source(manifest, app_root, release_bundle_root)
    (releases_root / f"{release_id}.json").write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n"
    )
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    return app_root


def deploy_confirmed_candidate(
    manifest: Manifest,
    manifest_path: Path,
    candidate_root: Path,
    generated_files: List[Path],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    deploy_metadata: DeployMetadata | None = None,
    *,
    expected_candidate_digest: str,
    expected_bundle_hash: str,
    expected_baseline_digest: str,
) -> Path:
    """Materialize the exact bytes from a reviewed production candidate."""

    if deploy_metadata is None or not deploy_metadata.locked:
        raise ValueError("Confirmed candidate deployment requires locked deploy metadata.")
    if candidate_root.is_symlink():
        raise ValueError(f"Confirmed candidate root must not be a symlink: {candidate_root}")
    try:
        candidate_root = candidate_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Confirmed candidate root is unavailable: {candidate_root}") from exc
    if manifest_path.resolve(strict=False) != candidate_root / "manifest.lock.json":
        raise ValueError("Confirmed manifest must be the candidate manifest.lock.json.")

    from .execution.staging import tree_digest

    if tree_digest(candidate_root) != expected_candidate_digest:
        raise ValueError("Confirmed candidate changed after token validation.")
    if deployment_baseline_digest(manifest, runtime_root) != expected_baseline_digest:
        raise ValueError("Live deployment state changed after confirmation validation.")

    bundle = _confirmed_generated_bundle(candidate_root, generated_files)
    if bundle_hash(bundle) != expected_bundle_hash:
        raise ValueError("Confirmed generated bytes do not match the reviewed bundle hash.")
    required_generated = {
        Path("env.example"),
        Path("manifest.lock.json"),
        Path("caddy") / f"{manifest.app}.caddy",
    }
    if manifest.kind in {"service", "multi-service"}:
        required_generated.add(Path("compose.yml"))
    missing_generated = required_generated - set(bundle)
    if missing_generated:
        joined = ", ".join(str(path) for path in sorted(missing_generated))
        raise ValueError(f"Confirmed candidate is missing generated files: {joined}")

    support_paths = bundle_support_paths(manifest)
    required_support_paths = bundle_support_paths(manifest, required_only=True)
    _preflight_candidate_support(candidate_root, support_paths, required_support_paths)
    relative_static = static_source_bundle_path(manifest)
    if relative_static is not None:
        static_source = candidate_root / relative_static
        if not static_source.exists():
            raise FileNotFoundError(f"Static asset source not found: {static_source}")
        if not static_source.is_dir():
            raise NotADirectoryError(f"Static asset source must be a directory: {static_source}")
        _assert_static_source_safe(static_source)

    app_root = runtime_root / "apps" / manifest.app
    live_env = app_root / "env"
    if live_env.is_symlink():
        raise ApplyPhaseError("env_validation", f"Expected a regular env file: {live_env}")
    env_source = live_env if live_env.exists() else candidate_root / "env.example"
    if env_source.is_symlink() or not env_source.is_file():
        raise ApplyPhaseError("env_validation", f"Expected a regular env file: {env_source}")
    missing_required_keys = missing_required_env_keys(manifest, env_source)
    if missing_required_keys:
        joined = ", ".join(missing_required_keys)
        raise ApplyPhaseError(
            "env_validation",
            f"Refusing to apply with missing required env keys in {env_source}: {joined}",
        )
    placeholder_keys = placeholder_env_keys(env_source)
    if placeholder_keys:
        joined = ", ".join(placeholder_keys)
        raise ApplyPhaseError(
            "env_validation",
            f"Refusing to apply with placeholder env values in {env_source}: {joined}",
        )

    previous_release_id = current_release_id(runtime_root, manifest.app)
    previous_support_files = _declared_support_files(app_root) | _support_files_under(app_root)
    deployed_at = _utc_now()
    resolved_metadata = _resolve_deploy_metadata(
        manifest,
        deployed_at=deployed_at,
        generated_release_id="",
        deploy_metadata=deploy_metadata,
    )
    release_id = resolved_metadata["release_id"]
    release_bundle_root = app_root / "release-bundles" / release_id
    if release_bundle_root.exists() or release_bundle_root.is_symlink():
        raise ValueError(f"Confirmed release bundle already exists: {release_bundle_root}")
    release_metadata = _release_metadata_env(
        manifest,
        release_id=release_id,
        commit_sha=resolved_metadata["commit_sha"],
        build_time=resolved_metadata["build_time"],
    )
    static_assets = static_asset_plan(manifest, manifest_path, runtime_root)
    staged_support_files = _declared_support_files(candidate_root) | _support_files_under(candidate_root)

    app_root.mkdir(parents=True, exist_ok=True)
    copy_staged_support_files(
        candidate_root,
        set(bundle),
        app_root,
        required_paths=set(bundle),
    )
    remove_stale_generated_files(app_root, bundle)
    copy_staged_support_files(
        candidate_root,
        support_paths,
        app_root,
        required_paths=required_support_paths,
    )
    copy_staged_static_source(manifest, candidate_root, app_root)
    remove_stale_support_files(
        app_root,
        preserve_paths=support_paths | active_support_paths(runtime_root, manifest.app),
    )
    env_path = app_root / "env"
    if not env_path.exists():
        shutil.copy2(candidate_root / "env.example", env_path)

    release = {
        "app": manifest.app,
        "environment": getattr(manifest, "environment", None),
        "kind": manifest.kind,
        "release_id": release_id,
        "previous_release_id": previous_release_id,
        "active_release_id_before": previous_release_id,
        "source_manifest": str(manifest_path),
        "manifest_path": str(manifest_path),
        "manifest_hash": _sha256_bytes(manifest_path.read_bytes()),
        "rendered_bundle_hash": bundle_hash(bundle),
        "commit_sha": resolved_metadata["commit_sha"],
        "build_time": resolved_metadata["build_time"],
        "git_sha": resolved_metadata["commit_sha"],
        "images": image_references(manifest),
        "image_digests": image_digests(manifest),
        "runtime_env": release_metadata,
        "static_assets": static_assets,
        "generated_files": [str(path) for path in sorted(bundle)],
        "support_files": [str(path) for path in sorted(staged_support_files)],
        "superseded_support_files": [str(path) for path in sorted(previous_support_files)],
        "bundle_path": str(release_bundle_root.resolve()),
        "runtime_path": str(app_root.resolve()),
        "deployed_at": deployed_at,
        "deployed_by": _deployed_by(),
        "source": _deploy_source(),
        "applied": False,
        "verified": None,
        "apply": {
            "status": "staged",
            "ok": None,
            "applied": False,
            "phase": "confirmed_candidate_materialized",
            "phases": [],
        },
        "verification": {"status": "not_run", "ok": None, "results": []},
    }
    releases_root = app_root / "releases"
    releases_root.mkdir(parents=True, exist_ok=True)
    copy_staged_support_files(
        candidate_root,
        set(bundle),
        release_bundle_root,
        required_paths=set(bundle),
    )
    copy_staged_support_files(
        candidate_root,
        support_paths,
        release_bundle_root,
        required_paths=required_support_paths,
    )
    copy_staged_static_source(manifest, candidate_root, release_bundle_root)
    (releases_root / f"{release_id}.json").write_text(
        json.dumps(release, indent=2, sort_keys=True) + "\n"
    )
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    return app_root


def _confirmed_generated_bundle(candidate_root: Path, generated_files: List[Path]) -> Dict[Path, str]:
    bundle: Dict[Path, str] = {}
    for value in generated_files:
        relative_path = Path(value)
        if (
            relative_path.is_absolute()
            or relative_path == Path(".")
            or ".." in relative_path.parts
            or "\\" in str(relative_path)
        ):
            raise ValueError(f"Invalid confirmed generated path: {value}")
        source = candidate_root / relative_path
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(candidate_root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Confirmed generated path escapes its candidate: {value}") from exc
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Confirmed generated path is not a regular file: {value}")
        try:
            bundle[relative_path] = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"Confirmed generated file is unreadable: {value}") from exc
    if len(bundle) != len(generated_files):
        raise ValueError("Confirmed generated file list contains duplicates.")
    return bundle


def _preflight_candidate_support(
    candidate_root: Path,
    support_paths: set[Path],
    required_paths: set[Path],
) -> None:
    for relative_path in sorted(support_paths):
        source = candidate_root / relative_path
        if not source.exists():
            if relative_path in required_paths:
                raise FileNotFoundError(f"Support file missing from staged bundle: {source}")
            continue
        try:
            source.resolve(strict=True).relative_to(candidate_root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Staged support path escapes its candidate: {relative_path}") from exc
        assert_no_external_symlinks(source, candidate_root)


def static_asset_plan(manifest: Manifest, manifest_path: Path, runtime_root: Path) -> Dict[str, object]:
    if manifest.kind != "static" or not manifest.static_root:
        return {"managed": False, "mode": "none"}
    if Path(manifest.static_root).is_absolute():
        root = Path(manifest.static_root)
        return {
            "managed": False,
            "mode": "external",
            "serving_root": str(root),
            "source": str(root),
            "source_exists": root.exists(),
        }

    return _static_asset_plan_for_source(
        manifest,
        manifest_path.parent.resolve(strict=False) / manifest.static_root,
        runtime_root,
    )


def _static_asset_plan_for_source(manifest: Manifest, source: Path, runtime_root: Path) -> Dict[str, object]:
    current = static_runtime_root(runtime_root, manifest) / "current"
    symlink_issues = _static_symlink_issues(source) if source.exists() and source.is_dir() else []
    source_digest = _directory_digest(source) if source.exists() and source.is_dir() and not symlink_issues else None
    current_digest = _directory_digest(current) if current.exists() and current.is_dir() else None
    return {
        "managed": True,
        "mode": "sync",
        "source": str(source),
        "source_exists": source.exists(),
        "source_is_dir": source.is_dir(),
        "unsafe_symlinks": symlink_issues,
        "source_digest": source_digest,
        "serving_root": static_caddy_root(manifest),
        "runtime_current": str(current),
        "current_exists": current.exists(),
        "current_digest": current_digest,
        "change": (
            "blocked"
            if symlink_issues
            else "none"
            if source_digest is not None and source_digest == current_digest
            else "sync"
        ),
    }


def publish_static_assets(
    manifest: Manifest,
    source_root: Path,
    runtime_root: Path,
    release_id: str,
) -> Dict[str, object]:
    relative_path = static_source_bundle_path(manifest)
    if relative_path is None:
        manifest_path = source_root / "manifest.lock.json"
        return static_asset_plan(manifest, manifest_path, runtime_root)

    source = source_root / relative_path
    if not source.exists():
        raise FileNotFoundError(f"Static asset source not found: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Static asset source must be a directory: {source}")
    _assert_static_source_safe(source)

    plan = _static_asset_plan_for_source(manifest, source, runtime_root)
    root = static_runtime_root(runtime_root, manifest)
    release_root = root / "releases" / release_id
    if release_root.exists():
        shutil.rmtree(release_root)
    shutil.copytree(source, release_root, symlinks=True)

    current = root / "current"
    tmp_current = root / f".current.{release_id}.tmp"
    if tmp_current.exists() or tmp_current.is_symlink():
        if tmp_current.is_dir() and not tmp_current.is_symlink():
            shutil.rmtree(tmp_current)
        else:
            tmp_current.unlink()
    tmp_current.symlink_to(Path("releases") / release_id, target_is_directory=True)
    if current.exists() and not current.is_symlink():
        shutil.rmtree(current)
    tmp_current.replace(current)

    return {
        **plan,
        "release_root": str(release_root),
        "runtime_current": str(current),
        "source_digest": _directory_digest(source),
        "release_digest": _directory_digest(release_root),
        "file_count": _directory_file_count(release_root),
        "change": "synced",
    }


def static_runtime_root(runtime_root: Path, manifest: Manifest) -> Path:
    return runtime_root / "static" / static_runtime_app_name(manifest)


def deployment_baseline_digest(manifest: Manifest, runtime_root: Path) -> str:
    """Digest live app state that a reviewed deploy is allowed to replace.

    The mutable runtime ``env`` file is intentionally excluded so an operator can
    satisfy planned secret requirements before applying the reviewed candidate.
    """

    digest = hashlib.sha256()
    app_root = runtime_root / "apps" / manifest.app
    digest.update(b"app-root\0")
    paths = (
        _current_generated_files(app_root)
        | _support_files_under(app_root)
        | _declared_support_files(app_root)
        | {Path("release.json"), Path("active_release.json")}
    )
    for relative_path in sorted(paths):
        _update_baseline_digest(digest, relative_path.as_posix(), app_root / relative_path)
    _update_baseline_digest(
        digest,
        "runtime-caddy-site",
        runtime_root / "caddy" / "sites.d" / f"{manifest.app}.caddy",
    )
    static_current = static_runtime_root(runtime_root, manifest) / "current"
    digest.update(b"static-current\0")
    if static_current.is_symlink():
        digest.update(str(static_current.readlink()).encode("utf-8"))
        digest.update(b"\0")
    static_digest = _directory_digest(static_current)
    digest.update((static_digest or "missing").encode("ascii"))
    digest.update(b"\0")
    return digest.hexdigest()


def _update_baseline_digest(digest: Any, label: str, path: Path) -> None:
    digest.update(label.encode("utf-8"))
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(str(path.readlink()).encode("utf-8"))
        digest.update(b"\0")
        return
    if not path.exists():
        digest.update(b"missing\0")
        return
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        return
    if path.is_dir():
        digest.update(b"directory\0")
        digest.update((_directory_digest(path) or "empty").encode("ascii"))
        digest.update(b"\0")
        return
    digest.update(b"special\0")


def _directory_digest(path: Path) -> str | None:
    if not path.exists() or not path.is_dir():
        return None
    import hashlib

    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path)
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        if child.is_symlink():
            digest.update(b"symlink\0")
            digest.update(str(child.readlink()).encode("utf-8"))
            digest.update(b"\0")
            continue
        if not child.is_file():
            continue
        digest.update(b"file\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _directory_file_count(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


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


def copy_staged_support_files(
    source_root: Path,
    support_paths: set[Path],
    output_dir: Path,
    *,
    required_paths: set[Path] | None = None,
) -> None:
    required_paths = required_paths or set()
    for relative_path in sorted(support_paths):
        source = source_root / relative_path
        if not source.exists():
            if relative_path in required_paths:
                raise FileNotFoundError(f"Support file missing from staged bundle: {source}")
            continue
        _copy_support_path(source, output_dir / relative_path, allowed_root=source_root)


def active_support_paths(runtime_root: Path, app: str) -> set[Path]:
    release = active_release(runtime_root, app)
    bundle_root = _release_bundle_root(runtime_root / "apps" / app, release) if release else None
    if bundle_root is not None and bundle_root.exists():
        return _support_files_under(bundle_root) | _declared_support_files(bundle_root)
    return set()


def remove_stale_support_files(
    app_root: Path,
    preserve_paths: set[Path],
    *,
    candidate_paths: set[Path] | None = None,
) -> List[Path]:
    removed: List[Path] = []
    candidates = _support_files_under(app_root) | (candidate_paths or set())
    for relative_path in sorted(candidates):
        if _preserves_support_path(relative_path, preserve_paths):
            continue
        target = app_root / relative_path
        if target.exists() and target.is_file():
            target.unlink()
            removed.append(relative_path)
    _prune_empty_dirs(app_root / "env.d", stop=app_root)
    _prune_empty_dirs(app_root / "artifacts", stop=app_root)
    return removed


def cleanup_inactive_support_files(
    runtime_root: Path,
    app: str,
    *,
    candidate_paths: set[Path] | None = None,
) -> List[Path]:
    app_root = runtime_root / "apps" / app
    release = active_release(runtime_root, app)
    preserve_paths = active_support_paths(runtime_root, app)
    if release:
        bundle_root = _release_bundle_root(app_root, release)
        manifest = _load_manifest_from_bundle(bundle_root)
        if manifest is not None:
            preserve_paths |= bundle_support_paths(manifest)
    return remove_stale_support_files(
        app_root,
        preserve_paths,
        candidate_paths=candidate_paths,
    )


def apply_local_bundle(
    manifest: Manifest,
    manifest_path: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    ophelia_root: Path | None = None,
    deploy_metadata: DeployMetadata | None = None,
    *,
    candidate_root: Path | None = None,
    candidate_generated_files: List[Path] | None = None,
    expected_candidate_digest: str | None = None,
    expected_bundle_hash: str | None = None,
    expected_baseline_digest: str | None = None,
) -> Path:
    if manifest.environment == "production" and not manifest.lifecycle.production_apply_allowed:
        raise ApplyPhaseError(
            "lifecycle_policy",
            "Refusing production apply because lifecycle.production_apply_allowed is false.",
        )
    current_app_root = runtime_root / "apps" / manifest.app
    superseded_support_files = (
        _declared_support_files(current_app_root) | _support_files_under(current_app_root)
    )
    if candidate_root is None:
        app_root = deploy_bundle(manifest, manifest_path, runtime_root, deploy_metadata=deploy_metadata)
    else:
        if (
            candidate_generated_files is None
            or expected_candidate_digest is None
            or expected_bundle_hash is None
            or expected_baseline_digest is None
        ):
            raise ValueError("Confirmed candidate apply is missing its reviewed bindings.")
        app_root = deploy_confirmed_candidate(
            manifest,
            manifest_path,
            candidate_root,
            candidate_generated_files,
            runtime_root,
            deploy_metadata,
            expected_candidate_digest=expected_candidate_digest,
            expected_bundle_hash=expected_bundle_hash,
            expected_baseline_digest=expected_baseline_digest,
        )
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
        missing_required_keys = missing_required_env_keys(manifest, app_root / "env")
        if missing_required_keys:
            joined = ", ".join(missing_required_keys)
            raise ApplyPhaseError(
                "env_validation",
                f"Refusing to apply with missing required env keys in {app_root / 'env'}: {joined}",
            )
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
            sync_caddy_env(manifest, app_root, runtime_root)
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
                mark_phase("network_prepare", "running")
                ensure_compose_networks(manifest)
                mark_phase("network_prepare", "ok")

                # Image pulls are release-critical. Continuing after a failed pull can
                # leave a host serving a stale local tag while the deploy appears done.
                # For private retained deployments, a digest-pinned image may already
                # exist locally while registry pulls are denied. In that case the local
                # cache is an exact target and apply can continue.
                mark_phase("image_pull", "running")
                _pull_or_use_local_images(compose_path, manifest)
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
                        "sh",
                        "-ec",
                        CADDY_ENVFILE_RELOAD_SCRIPT,
                    ],
                    "caddy_reload",
                    env=_runtime_env(runtime_root),
                )
                mark_phase("caddy_reload", "ok")

        if static_source_bundle_path(manifest) is not None:
            mark_phase("static_assets_publish", "running")
            publish_staged_static_assets(manifest, runtime_root, app_root)
            mark_phase("static_assets_publish", "ok")

        update_current_release_apply(
            runtime_root,
            manifest.app,
            {"status": "applied", "ok": True, "applied": True, "phase": "complete", "phases": phases},
        )
        cleanup_inactive_support_files(
            runtime_root,
            manifest.app,
            candidate_paths=superseded_support_files,
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


def ensure_compose_networks(manifest: Manifest) -> None:
    if manifest.kind not in {"service", "multi-service"}:
        return

    required = ["ophelia-edge"]
    if manifest.networking.internal == "shared" or manifest.addons.postgres or manifest.addons.redis:
        required.append("ophelia-internal")

    for network in required:
        result = _run(
            ["docker", "network", "inspect", network],
            capture_output=True,
            allow_failure=True,
        )
        if result is None:
            _run_apply_phase(
                ["docker", "network", "create", network],
                "network_prepare",
                capture_output=True,
            )


def publish_staged_static_assets(manifest: Manifest, runtime_root: Path, app_root: Path) -> Dict[str, object]:
    release = _load_release_json_or_raise(app_root / "release.json", f"{manifest.app}/current")
    release_id = release.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise ApplyPhaseError("static_assets_publish", "Release record is missing release_id.")
    bundle_path = release.get("bundle_path")
    bundle_root = (
        Path(str(bundle_path))
        if isinstance(bundle_path, str) and bundle_path
        else app_root / "release-bundles" / release_id
    )
    static_assets = publish_static_assets(manifest, bundle_root, runtime_root, release_id)
    _update_current_release(runtime_root, manifest.app, {"static_assets": static_assets})
    return static_assets


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

        payload = _load_release_json(release_path)
        required = {"app", "kind", "runtime_path", "deployed_at", "source_manifest"}
        if not payload or not required.issubset(payload):
            continue
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
    payload = _load_release_json(release_path)
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
        payload = _load_release_json(active_path)
        if isinstance(payload, dict) and payload:
            return payload

    legacy_path = app_root / "release.json"
    if legacy_path.exists():
        payload = _load_release_json(legacy_path)
        if isinstance(payload, dict) and payload.get("applied") is True:
            return payload
    return {}


def list_releases(runtime_root: Path, app: str) -> List[Dict[str, object]]:
    releases_root = runtime_root / "apps" / app / "releases"
    records: List[Dict[str, object]] = []

    if releases_root.exists():
        for release_path in sorted(releases_root.glob("*.json")):
            payload = _load_release_json(release_path)
            if not isinstance(payload, dict):
                continue
            payload.setdefault("release_id", release_path.stem)
            records.append(payload)

    if not records:
        legacy_path = runtime_root / "apps" / app / "release.json"
        if legacy_path.exists():
            payload = _load_release_json(legacy_path)
            if isinstance(payload, dict) and payload:
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
        return _load_release_json_or_raise(active_path, f"{app}/active")

    release_path = runtime_root / "apps" / app / "releases" / f"{release_id}.json"
    if release_path.exists():
        return _load_release_json_or_raise(release_path, f"{app}/{release_id}")

    legacy_path = runtime_root / "apps" / app / "release.json"
    if release_id == "current" and legacy_path.exists():
        return _load_release_json_or_raise(legacy_path, f"{app}/current")
    if release_id == "legacy-current" and legacy_path.exists():
        payload = _load_release_json_or_raise(legacy_path, f"{app}/legacy-current")
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
            historical = _load_release_json(historical_path)
            historical.update(release)
            historical_path.write_text(json.dumps(historical, indent=2, sort_keys=True) + "\n")
    latest_path = app_root / "release.json"
    if latest_path.exists():
        latest = _load_release_json(latest_path)
        if latest.get("release_id") == release_id:
            latest.update(release)
            latest_path.write_text(json.dumps(latest, indent=2, sort_keys=True) + "\n")


def _update_current_release(runtime_root: Path, app: str, updates: Dict[str, object]) -> Dict[str, object] | None:
    app_root = runtime_root / "apps" / app
    release_path = app_root / "release.json"
    if not release_path.exists():
        return None

    payload = _load_release_json(release_path)
    if not payload:
        return None
    payload.update(updates)
    release_id = payload.get("release_id")
    if isinstance(release_id, str):
        historical_path = app_root / "releases" / f"{release_id}.json"
        if historical_path.exists():
            historical_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    release_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _load_release_json(path: Path) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_release_json_or_raise(path: Path, label: str) -> Dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Release record is unreadable: {label}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Release record must be a JSON object: {label}")
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


def _release_metadata_env(
    manifest: Manifest,
    *,
    release_id: str,
    commit_sha: str,
    build_time: str,
) -> Dict[str, Any]:
    images = image_references(manifest)
    digests = image_digests(manifest)
    services: Dict[str, Dict[str, str]] = {}
    for service_name in sorted(manifest.services):
        image_ref = images.get(service_name) or images.get("default") or ""
        services[service_name] = {
            "image_ref": image_ref,
            "image_digest": digests.get(service_name) or digests.get("default") or "",
        }
    return {
        "environment": manifest.environment or "unknown",
        "app": manifest.app,
        "release_id": release_id,
        "commit_sha": commit_sha,
        "build_time": build_time,
        "services": services,
    }


def _resolve_deploy_metadata(
    manifest: Manifest,
    *,
    deployed_at: str,
    generated_release_id: str,
    deploy_metadata: DeployMetadata | None,
) -> Dict[str, str]:
    metadata = deploy_metadata or DeployMetadata()
    if metadata.locked:
        if not metadata.release_id or not metadata.build_time:
            raise ValueError("Locked deploy metadata requires release_id and build_time.")
        return {
            "app": manifest.app,
            "environment": manifest.environment or "unknown",
            "release_id": metadata.release_id,
            "commit_sha": metadata.commit_sha or "",
            "build_time": metadata.build_time,
        }
    release_id = _first_nonempty(
        metadata.release_id,
        os.environ.get("OPHELIA_DEPLOY_RELEASE_ID"),
        os.environ.get("OPHELIA_RELEASE_ID"),
        generated_release_id,
    )
    commit_sha = _first_nonempty(
        metadata.commit_sha,
        os.environ.get("OPHELIA_DEPLOY_COMMIT_SHA"),
        os.environ.get("OPHELIA_COMMIT_SHA"),
        os.environ.get("GITHUB_SHA"),
        "",
    )
    build_time = _first_nonempty(
        metadata.build_time,
        os.environ.get("OPHELIA_DEPLOY_BUILD_TIME"),
        os.environ.get("OPHELIA_BUILD_TIME"),
        deployed_at,
    )
    return {
        "app": manifest.app,
        "environment": manifest.environment or "unknown",
        "release_id": release_id,
        "commit_sha": commit_sha,
        "build_time": build_time,
    }


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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


def _declared_support_files(root: Path) -> set[Path]:
    if not root.exists() or not root.is_dir():
        return set()
    manifest = _load_manifest_from_bundle(root)
    if manifest is None:
        return set()
    files: set[Path] = set()
    for relative_path in bundle_support_paths(manifest):
        target = root / relative_path
        if target.is_file() or target.is_symlink():
            files.add(relative_path)
            continue
        if target.is_dir():
            files.update(
                child.relative_to(root)
                for child in target.rglob("*")
                if child.is_file() or child.is_symlink()
            )
    return files


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
    except (ManifestError, OSError, ValueError):
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


def missing_required_env_keys(manifest: Manifest, path: Path) -> List[str]:
    values = _load_env_file(path)
    return sorted(key for key in _required_placeholder_env_keys(manifest) if key not in values)


def _required_placeholder_env_keys(manifest: Manifest) -> List[str]:
    keys: List[str] = []
    for raw_line in render_env_example(manifest).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if _is_placeholder_value(value):
            keys.append(key)
    return sorted(set(keys))


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


def _pull_or_use_local_images(compose_path: Path, manifest: Manifest) -> None:
    try:
        _run_apply_phase(["docker", "compose", "-f", str(compose_path), "pull"], "image_pull")
        return
    except ApplyPhaseError as exc:
        missing = _missing_local_images(manifest)
        if missing:
            joined = ", ".join(missing)
            raise ApplyPhaseError(
                "image_pull",
                f"{exc}; missing local image reference(s): {joined}",
            ) from exc


def _missing_local_images(manifest: Manifest) -> List[str]:
    missing: List[str] = []
    for image in sorted(set(image_references(manifest).values())):
        result = _run(["docker", "image", "inspect", image], capture_output=True, allow_failure=True)
        if result is None:
            missing.append(image)
    return missing


def _resolve_support_path(manifest_dir: Path, source: str) -> Path:
    raw = Path(source).expanduser()
    return raw if raw.is_absolute() else (manifest_dir / raw)


def _support_path_candidate(manifest_dir: Path, source: str) -> Path:
    return _resolve_support_path(manifest_dir, source)


def _copy_support_path(source: Path, destination: Path, *, allowed_root: Path) -> None:
    if source.resolve(strict=False) == destination.resolve(strict=False):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert_no_external_symlinks(source, allowed_root)
    if source.is_symlink():
        shutil.copy2(source, destination, follow_symlinks=False)
        return
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        return
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_static_source_tree(source: Path, destination: Path) -> None:
    resolved_source = source.resolve(strict=False)
    resolved_destination = destination.resolve(strict=False)
    if resolved_source == resolved_destination:
        return
    if resolved_source in resolved_destination.parents:
        raise RuntimeError(f"Refusing to copy static assets into their own source tree: {destination}")
    if not source.exists():
        raise FileNotFoundError(f"Static asset source not found: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Static asset source must be a directory: {source}")
    _assert_static_source_safe(source)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def _assert_static_source_safe(source: Path) -> None:
    issues = _static_symlink_issues(source)
    if not issues:
        return
    first = issues[0]
    detail = first.get("target") or first.get("reason")
    raise RuntimeError(f"Static asset source contains an unsafe symlink: {first['path']} -> {detail}")


def _static_symlink_issues(source: Path) -> List[Dict[str, str]]:
    if not source.exists() and not source.is_symlink():
        return []

    issues: List[Dict[str, str]] = []
    root = source.resolve(strict=False)

    def check(path: Path) -> None:
        try:
            target = path.readlink()
        except OSError as exc:
            issues.append({"path": str(path), "reason": str(exc)})
            return
        if target.is_absolute():
            issues.append({"path": str(path), "target": str(target), "reason": "absolute symlink"})
            return
        target_path = path.parent / target
        if not target_path.exists():
            issues.append({"path": str(path), "target": str(target), "reason": "broken symlink"})
            return
        try:
            path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            issues.append({"path": str(path), "target": str(target), "reason": "outside static root"})

    if source.is_symlink():
        issues.append({"path": str(source), "target": str(source.readlink()), "reason": "static root symlink"})
        return issues
    if not source.is_dir():
        return issues
    for child in source.rglob("*"):
        if child.is_symlink():
            check(child)
    return issues


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
