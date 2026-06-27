"""Production image digest locking workflow."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .manifest import Manifest, ManifestError, load_manifest
from .operation_schema import artifact, issue, plan_envelope, receipt_envelope, token
from .redaction import deep_redact, redact_command_string
from .runtime import image_references


def image_lock_plan(
    manifest_path: Path,
    *,
    output: Optional[Path] = None,
    pinned_manifest: Optional[Path] = None,
    timeout: float = 30.0,
) -> Dict[str, object]:
    manifest = load_manifest(manifest_path)
    output_path = output or _default_lock_path(manifest_path)
    pinned_manifest_path = pinned_manifest
    warnings: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []
    checks: List[Dict[str, object]] = []

    if manifest.environment != "production":
        warnings.append(
            issue(
                "image_lock_non_production",
                "Image digest locks are most important for production manifests; this manifest is not production.",
                "environment",
            )
        )

    targets = _image_targets(manifest)
    if not targets:
        blockers.append(issue("image_lock_no_images", "No image references were found in this manifest.", "image"))

    locked: List[Dict[str, object]] = []
    resolved_by_image: Dict[str, Dict[str, object]] = {}
    for target in targets:
        image = str(target["image"])
        resolved = resolved_by_image.get(image)
        if resolved is None:
            resolved = resolve_image_digest(image, timeout=timeout)
            resolved_by_image[image] = resolved
        entry = {
            **target,
            "digest": resolved.get("digest"),
            "locked_image": _locked_image_ref(image, resolved.get("digest")),
            "resolver": resolved.get("resolver"),
            "status": resolved["status"],
            "already_pinned": _image_has_digest(image),
        }
        if resolved["status"] == "unresolved":
            entry["error"] = resolved.get("error")
            blockers.append(
                issue(
                    "image_digest_unresolved",
                    f"Could not resolve digest for image `{image}`: {resolved.get('error') or 'unknown error'}",
                    str(target.get("path") or "image"),
                )
            )
        locked.append(deep_redact(entry, propagate=True))

    checks.append(
        {
            "name": "image_digest_resolution",
            "ok": not any(entry.get("status") == "unresolved" for entry in locked),
            "message": f"{sum(1 for entry in locked if entry.get('digest'))}/{len(locked)} image reference(s) have digests.",
        }
    )
    changes = [
        {
            "path": entry["path"],
            "service": entry.get("service"),
            "from": entry["image"],
            "to": entry["locked_image"],
        }
        for entry in locked
        if entry.get("patchable", True)
        and entry.get("locked_image")
        and entry.get("locked_image") != entry.get("image")
    ]
    apply_input = {
        "manifest": str(manifest_path),
        "output": str(output_path),
        "pinned_manifest": str(pinned_manifest_path) if pinned_manifest_path is not None else None,
        "locked_images": [
            {
                "path": entry.get("path"),
                "image": entry.get("image"),
                "digest": entry.get("digest"),
                "locked_image": entry.get("locked_image"),
            }
            for entry in locked
        ],
    }
    confirmation_token = None if blockers else token("release.image-lock.apply", apply_input)
    apply_command = None
    if confirmation_token:
        apply_command = (
            f"ship release image-lock apply {manifest_path} --output {output_path} "
            f"--confirm {confirmation_token}"
        )
        if pinned_manifest_path is not None:
            apply_command += f" --pinned-manifest {pinned_manifest_path}"

    return plan_envelope(
        "release.image-lock.plan",
        manifest.app,
        manifest.environment,
        f"Image digest lock plan for {manifest.app}: {len(blockers)} blocker(s), {len(changes)} change(s).",
        blockers=blockers,
        warnings=warnings,
        checks=checks,
        artifacts=[
            artifact(str(output_path), "image-lock", "Resolved image digest lock file", present=output_path.exists()),
            *(
                [
                    artifact(
                        str(pinned_manifest_path),
                        "pinned-manifest",
                        "Manifest copy with image references rewritten to immutable digests",
                        present=pinned_manifest_path.exists(),
                    )
                ]
                if pinned_manifest_path is not None
                else []
            ),
        ],
        confirmation_required=True,
        confirmation_token=confirmation_token,
        exact_apply_input={"command": apply_command} if apply_command else None,
        risk="high" if manifest.environment == "production" else "medium",
        changes=changes,
        manifest_path=str(manifest_path),
        output=str(output_path),
        pinned_manifest=str(pinned_manifest_path) if pinned_manifest_path is not None else None,
        images=locked,
        all_pinned=not blockers and all(bool(entry.get("digest")) for entry in locked),
        secrets_redacted=True,
    )


def image_lock_apply(
    manifest_path: Path,
    *,
    output: Optional[Path] = None,
    pinned_manifest: Optional[Path] = None,
    confirm: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, object]:
    started_at = _utc_now()
    plan = image_lock_plan(
        manifest_path,
        output=output,
        pinned_manifest=pinned_manifest,
        timeout=timeout,
    )
    blockers = list(plan.get("blockers", [])) if isinstance(plan.get("blockers"), list) else []
    warnings = list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else []
    expected = plan.get("confirmation_token")
    if not isinstance(confirm, str) or not confirm:
        blockers.append(issue("confirmation_token_missing", "Image lock apply requires a confirmation token from image-lock plan."))
    elif confirm != expected:
        blockers.append(issue("confirmation_token_mismatch", "Image lock confirmation token does not match the current plan."))

    app = str(plan.get("app") or "unknown")
    environment = str(plan.get("environment") or "unknown")
    if blockers:
        return receipt_envelope(
            "release.image-lock.apply",
            app,
            environment,
            "blocked",
            started_at,
            _utc_now(),
            artifacts=plan.get("artifacts", []) if isinstance(plan.get("artifacts"), list) else [],
            checks=plan.get("checks", []) if isinstance(plan.get("checks"), list) else [],
            rollback={"available": False, "note": "Image lock apply was blocked before writing files."},
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            blockers=blockers,
            warnings=warnings,
            inputs_redacted=True,
            secrets_redacted=True,
        )

    output_path = Path(str(plan["output"]))
    lock_payload = _lock_payload(plan)
    _write_json(output_path, lock_payload)
    written_artifacts = [
        artifact(str(output_path), "image-lock", "Resolved image digest lock file", present=True),
    ]
    if pinned_manifest is not None:
        _write_pinned_manifest(manifest_path, pinned_manifest, plan)
        written_artifacts.append(
            artifact(
                str(pinned_manifest),
                "pinned-manifest",
                "Manifest copy with image references rewritten to immutable digests",
                present=True,
            )
        )

    return receipt_envelope(
        "release.image-lock.apply",
        app,
        environment,
        "succeeded",
        started_at,
        _utc_now(),
        artifacts=written_artifacts,
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched current image-lock plan."},
            {
                "name": "image_lock_written",
                "ok": True,
                "message": f"Wrote {len(lock_payload['images'])} locked image reference(s).",
            },
        ],
        rollback={
            "available": True,
            "note": "Remove the generated lock file or pinned manifest copy; no runtime state was changed.",
        },
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        warnings=warnings,
        output=str(output_path),
        pinned_manifest=str(pinned_manifest) if pinned_manifest is not None else None,
        lock=lock_payload,
        inputs_redacted=True,
        secrets_redacted=True,
    )


def resolve_image_digest(image: str, *, timeout: float = 30.0) -> Dict[str, object]:
    if _image_has_digest(image):
        return {"status": "already_pinned", "digest": image.split("@", 1)[1], "resolver": "image-reference"}
    buildx = _resolve_with_buildx(image, timeout=timeout)
    if buildx.get("digest"):
        return {"status": "resolved", **buildx}
    manifest = _resolve_with_docker_manifest(image, timeout=timeout)
    if manifest.get("digest"):
        return {"status": "resolved", **manifest}
    error = manifest.get("error") or buildx.get("error") or "Docker could not resolve an image digest."
    return {"status": "unresolved", "digest": None, "resolver": None, "error": error}


def _image_targets(manifest: Manifest) -> List[Dict[str, object]]:
    targets: List[Dict[str, object]] = []
    if manifest.image:
        targets.append({"path": "image", "service": "default", "image": manifest.image, "patchable": True})
    for service_name, service in sorted(manifest.services.items()):
        if service.image:
            targets.append({"path": f"services.{service_name}.image", "service": service_name, "image": service.image, "patchable": True})
        elif manifest.image:
            targets.append(
                {
                    "path": f"services.{service_name}.image",
                    "service": service_name,
                    "image": manifest.image,
                    "inherited": True,
                    "patchable": False,
                }
            )
    return targets


def _resolve_with_buildx(image: str, *, timeout: float) -> Dict[str, object]:
    result = _run_docker(["docker", "buildx", "imagetools", "inspect", image], timeout=timeout)
    if result.returncode != 0:
        return {"resolver": "docker-buildx-imagetools", "error": _resolver_error(result)}
    match = re.search(r"(?im)^\s*Digest:\s*(sha256:[0-9a-f]{64})\s*$", result.stdout)
    if not match:
        return {"resolver": "docker-buildx-imagetools", "error": "No digest line found in Docker buildx output."}
    return {"resolver": "docker-buildx-imagetools", "digest": match.group(1)}


def _resolve_with_docker_manifest(image: str, *, timeout: float) -> Dict[str, object]:
    result = _run_docker(["docker", "manifest", "inspect", "--verbose", image], timeout=timeout)
    if result.returncode != 0:
        return {"resolver": "docker-manifest", "error": _resolver_error(result)}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    digest = _digest_from_manifest_payload(payload)
    if digest:
        return {"resolver": "docker-manifest", "digest": digest}
    match = re.search(r"(sha256:[0-9a-f]{64})", result.stdout)
    if match:
        return {"resolver": "docker-manifest", "digest": match.group(1)}
    return {"resolver": "docker-manifest", "error": "No digest found in Docker manifest output."}


def _run_docker(args: List[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _digest_from_manifest_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        descriptor = payload.get("Descriptor")
        if isinstance(descriptor, dict) and isinstance(descriptor.get("digest"), str):
            return descriptor["digest"]
        if isinstance(payload.get("digest"), str):
            return payload["digest"]
    if isinstance(payload, list):
        for item in payload:
            digest = _digest_from_manifest_payload(item)
            if digest:
                return digest
    return None


def _resolver_error(result: subprocess.CompletedProcess[str]) -> str:
    message = (result.stderr or result.stdout or "").strip()
    if not message:
        return f"Docker command exited with {result.returncode}."
    return redact_command_string(message.splitlines()[-1][:500])


def _locked_image_ref(image: str, digest: object) -> Optional[str]:
    if not isinstance(digest, str) or not digest:
        return None
    if "@" in image:
        return image
    return f"{_image_without_tag(image)}@{digest}"


def _image_without_tag(image: str) -> str:
    # Preserve registry ports by looking for tag separators after the last slash.
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon]
    return image


def _image_has_digest(image: str) -> bool:
    return "@sha256:" in image


def _default_lock_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.stem}.image-lock.json")


def _lock_payload(plan: Dict[str, object]) -> Dict[str, object]:
    return deep_redact(
        {
            "schema_version": 1,
            "kind": "ophelia.image_lock",
            "app": plan.get("app"),
            "environment": plan.get("environment"),
            "manifest_path": plan.get("manifest_path"),
            "created_at": _utc_now(),
            "plan_operation_id": plan.get("operation_id"),
            "images": plan.get("images", []),
            "changes": plan.get("changes", []),
            "secrets_redacted": True,
        },
        propagate=True,
    )


def _write_pinned_manifest(manifest_path: Path, output_path: Path, plan: Dict[str, object]) -> None:
    yaml = _load_yaml()
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError("Manifest root must be a mapping.")
    for change in plan.get("changes", []) if isinstance(plan.get("changes"), list) else []:
        if not isinstance(change, dict) or not isinstance(change.get("to"), str):
            continue
        path = str(change.get("path") or "")
        if path == "image":
            raw["image"] = change["to"]
        elif path.startswith("services.") and path.endswith(".image"):
            _, service_name, _ = path.split(".", 2)
            services = raw.setdefault("services", {})
            if isinstance(services, dict):
                service = services.setdefault(service_name, {})
                if isinstance(service, dict):
                    service["image"] = change["to"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _load_yaml():
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:
        raise ManifestError("PyYAML is required to write pinned manifests.") from exc
    return yaml


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
