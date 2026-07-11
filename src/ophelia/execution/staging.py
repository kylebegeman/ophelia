"""Operation-scoped staging for read-only planning and confirmed apply."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Tuple

from ..manifest import Manifest
from ..runtime import (
    bundle_hash,
    bundle_support_paths,
    copy_staged_static_source,
    copy_staged_support_files,
    deployment_baseline_digest,
    sync_bundle_static_source,
    sync_bundle_support_files,
    write_bundle,
)


_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class StagingError(RuntimeError):
    """Raised when operation-local candidate or evidence is unsafe or invalid."""


@dataclass(frozen=True)
class OperationStaging:
    operation_id: str
    root: Path
    candidate: Path
    evidence: Path

    @classmethod
    def create(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        _validate_operation_id(operation_id)
        runtime_root, staging_base = _safe_staging_base(runtime_root, create=True)
        root = staging_base / operation_id
        try:
            root.mkdir(mode=0o700, exist_ok=False)
            candidate = root / "candidate"
            evidence = root / "evidence"
            candidate.mkdir(mode=0o700)
            evidence.mkdir(mode=0o700)
        except OSError as exc:
            raise StagingError(f"Unable to create plan staging for {operation_id}: {exc}") from exc
        return cls._validated(runtime_root, operation_id, root, candidate, evidence)

    @classmethod
    def open_uploaded(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        staging = cls._open_existing(runtime_root, operation_id)
        if staging.evidence.exists():
            raise StagingError(f"Plan evidence already exists for {operation_id}.")
        try:
            staging.evidence.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            raise StagingError(f"Unable to create plan evidence for {operation_id}: {exc}") from exc
        return cls._validated(
            runtime_root.resolve(strict=False),
            operation_id,
            staging.root,
            staging.candidate,
            staging.evidence,
        )

    @classmethod
    def open_confirmed(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        staging = cls._open_existing(runtime_root, operation_id)
        if not staging.evidence.is_dir():
            raise StagingError(f"Plan evidence is missing for {operation_id}.")
        return cls._validated(
            runtime_root.resolve(strict=False),
            operation_id,
            staging.root,
            staging.candidate,
            staging.evidence,
        )

    @classmethod
    def _open_existing(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        _validate_operation_id(operation_id)
        resolved_runtime, staging_base = _safe_staging_base(runtime_root, create=False)
        root = staging_base / operation_id
        candidate = root / "candidate"
        evidence = root / "evidence"
        if not root.is_dir() or not candidate.is_dir():
            raise StagingError(f"Uploaded plan candidate is missing: {candidate}")
        return cls._validated(resolved_runtime, operation_id, root, candidate, evidence)

    @classmethod
    def _validated(
        cls,
        resolved_runtime: Path,
        operation_id: str,
        root: Path,
        candidate: Path,
        evidence: Path,
    ) -> "OperationStaging":
        staging_base = resolved_runtime / "staging"
        for path, label in (
            (staging_base, "staging"),
            (root, "operation"),
            (candidate, "candidate"),
            (evidence, "evidence"),
        ):
            _reject_symlink(path, label)
            _require_contained(path, staging_base, label)
        return cls(operation_id, root, candidate, evidence)

    def stage_candidate(
        self,
        manifest: Manifest,
        manifest_path: Path,
        bundle: Dict[Path, str],
        *,
        stage_static: bool = True,
    ) -> str:
        self._assert_safe()
        try:
            write_bundle(bundle, self.candidate)
            if manifest_path.name == "manifest.lock.json":
                support_paths = bundle_support_paths(manifest)
                copy_staged_support_files(
                    manifest_path.parent,
                    support_paths,
                    self.candidate,
                    required_paths=bundle_support_paths(manifest, required_only=True),
                )
                if stage_static:
                    copy_staged_static_source(manifest, manifest_path.parent, self.candidate)
            else:
                try:
                    sync_bundle_support_files(manifest, manifest_path, self.candidate)
                except FileNotFoundError as exc:
                    if manifest.environment == "production":
                        raise
                    missing = Path(exc.filename) if exc.filename else None
                    if missing is not None and self.candidate in missing.parents:
                        raise
                    # Preserve read-only reporting for a source input not present yet.
                if stage_static:
                    sync_bundle_static_source(manifest, manifest_path, self.candidate)
            _harden_tree(self.root)
            return tree_digest(self.candidate)
        except (OSError, RuntimeError) as exc:
            raise StagingError(f"Unable to stage plan candidate for {self.operation_id}: {exc}") from exc

    def finalize_uploaded_candidate(self, bundle: Dict[Path, str]) -> str:
        self._assert_safe()
        try:
            write_bundle(bundle, self.candidate)
            _harden_tree(self.root)
            return tree_digest(self.candidate)
        except OSError as exc:
            raise StagingError(
                f"Unable to finalize uploaded plan candidate for {self.operation_id}: {exc}"
            ) from exc

    def write_evidence(self, name: str, content: str) -> tuple[Path, str]:
        self._assert_safe()
        if Path(name).name != name:
            raise StagingError(f"Invalid plan evidence name: {name}")
        target = self.evidence / name
        _require_contained(target, self.evidence, "evidence artifact")
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise StagingError(f"Unable to write plan evidence {target}: {exc}") from exc
        target.chmod(0o600)
        return target, file_digest(target)

    def write_binding(self, payload: Dict[str, object]) -> Path:
        self._assert_safe()
        target = self.root / "plan-binding.json"
        _require_contained(target, self.root, "plan binding")
        try:
            with target.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except OSError as exc:
            raise StagingError(f"Unable to write plan binding {target}: {exc}") from exc
        target.chmod(0o600)
        return target

    def _assert_safe(self) -> None:
        resolved_runtime = self.root.parent.parent
        self._validated(
            resolved_runtime,
            self.operation_id,
            self.root,
            self.candidate,
            self.evidence,
        )


@dataclass(frozen=True)
class ConfirmedStaging:
    staging: OperationStaging
    binding: Dict[str, object]

    @property
    def manifest_path(self) -> Path:
        return self.staging.candidate / "manifest.lock.json"

    @property
    def generated_files(self) -> Tuple[Path, ...]:
        value = self.binding.get("generated_files")
        if not isinstance(value, list):
            raise StagingError("Confirmed plan generated file binding is missing.")
        return tuple(_validated_relative_path(item, "generated file") for item in value)

    @property
    def baseline_digest(self) -> str:
        value = self.binding.get("baseline_digest")
        if not isinstance(value, str) or not value:
            raise StagingError("Confirmed plan live baseline binding is missing.")
        return value

    @property
    def candidate_digest(self) -> str:
        value = self.binding.get("candidate_digest")
        if not isinstance(value, str) or not value:
            raise StagingError("Confirmed plan candidate digest binding is missing.")
        return value

    @property
    def rendered_bundle_hash(self) -> str:
        payload = self.binding.get("confirmation_payload")
        value = payload.get("rendered_bundle_hash") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise StagingError("Confirmed plan rendered bundle binding is missing.")
        return value


def find_confirmed_staging(
    runtime_root: Path,
    app: str,
    confirmation_token: str,
) -> ConfirmedStaging:
    if not confirmation_token:
        raise StagingError("A confirmation token is required.")
    _, staging_base = _safe_staging_base(runtime_root, create=False)
    matches: list[ConfirmedStaging] = []
    try:
        roots = sorted(staging_base.iterdir())
    except OSError as exc:
        raise StagingError(f"Unable to inspect operation staging: {exc}") from exc
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        if (root / "applied.json").exists() or (root / "applied.json").is_symlink():
            continue
        binding_path = root / "plan-binding.json"
        if binding_path.is_symlink() or not binding_path.is_file():
            continue
        try:
            binding = json.loads(binding_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(binding, dict) or binding.get("app") != app:
            continue
        token = binding.get("confirmation_token")
        if not isinstance(token, str) or not hmac.compare_digest(token, confirmation_token):
            continue
        operation_id = binding.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        staging = OperationStaging.open_confirmed(runtime_root, operation_id)
        _verify_binding(staging, binding, confirmation_token)
        matches.append(ConfirmedStaging(staging, binding))
    if len(matches) != 1:
        raise StagingError("Confirmation token does not identify exactly one valid staged plan.")
    return matches[0]


def consume_confirmed_staging(confirmed: ConfirmedStaging) -> Path:
    """Mark a confirmed staging operation as applied exactly once."""

    staging = confirmed.staging
    staging._assert_safe()
    target = staging.root / "applied.json"
    _require_contained(target, staging.root, "applied marker")
    payload = {
        "schema_version": 1,
        "kind": "ophelia.deploy-plan-consumption",
        "operation_id": staging.operation_id,
        "app": confirmed.binding.get("app"),
        "consumed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with target.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        target.chmod(0o600)
    except FileExistsError as exc:
        raise StagingError("Confirmed plan has already been consumed.") from exc
    except OSError as exc:
        raise StagingError(f"Unable to consume confirmed plan {staging.operation_id}: {exc}") from exc
    return target


def confirmation_token(payload: Dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def tree_digest(root: Path) -> str:
    _reject_symlink(root, "digest root")
    digest = hashlib.sha256()
    root_metadata = root.lstat()
    digest.update(b"root\0")
    digest.update(f"{stat.S_IMODE(root_metadata.st_mode):04o}".encode("ascii"))
    digest.update(b"\0")
    for child in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = child.relative_to(root)
        metadata = child.lstat()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"symlink\0")
            digest.update(str(child.readlink()).encode("utf-8"))
            digest.update(b"\0")
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"directory\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
        else:
            raise StagingError(f"Operation staging contains a special file: {child}")
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StagingError(f"Evidence artifact is missing or symlinked: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_binding(
    staging: OperationStaging,
    binding: Dict[str, object],
    supplied_token: str,
) -> None:
    _require_private_tree(staging.root)
    payload = binding.get("confirmation_payload")
    if not isinstance(payload, dict):
        raise StagingError("Staged plan confirmation payload is missing.")
    expected = confirmation_token(payload)
    if not hmac.compare_digest(expected, supplied_token):
        raise StagingError("Staged plan confirmation binding is invalid.")
    if payload.get("operation_id") != staging.operation_id:
        raise StagingError("Staged plan operation identity does not match its directory.")
    if binding.get("app") != payload.get("app") or binding.get("environment") != payload.get("environment"):
        raise StagingError("Staged plan identity does not match its confirmation payload.")
    if binding.get("deploy_metadata") != payload.get("deploy_metadata"):
        raise StagingError("Staged deploy metadata does not match its confirmation payload.")
    if binding.get("policy_digest") != payload.get("policy_digest"):
        raise StagingError("Staged policy digest does not match its confirmation payload.")
    if binding.get("generated_files") != payload.get("generated_files"):
        raise StagingError("Staged generated files do not match their confirmation payload.")
    generated = binding.get("generated_files")
    if not isinstance(generated, list) or not generated:
        raise StagingError("Staged generated file binding is missing.")
    generated_paths = [_validated_relative_path(item, "generated file") for item in generated]
    if len(set(generated_paths)) != len(generated_paths):
        raise StagingError("Staged generated file binding contains duplicates.")
    candidate_digest = binding.get("candidate_digest")
    if candidate_digest != payload.get("candidate_digest"):
        raise StagingError("Staged candidate binding does not match its confirmation payload.")
    if not isinstance(candidate_digest, str) or tree_digest(staging.candidate) != candidate_digest:
        raise StagingError("Staged candidate digest no longer matches the reviewed plan.")
    generated_bundle: Dict[Path, str] = {}
    for relative_path in generated_paths:
        path = staging.candidate / relative_path
        _require_contained(path, staging.candidate, "generated file")
        if path.is_symlink() or not path.is_file():
            raise StagingError(f"Staged generated file is missing or unsafe: {relative_path}")
        try:
            generated_bundle[relative_path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StagingError(f"Unable to read staged generated file: {relative_path}") from exc
    if bundle_hash(generated_bundle) != payload.get("rendered_bundle_hash"):
        raise StagingError("Staged generated bytes no longer match the reviewed bundle hash.")
    evidence = binding.get("evidence")
    if not isinstance(evidence, list):
        raise StagingError("Staged plan evidence binding is missing.")
    verified_digests: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise StagingError("Staged plan evidence binding is malformed.")
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str):
            raise StagingError("Staged plan evidence binding is malformed.")
        path = staging.evidence / name
        _require_contained(path, staging.evidence, "evidence artifact")
        if file_digest(path) != digest:
            raise StagingError(f"Staged evidence digest no longer matches: {name}")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise StagingError(f"Staged evidence mode no longer matches: {name}")
        verified_digests.append(digest)
    if payload.get("evidence_digests") != sorted(verified_digests):
        raise StagingError("Staged evidence set does not match the confirmation payload.")
    created_at = _binding_timestamp(payload.get("confirmation_created_at"), "creation")
    expires_at = _binding_timestamp(payload.get("confirmation_expires_at"), "expiry")
    if expires_at <= created_at:
        raise StagingError("Staged plan confirmation expiry is invalid.")
    if datetime.now(timezone.utc) > expires_at:
        raise StagingError("Staged plan confirmation has expired.")
    baseline_digest = binding.get("baseline_digest")
    if baseline_digest != payload.get("baseline_digest") or not isinstance(baseline_digest, str):
        raise StagingError("Staged live baseline does not match its confirmation payload.")
    try:
        from ..manifest import load_manifest

        manifest = load_manifest(staging.candidate / "manifest.lock.json")
    except (OSError, ValueError) as exc:
        raise StagingError("Staged manifest is unreadable during confirmation.") from exc
    if manifest.app != binding.get("app") or manifest.environment != binding.get("environment"):
        raise StagingError("Staged manifest identity does not match its confirmation binding.")
    runtime_root = staging.root.parent.parent
    if deployment_baseline_digest(manifest, runtime_root) != baseline_digest:
        raise StagingError("Live deployment state changed after this plan was reviewed.")


def _validated_relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise StagingError(f"Staged {label} binding is malformed.")
    path = Path(value)
    if path.is_absolute() or path == Path(".") or ".." in path.parts or path.as_posix() != value:
        raise StagingError(f"Staged {label} binding is malformed: {value}")
    return path


def _binding_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise StagingError(f"Staged plan confirmation {label} timestamp is missing.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StagingError(f"Staged plan confirmation {label} timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StagingError(f"Staged plan confirmation {label} timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _require_private_tree(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise StagingError(f"Operation staging contains a symlink: {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o077:
            raise StagingError(f"Operation staging permissions are no longer private: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            if mode != 0o700:
                raise StagingError(f"Operation staging directory mode no longer matches: {path}")
            continue
        if stat.S_ISREG(metadata.st_mode):
            if mode not in {0o600, 0o700}:
                raise StagingError(f"Operation staging file mode no longer matches: {path}")
            continue
        raise StagingError(f"Operation staging contains a special file: {path}")


def _safe_staging_base(runtime_root: Path, *, create: bool) -> tuple[Path, Path]:
    if runtime_root.is_symlink():
        raise StagingError(f"Runtime root must not be a symlink: {runtime_root}")
    try:
        if create:
            runtime_root.mkdir(parents=True, exist_ok=True)
        resolved_runtime = runtime_root.resolve(strict=True)
    except OSError as exc:
        raise StagingError(f"Unable to resolve runtime root {runtime_root}: {exc}") from exc
    staging_base = resolved_runtime / "staging"
    _reject_symlink(staging_base, "staging")
    try:
        if create:
            staging_base.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise StagingError(f"Unable to create staging root {staging_base}: {exc}") from exc
    if not staging_base.is_dir():
        raise StagingError(f"Staging root is missing: {staging_base}")
    _reject_symlink(staging_base, "staging")
    _require_contained(staging_base, resolved_runtime, "staging")
    try:
        staging_base.chmod(0o700)
    except OSError as exc:
        raise StagingError(f"Unable to secure staging root {staging_base}: {exc}") from exc
    return resolved_runtime, staging_base


def _harden_tree(root: Path) -> None:
    """Remove group/other access while preserving owner execute on regular files."""

    try:
        root.chmod(0o700)
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                path.chmod(0o700)
                continue
            if stat.S_ISREG(metadata.st_mode):
                owner_execute = stat.S_IMODE(metadata.st_mode) & stat.S_IXUSR
                path.chmod(0o600 | owner_execute)
                continue
            raise StagingError(f"Operation staging contains a special file: {path}")
    except OSError as exc:
        raise StagingError(f"Unable to secure operation staging {root}: {exc}") from exc


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise StagingError(f"{label.capitalize()} path must not be a symlink: {path}")


def _require_contained(path: Path, base: Path, label: str) -> None:
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(base.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise StagingError(f"{label.capitalize()} path escapes staging containment: {path}") from exc


def _validate_operation_id(value: str) -> None:
    if not _OPERATION_ID.fullmatch(value) or value in {".", ".."}:
        raise StagingError(f"Invalid operation id: {value!r}")
