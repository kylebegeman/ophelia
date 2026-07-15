"""Independent consumer for Forge-compatible ``product.*`` operations bundles.

Ophelia intentionally does not import Forge.  This module implements the shared
v1 wire contracts at the process boundary, including strict object shapes,
content digests, cross-document identity, and artifact byte verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


MAX_CONTRACT_BYTES = 4 << 20
MAX_ARTIFACT_BYTES = 2 << 30
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProductBundleError(ValueError):
    """A product operations bundle failed closed at Ophelia's boundary."""


@dataclass(frozen=True)
class ProductOperationsBundle:
    root: Path
    bundle: Mapping[str, Any]
    runtime: Mapping[str, Any]
    release: Mapping[str, Any]
    recovery: Mapping[str, Any]
    document_digests: Mapping[str, str]

    @property
    def product_id(self) -> str:
        return str(self.bundle["product_id"])

    @property
    def release_id(self) -> str:
        return str(self.bundle["release_id"])

    @property
    def bundle_digest(self) -> str:
        return str(self.bundle["bundle_digest"])

    @property
    def composition_digest(self) -> str:
        return str(self.bundle["composition_digest"])

    def artifact(self, artifact_id: str) -> Mapping[str, Any]:
        for item in self.release["artifacts"]:
            if item["id"] == artifact_id:
                return item
        raise ProductBundleError(f"Release artifact is not declared: {artifact_id}")


@dataclass(frozen=True)
class VerifiedProductArtifact:
    artifact_id: str
    path: Path
    digest: str
    size_bytes: int
    compatible: bool
    platform_os: str
    platform_arch: str


def load_product_operations_bundle(root: Path) -> ProductOperationsBundle:
    """Load and validate a complete bundle without relying on Forge code."""

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ProductBundleError("Operations bundle root must be a real directory.")
    root = root.resolve()
    bundle, bundle_bytes = _read_json(root / "bundle.json")
    _validate_bundle(bundle)

    documents: Dict[str, Mapping[str, Any]] = {}
    document_digests: Dict[str, str] = {}
    for reference in bundle["documents"]:
        kind = str(reference["kind"])
        path = _contained_file(root, str(reference["path"]))
        value, raw = _read_json(path)
        actual = _digest_bytes(raw)
        if actual != reference["digest"]:
            raise ProductBundleError(f"Bundle document digest mismatch: {kind}")
        documents[kind] = value
        document_digests[kind] = actual

    runtime = documents["runtime-requirements"]
    release = documents["release-manifest"]
    recovery = documents["recovery-contract"]
    _validate_runtime(runtime)
    _validate_release(release)
    _validate_recovery(recovery)
    _validate_value_digest(runtime, "contract_digest", "runtime contract")
    _validate_value_digest(release, "manifest_digest", "release manifest")
    _validate_value_digest(recovery, "contract_digest", "recovery contract")
    _validate_value_digest(bundle, "bundle_digest", "operations bundle")
    _validate_cross_document(bundle, runtime, release, recovery)

    # The readable file digest is intentionally distinct from bundle_digest.
    # The former binds exact bytes; the latter binds the canonical Go value.
    if _digest_bytes(bundle_bytes) in document_digests.values():
        raise ProductBundleError("Bundle index aliases a contract document digest.")
    return ProductOperationsBundle(
        root=root,
        bundle=bundle,
        runtime=runtime,
        release=release,
        recovery=recovery,
        document_digests=document_digests,
    )


def verify_product_artifact(
    bundle: ProductOperationsBundle,
    artifact_id: str,
    path: Path,
    *,
    require_compatible: bool = True,
) -> VerifiedProductArtifact:
    """Verify bytes, size, kind, and host compatibility for one artifact."""

    declaration = bundle.artifact(artifact_id)
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductBundleError(f"Artifact is unavailable: {artifact_id}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ProductBundleError(f"Artifact must be a real regular file: {artifact_id}")
    size = metadata.st_size
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        raise ProductBundleError(f"Artifact size is outside Ophelia's bounds: {artifact_id}")
    if size != declaration["size_bytes"]:
        raise ProductBundleError(f"Artifact size does not match the release: {artifact_id}")
    actual = _digest_file(path)
    if actual != declaration["digest"]:
        raise ProductBundleError(f"Artifact digest does not match the release: {artifact_id}")
    target = declaration["platform"]
    compatible = (
        str(target["os"]) == host_platform()[0]
        and _normalize_arch(str(target["arch"])) == host_platform()[1]
    )
    if require_compatible and not compatible:
        raise ProductBundleError(
            "Artifact platform %s/%s cannot execute on this host's %s/%s."
            % (target["os"], target["arch"], *host_platform())
        )
    return VerifiedProductArtifact(
        artifact_id=artifact_id,
        path=path.resolve(),
        digest=actual,
        size_bytes=size,
        compatible=compatible,
        platform_os=str(target["os"]),
        platform_arch=str(target["arch"]),
    )


def host_platform() -> Tuple[str, str]:
    system = platform.system().lower()
    return system, _normalize_arch(platform.machine())


def bundle_report(bundle: ProductOperationsBundle) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "ophelia.product-bundle-report",
        "ok": True,
        "product_id": bundle.product_id,
        "release_id": bundle.release_id,
        "bundle_digest": bundle.bundle_digest,
        "composition_digest": bundle.composition_digest,
        "document_digests": dict(sorted(bundle.document_digests.items())),
        "artifact_count": len(bundle.release["artifacts"]),
        "process_count": len(bundle.runtime["processes"]),
        "dataset_count": len(bundle.recovery["datasets"]),
        "host_platform": {"os": host_platform()[0], "arch": host_platform()[1]},
        "secret_values_present": False,
    }


def _read_json(path: Path) -> Tuple[Mapping[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise ProductBundleError(f"Contract must be a real regular file: {path.name}")
        if metadata.st_size > MAX_CONTRACT_BYTES:
            raise ProductBundleError(f"Contract exceeds {MAX_CONTRACT_BYTES} bytes: {path.name}")
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except ProductBundleError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductBundleError(f"Contract is unreadable or malformed: {path.name}") from exc
    if not isinstance(value, dict):
        raise ProductBundleError(f"Contract root must be an object: {path.name}")
    return value, raw


def _strict_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProductBundleError(f"Contract repeats object key: {key}")
        value[key] = item
    return value


def _validate_bundle(value: Mapping[str, Any]) -> None:
    _shape(value, "bundle", {
        "schema_version", "kind", "bundle_digest", "product_id", "release_id",
        "composition_digest", "documents",
    })
    _constant(value, "schema_version", "product.operations-bundle/v1")
    _constant(value, "kind", "product.operations-bundle")
    _id(value["product_id"], "product_id")
    _id(value["release_id"], "release_id")
    _digest(value["bundle_digest"], "bundle_digest")
    _digest(value["composition_digest"], "composition_digest")
    documents = _list(value["documents"], "documents", minimum=3)
    if len(documents) != 3:
        raise ProductBundleError("Operations bundle must contain exactly three documents.")
    kinds, paths = set(), set()
    for item in documents:
        _shape(item, "document", {"kind", "path", "digest"})
        if item["kind"] not in {"runtime-requirements", "release-manifest", "recovery-contract"}:
            raise ProductBundleError("Operations bundle has an unknown document kind.")
        _safe_path(item["path"], "document path")
        _digest(item["digest"], "document digest")
        kinds.add(item["kind"])
        paths.add(item["path"])
    if len(kinds) != 3 or len(paths) != 3:
        raise ProductBundleError("Operations bundle document kinds and paths must be unique.")


def _validate_runtime(value: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "kind", "contract_digest", "product_id", "stack",
        "composition_digest", "processes", "ports", "health", "data", "lifecycle",
    }
    _shape(value, "runtime requirements", required, {
        "environment", "secret_references", "providers",
    })
    _constant(value, "schema_version", "product.runtime-requirements/v1")
    _constant(value, "kind", "product.runtime-requirements")
    _id(value["product_id"], "product_id")
    _digest(value["contract_digest"], "contract_digest")
    _digest(value["composition_digest"], "composition_digest")
    _validate_stack(value["stack"])
    process_ids = set()
    for item in _list(value["processes"], "processes"):
        _shape(item, "process", {"id", "artifact_id", "argv", "replicas", "shutdown_seconds"})
        _id(item["id"], "process id")
        _id(item["artifact_id"], "artifact id")
        argv = _list(item["argv"], "process argv")
        if not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise ProductBundleError("Process argv must contain strings without NUL bytes.")
        _integer(item["replicas"], "replicas", 1)
        _integer(item["shutdown_seconds"], "shutdown_seconds", 1, 900)
        if item["id"] in process_ids:
            raise ProductBundleError("Runtime process ids must be unique.")
        process_ids.add(item["id"])
    port_ids = set()
    for item in _list(value["ports"], "ports"):
        _shape(item, "port", {"id", "process_id", "protocol", "port", "exposure"})
        _id(item["id"], "port id")
        if item["process_id"] not in process_ids:
            raise ProductBundleError("Runtime port references an unknown process.")
        if item["protocol"] not in {"http", "https", "tcp"} or item["exposure"] not in {"public", "internal", "loopback"}:
            raise ProductBundleError("Runtime port protocol or exposure is invalid.")
        _integer(item["port"], "port", 1, 65535)
        if item["id"] in port_ids:
            raise ProductBundleError("Runtime port ids must be unique.")
        port_ids.add(item["id"])
    health_ids = set()
    for item in _list(value["health"], "health"):
        _shape(item, "health probe", {"id", "process_id", "kind", "path", "port_id", "interval_seconds", "timeout_seconds"})
        _id(item["id"], "health id")
        if item["process_id"] not in process_ids or item["port_id"] not in port_ids:
            raise ProductBundleError("Health probe references an unknown process or port.")
        if item["kind"] not in {"liveness", "readiness"} or not isinstance(item["path"], str) or not item["path"].startswith("/"):
            raise ProductBundleError("Health probe kind or path is invalid.")
        _integer(item["timeout_seconds"], "health timeout", 1)
        _integer(item["interval_seconds"], "health interval", item["timeout_seconds"])
        if item["id"] in health_ids:
            raise ProductBundleError("Runtime health ids must be unique.")
        health_ids.add(item["id"])
    environment_names = set()
    for item in _list(value.get("environment", []), "environment", minimum=0):
        _shape(item, "environment requirement", {"name", "owner", "purpose", "required", "secret"}, {"environments"})
        _env(item["name"])
        _id(item["owner"], "environment owner")
        _text(item["purpose"], "environment purpose")
        _boolean(item["required"], "environment required")
        _boolean(item["secret"], "environment secret")
        if "environments" in item:
            _unique_strings(item["environments"], "environments")
        if item["name"] in environment_names:
            raise ProductBundleError("Runtime environment names must be unique.")
        environment_names.add(item["name"])
    secret_names = set()
    for item in _list(value.get("secret_references", []), "secret references", minimum=0):
        _shape(item, "secret reference", {"name", "owner", "purpose", "required"})
        _env(item["name"])
        _id(item["owner"], "secret owner")
        _text(item["purpose"], "secret purpose")
        _boolean(item["required"], "secret required")
        if item["name"] in secret_names:
            raise ProductBundleError("Runtime secret reference names must be unique.")
        secret_names.add(item["name"])
    provider_ids = set()
    for item in _list(value.get("providers", []), "providers", minimum=0):
        _shape(item, "provider", {"id", "owner", "required"})
        _id(item["id"], "provider id")
        _id(item["owner"], "provider owner")
        _boolean(item["required"], "provider required")
        if item["id"] in provider_ids:
            raise ProductBundleError("Runtime provider ids must be unique.")
        provider_ids.add(item["id"])
    _validate_datasets(value["data"], "runtime data")
    lifecycle = value["lifecycle"]
    _shape(lifecycle, "lifecycle", {"startup", "shutdown_signal", "shutdown_timeout_seconds"})
    if lifecycle["startup"] not in {"single-process", "ordered-processes"} or lifecycle["shutdown_signal"] not in {"SIGTERM", "SIGINT"}:
        raise ProductBundleError("Runtime lifecycle is invalid.")
    _integer(lifecycle["shutdown_timeout_seconds"], "shutdown timeout", 1, 900)


def _validate_release(value: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "kind", "manifest_digest", "release_id", "product_id",
        "stack", "provenance", "artifacts", "capabilities", "compatibility", "rollout",
    }
    _shape(value, "release manifest", required, {"configuration", "migrations"})
    _constant(value, "schema_version", "product.release-manifest/v1")
    _constant(value, "kind", "product.release-manifest")
    _digest(value["manifest_digest"], "manifest_digest")
    _id(value["release_id"], "release_id")
    _id(value["product_id"], "product_id")
    _validate_stack(value["stack"])
    provenance = value["provenance"]
    _shape(provenance, "release provenance", {"plan_digest", "product_spec_digest", "registry_digest"})
    for name in ("plan_digest", "product_spec_digest", "registry_digest"):
        _digest(provenance[name], name)
    artifact_ids = set()
    for item in _list(value["artifacts"], "artifacts"):
        _shape(item, "release artifact", {"id", "kind", "path", "digest", "size_bytes", "build_digest", "platform"})
        _id(item["id"], "artifact id")
        if item["id"] in artifact_ids:
            raise ProductBundleError("Release artifact ids must be unique.")
        artifact_ids.add(item["id"])
        if item["kind"] not in {"executable", "archive", "static"}:
            raise ProductBundleError("Release artifact kind is invalid.")
        _safe_path(item["path"], "artifact path")
        _digest(item["digest"], "artifact digest")
        _digest(item["build_digest"], "build digest")
        _integer(item["size_bytes"], "artifact size", 1, MAX_ARTIFACT_BYTES)
        target = item["platform"]
        _shape(target, "artifact platform", {"os", "arch", "cgo_enabled", "toolchain"})
        if target["os"] not in {"darwin", "linux", "windows", "freebsd"}:
            raise ProductBundleError("Artifact operating system is invalid.")
        _text(target["arch"], "artifact architecture")
        _boolean(target["cgo_enabled"], "cgo_enabled")
        _text(target["toolchain"], "artifact toolchain")
    configuration = {}
    for item in _list(value.get("configuration", []), "configuration", minimum=0):
        _shape(item, "release configuration", {"name", "required", "secret"})
        _env(item["name"])
        _boolean(item["required"], "configuration required")
        _boolean(item["secret"], "configuration secret")
        if item["name"] in configuration:
            raise ProductBundleError("Release configuration names must be unique.")
        configuration[item["name"]] = item
    migration_ids = set()
    for item in _list(value.get("migrations", []), "migrations", minimum=0):
        _shape(item, "release migration", {"id", "capability_id", "digest"})
        _id(item["id"], "migration id")
        _id(item["capability_id"], "migration capability id")
        _digest(item["digest"], "migration digest")
        if item["id"] in migration_ids:
            raise ProductBundleError("Release migration ids must be unique.")
        migration_ids.add(item["id"])
    capability_ids = set()
    for item in _list(value["capabilities"], "capabilities", minimum=0):
        _shape(item, "resolved capability", {"id", "version", "manifest_digest", "adapter_id", "adapter_version", "adapter_digest"})
        for name in ("id", "adapter_id"):
            _id(item[name], name)
        for name in ("version", "adapter_version"):
            _version(item[name], name)
        for name in ("manifest_digest", "adapter_digest"):
            _digest(item[name], name)
        if item["id"] in capability_ids:
            raise ProductBundleError("Resolved capability ids must be unique.")
        capability_ids.add(item["id"])
    compatibility = value["compatibility"]
    _shape(compatibility, "release compatibility", {"runtime_requirements", "release_manifest", "recovery_contract"})
    expected = {
        "runtime_requirements": "product.runtime-requirements/v1",
        "release_manifest": "product.release-manifest/v1",
        "recovery_contract": "product.recovery-contract/v1",
    }
    if compatibility != expected:
        raise ProductBundleError("Release compatibility family is invalid.")
    rollout = value["rollout"]
    _shape(rollout, "release rollout", {"strategy", "migration_behavior", "rollback", "preconditions"})
    if rollout["strategy"] not in {"replace", "rolling", "blue-green"} or rollout["migration_behavior"] not in {"forward-only-at-startup", "external-before-start"} or rollout["rollback"] not in {"restore-required", "artifact-only", "not-supported"}:
        raise ProductBundleError("Release rollout policy is invalid.")
    _unique_ids(rollout["preconditions"], "rollout preconditions")


def _validate_recovery(value: Mapping[str, Any]) -> None:
    _shape(value, "recovery contract", {
        "schema_version", "kind", "contract_digest", "product_id",
        "composition_digest", "objectives", "datasets", "backup_order",
        "restore_order", "validation_order",
    })
    _constant(value, "schema_version", "product.recovery-contract/v1")
    _constant(value, "kind", "product.recovery-contract")
    _digest(value["contract_digest"], "contract_digest")
    _id(value["product_id"], "product_id")
    _digest(value["composition_digest"], "composition_digest")
    objectives = value["objectives"]
    _shape(objectives, "recovery objectives", {"rpo_hours", "rto_hours", "retention_days"})
    for name in ("rpo_hours", "rto_hours", "retention_days"):
        _integer(objectives[name], name, 1)
    dataset_ids = _validate_datasets(value["datasets"], "recovery datasets")
    backup = _unique_ids(value["backup_order"], "backup order")
    restore = _unique_ids(value["restore_order"], "restore order")
    if set(backup) != dataset_ids or set(restore) != dataset_ids:
        raise ProductBundleError("Backup and restore order must cover every recovery dataset.")
    validation_ids = []
    for item in _list(value["validation_order"], "validation order"):
        _shape(item, "recovery validation", {"dataset_id", "checks"})
        if item["dataset_id"] not in dataset_ids:
            raise ProductBundleError("Recovery validation references an unknown dataset.")
        _unique_ids(item["checks"], "recovery checks")
        validation_ids.append(item["dataset_id"])
    if len(validation_ids) != len(set(validation_ids)):
        raise ProductBundleError("Recovery validation datasets must be unique.")


def _validate_datasets(value: Any, owner: str) -> set[str]:
    identifiers = set()
    for item in _list(value, owner):
        required = {"id", "owner", "kind", "binding", "consistency", "consistency_group", "backup", "quiescence", "backup_order", "restore_validations"}
        _shape(item, "dataset", required, {"path"})
        _id(item["id"], "dataset id")
        _id(item["owner"], "dataset owner")
        _text(item["kind"], "dataset kind")
        if "path" in item:
            _safe_path(item["path"], "dataset path")
        if item["binding"] not in {"stack-owned", "provider-selected"} or item["backup"] not in {"required", "optional", "excluded"} or item["quiescence"] not in {"application-stop", "provider-snapshot", "none"}:
            raise ProductBundleError("Dataset binding or recovery policy is invalid.")
        _text(item["consistency"], "dataset consistency")
        _id(item["consistency_group"], "consistency group")
        _integer(item["backup_order"], "backup order", 1)
        _unique_ids(item["restore_validations"], "restore validations")
        if item["id"] in identifiers:
            raise ProductBundleError(f"{owner} repeats a dataset id.")
        identifiers.add(item["id"])
    return identifiers


def _validate_cross_document(bundle, runtime, release, recovery) -> None:
    identities = {bundle["product_id"], runtime["product_id"], release["product_id"], recovery["product_id"]}
    compositions = {bundle["composition_digest"], runtime["composition_digest"], recovery["composition_digest"], release["provenance"]["plan_digest"]}
    if len(identities) != 1 or len(compositions) != 1:
        raise ProductBundleError("Operations documents do not share product and composition identity.")
    if bundle["release_id"] != release["release_id"] or runtime["stack"] != release["stack"]:
        raise ProductBundleError("Release identity or resolved stack differs across documents.")
    artifact_ids = {item["id"] for item in release["artifacts"]}
    if any(item["artifact_id"] not in artifact_ids for item in runtime["processes"]):
        raise ProductBundleError("Runtime process references an undeclared release artifact.")
    runtime_configuration = {
        item["name"]: item["secret"] for item in runtime.get("environment", [])
    }
    release_configuration = {
        item["name"]: item["secret"] for item in release.get("configuration", [])
    }
    # Release generation may tighten environment-specific requiredness (for
    # example, production-only integrity keys). Names and secrecy must still be
    # identical across both documents.
    if runtime_configuration != release_configuration:
        raise ProductBundleError("Runtime and release configuration declarations differ.")
    runtime_groups: Dict[str, list[Mapping[str, Any]]] = {}
    for item in runtime["data"]:
        runtime_groups.setdefault(item["consistency_group"], []).append(item)
    recovery_groups = {
        item["consistency_group"]: item for item in recovery["datasets"]
    }
    if (
        len(recovery_groups) != len(recovery["datasets"])
        or set(runtime_groups) != set(recovery_groups)
    ):
        raise ProductBundleError(
            "Runtime and recovery consistency groups differ."
        )
    for group, runtime_items in runtime_groups.items():
        recovery_item = recovery_groups[group]
        if any(
            item["binding"] != recovery_item["binding"]
            for item in runtime_items
        ):
            raise ProductBundleError(
                "Runtime and recovery dataset bindings differ."
            )
        if recovery_item["binding"] == "stack-owned" and any(
            item.get("path") != recovery_item.get("path")
            for item in runtime_items
        ):
            raise ProductBundleError(
                "Runtime and recovery stack-owned dataset paths differ."
            )


def _validate_value_digest(value: Mapping[str, Any], field: str, owner: str) -> None:
    expected = value[field]
    replacement = dict(value)
    replacement[field] = ""
    encoded = json.dumps(replacement, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    # encoding/json escapes these characters even when they occur in strings.
    encoded = encoded.replace(b"<", b"\\u003c").replace(b">", b"\\u003e").replace(b"&", b"\\u0026")
    if _digest_bytes(encoded) != expected:
        raise ProductBundleError(f"{owner.capitalize()} canonical digest mismatch.")


def _contained_file(root: Path, relative: str) -> Path:
    _safe_path(relative, "document path")
    relative_path = PurePosixPath(relative)
    path = root / relative_path
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ProductBundleError("Bundle document escapes its root.") from exc
    cursor = root
    for part in relative_path.parts:
        cursor = cursor / part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ProductBundleError("Bundle document path contains a symlink.")
        except OSError as exc:
            raise ProductBundleError("Bundle document is unavailable.") from exc
    return path


def _shape(value: Any, owner: str, required: set[str], optional: Optional[set[str]] = None) -> None:
    if not isinstance(value, dict):
        raise ProductBundleError(f"{owner.capitalize()} must be an object.")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - (optional or set())
    if missing:
        raise ProductBundleError(f"{owner.capitalize()} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ProductBundleError(f"{owner.capitalize()} has unknown fields: {', '.join(sorted(unknown))}")


def _list(value: Any, owner: str, minimum: int = 1) -> list[Any]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ProductBundleError(f"{owner.capitalize()} must contain at least {minimum} item(s).")
    return value


def _constant(value: Mapping[str, Any], name: str, expected: str) -> None:
    if value[name] != expected:
        raise ProductBundleError(f"{name} must be {expected}.")


def _id(value: Any, owner: str) -> None:
    if not isinstance(value, str) or len(value) > 160 or not _ID.fullmatch(value):
        raise ProductBundleError(f"{owner.capitalize()} is invalid.")


def _version(value: Any, owner: str) -> None:
    if not isinstance(value, str) or len(value) > 80 or not _VERSION.fullmatch(value):
        raise ProductBundleError(f"{owner.capitalize()} is invalid.")


def _env(value: Any) -> None:
    if not isinstance(value, str) or not _ENV.fullmatch(value):
        raise ProductBundleError("Environment variable name is invalid.")


def _digest(value: Any, owner: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ProductBundleError(f"{owner.capitalize()} is invalid.")


def _integer(value: Any, owner: str, minimum: int, maximum: Optional[int] = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise ProductBundleError(f"{owner.capitalize()} is outside its allowed range.")


def _boolean(value: Any, owner: str) -> None:
    if not isinstance(value, bool):
        raise ProductBundleError(f"{owner.capitalize()} must be boolean.")


def _text(value: Any, owner: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProductBundleError(f"{owner.capitalize()} must be non-empty text.")


def _safe_path(value: Any, owner: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\\" in value:
        raise ProductBundleError(f"{owner.capitalize()} is unsafe.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProductBundleError(f"{owner.capitalize()} is unsafe.")


def _unique_strings(value: Any, owner: str) -> Tuple[str, ...]:
    items = _list(value, owner)
    if not all(isinstance(item, str) for item in items) or len(items) != len(set(items)):
        raise ProductBundleError(f"{owner.capitalize()} must contain unique strings.")
    return tuple(items)


def _unique_ids(value: Any, owner: str) -> Tuple[str, ...]:
    items = _unique_strings(value, owner)
    for item in items:
        _id(item, owner)
    return items


def _validate_stack(value: Any) -> None:
    _shape(value, "resolved stack", {"id", "version", "manifest_digest", "blueprint_digest"})
    _id(value["id"], "stack id")
    _version(value["version"], "stack version")
    _digest(value["manifest_digest"], "stack manifest digest")
    _digest(value["blueprint_digest"], "stack blueprint digest")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _normalize_arch(value: str) -> str:
    lowered = value.lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(lowered, lowered)
