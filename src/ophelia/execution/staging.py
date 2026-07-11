"""Operation-scoped staging for read-only planning evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from ..manifest import Manifest
from ..runtime import (
    bundle_support_paths,
    copy_staged_static_source,
    copy_staged_support_files,
    sync_bundle_static_source,
    sync_bundle_support_files,
    write_bundle,
)


_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class StagingError(RuntimeError):
    """Raised when operation-local candidate or evidence cannot be persisted."""


@dataclass(frozen=True)
class OperationStaging:
    operation_id: str
    root: Path
    candidate: Path
    evidence: Path

    @classmethod
    def create(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        _validate_operation_id(operation_id)
        root = runtime_root / "staging" / operation_id
        try:
            root.mkdir(parents=True, exist_ok=False)
            candidate = root / "candidate"
            evidence = root / "evidence"
            candidate.mkdir()
            evidence.mkdir()
        except OSError as exc:
            raise StagingError(f"Unable to create plan staging for {operation_id}: {exc}") from exc
        return cls(operation_id, root, candidate, evidence)

    @classmethod
    def open_uploaded(cls, runtime_root: Path, operation_id: str) -> "OperationStaging":
        _validate_operation_id(operation_id)
        root = runtime_root / "staging" / operation_id
        candidate = root / "candidate"
        evidence = root / "evidence"
        if not candidate.is_dir():
            raise StagingError(f"Uploaded plan candidate is missing: {candidate}")
        try:
            evidence.mkdir(exist_ok=False)
        except OSError as exc:
            raise StagingError(f"Unable to create plan evidence for {operation_id}: {exc}") from exc
        return cls(operation_id, root, candidate, evidence)

    def stage_candidate(
        self,
        manifest: Manifest,
        manifest_path: Path,
        bundle: Dict[Path, str],
        *,
        stage_static: bool = True,
    ) -> str:
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
                    missing = Path(exc.filename) if exc.filename else None
                    if missing is not None and self.candidate in missing.parents:
                        raise
                    # Planning preserves its existing ability to describe a candidate
                    # whose source repository does not contain a required input yet.
                if stage_static:
                    sync_bundle_static_source(manifest, manifest_path, self.candidate)
            return tree_digest(self.candidate)
        except (OSError, RuntimeError) as exc:
            raise StagingError(f"Unable to stage plan candidate for {self.operation_id}: {exc}") from exc

    def finalize_uploaded_candidate(self, bundle: Dict[Path, str]) -> str:
        """Finalize generated files after an upload, leaving support/static inputs intact."""
        try:
            write_bundle(bundle, self.candidate)
            return tree_digest(self.candidate)
        except OSError as exc:
            raise StagingError(
                f"Unable to finalize uploaded plan candidate for {self.operation_id}: {exc}"
            ) from exc

    def write_evidence(self, name: str, content: str) -> Path:
        if Path(name).name != name:
            raise StagingError(f"Invalid plan evidence name: {name}")
        target = self.evidence / name
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise StagingError(f"Unable to write plan evidence {target}: {exc}") from exc
        return target


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = child.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if child.is_symlink():
            digest.update(b"symlink\0")
            digest.update(str(child.readlink()).encode("utf-8"))
            digest.update(b"\0")
        elif child.is_file():
            digest.update(b"file\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _validate_operation_id(value: str) -> None:
    if not _OPERATION_ID.fullmatch(value) or value in {".", ".."}:
        raise StagingError(f"Invalid operation id: {value!r}")
