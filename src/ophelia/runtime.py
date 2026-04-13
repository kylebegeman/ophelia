from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest
from .templates import render_caddy, render_compose, render_env_example


@dataclass
class DeploymentRecord:
    app: str
    kind: str
    runtime_path: Path
    deployed_at: str
    source_manifest: str


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


def deploy_bundle(manifest: Manifest, manifest_path: Path, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Path:
    app_root = runtime_root / "apps" / manifest.app
    write_bundle(render_bundle(manifest), app_root)

    env_path = app_root / "env"
    env_example_path = app_root / "env.example"
    if not env_path.exists():
        env_path.write_text(env_example_path.read_text())

    release = {
        "app": manifest.app,
        "kind": manifest.kind,
        "source_manifest": str(manifest_path.resolve()),
        "runtime_path": str(app_root.resolve()),
        "deployed_at": _utc_now(),
    }
    (app_root / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
    return app_root


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
                runtime_path=Path(payload["runtime_path"]),
                deployed_at=payload["deployed_at"],
                source_manifest=payload["source_manifest"],
            )
        )
    return deployments


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
