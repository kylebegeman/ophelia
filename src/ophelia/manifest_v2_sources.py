"""Safe, deterministic source staging for manifest v2 revision bundles."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Iterator, Tuple

from .manifest_v2 import ManifestV2, WorkloadV2
from .domain import canonical_digest


class ManifestV2SourceError(ValueError):
    """A manifest source input is missing, unsafe, or collides with the bundle."""


_GENERATED_ROOTS = {
    "artifact-lock.json",
    "caddy",
    "compose.yml",
    "manifest.lock.json",
    "revision.json",
    "secret-refs.json",
    "support",
}


def support_file_target(
    workload: WorkloadV2,
    index: int,
    source: str,
    *,
    service_name: str | None = None,
) -> Path:
    """Return the stable bundle path used by Compose for one env file."""

    return (
        Path("support")
        / (service_name or workload.name)
        / ("%02d-%s" % (index, Path(source).name))
    )


def mounted_file_target(
    workload: WorkloadV2,
    index: int,
    source: str,
    *,
    service_name: str | None = None,
) -> Path:
    """Return the stable bundle path used by one read-only support-file mount."""

    return (
        Path("support")
        / (service_name or workload.name)
        / "files"
        / ("%02d-%s" % (index, Path(source).name))
    )


def stage_manifest_v2_sources(
    manifest: ManifestV2,
    manifest_path: Path,
    output_root: Path,
) -> str:
    """Copy static artifacts, env files, and support files without following links."""

    source_root = Path(manifest_path).expanduser().parent.resolve(strict=True)
    output_root = Path(output_root)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise ManifestV2SourceError("Manifest v2 output root must be a real directory.")

    static_targets: list[Path] = []
    for artifact in manifest.artifacts:
        if artifact.static_root is None:
            continue
        relative = _safe_relative(artifact.static_root, "static artifact root")
        if not relative.parts or relative.parts[0] in _GENERATED_ROOTS:
            raise ManifestV2SourceError(
                "Static artifact root collides with Ophelia revision metadata."
            )
        if any(_overlaps(relative, other) for other in static_targets):
            raise ManifestV2SourceError("Static artifact roots may not overlap.")
        static_targets.append(relative)
        source = _trusted_source(source_root, relative, require_directory=True)
        _copy_tree(source, output_root / relative, allowed_output=output_root)

    for service_name, workload in _runtime_workloads(manifest):
        for index, raw_source in enumerate(workload.env_files):
            relative = _safe_relative(raw_source, "env file")
            source = _trusted_source(source_root, relative, require_directory=False)
            target = output_root / support_file_target(
                workload,
                index,
                raw_source,
                service_name=service_name,
            )
            _copy_file(source, target, allowed_output=output_root)
        for index, raw_source in enumerate(item.source for item in workload.file_mounts):
            relative = _safe_relative(raw_source, "support file")
            source = _trusted_source(source_root, relative, require_directory=False)
            target = output_root / mounted_file_target(
                workload,
                index,
                raw_source,
                service_name=service_name,
            )
            _copy_file(source, target, allowed_output=output_root)
    return manifest_v2_sources_digest(manifest, output_root)


def manifest_v2_sources_digest(manifest: ManifestV2, staged_root: Path) -> str:
    """Digest every staged source byte at the exact runtime-relative path."""

    digest = hashlib.sha256()
    paths: list[Path] = []
    for artifact in manifest.artifacts:
        if artifact.static_root is not None:
            root = Path(staged_root) / _safe_relative(
                artifact.static_root, "static artifact root"
            )
            paths.extend([root, *sorted(root.rglob("*"))])
    for service_name, workload in _runtime_workloads(manifest):
        for index, source in enumerate(workload.env_files):
            paths.append(
                Path(staged_root)
                / support_file_target(
                    workload,
                    index,
                    source,
                    service_name=service_name,
                )
            )
        for index, source in enumerate(item.source for item in workload.file_mounts):
            paths.append(
                Path(staged_root)
                / mounted_file_target(
                    workload,
                    index,
                    source,
                    service_name=service_name,
                )
            )
    for path in sorted(set(paths), key=lambda item: item.relative_to(staged_root).as_posix()):
        relative = path.relative_to(staged_root)
        metadata = path.lstat()
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            raise ManifestV2SourceError("Staged manifest sources may not contain symbolic links.")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"directory\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            _update_digest_from_file(digest, path)
            digest.update(b"\0")
        else:
            raise ManifestV2SourceError("Staged manifest sources contain a special file.")
    return "sha256:" + digest.hexdigest()


def manifest_v2_request_digest(manifest: ManifestV2, manifest_path: Path) -> str:
    """Bind a planning request to manifest content and every declared source byte."""

    source_root = Path(manifest_path).expanduser().parent.resolve(strict=True)
    inputs = []
    for artifact in manifest.artifacts:
        if artifact.static_root is not None:
            relative = _safe_relative(artifact.static_root, "static artifact root")
            source = _trusted_source(source_root, relative, require_directory=True)
            inputs.append(
                {
                    "target": relative.as_posix(),
                    "declared_digest": artifact.digest,
                    "source_digest": _source_tree_digest(source),
                }
            )
    for service_name, workload in _runtime_workloads(manifest):
        for index, raw_source in enumerate(workload.env_files):
            relative = _safe_relative(raw_source, "env file")
            source = _trusted_source(source_root, relative, require_directory=False)
            inputs.append(
                {
                    "target": support_file_target(
                        workload,
                        index,
                        raw_source,
                        service_name=service_name,
                    ).as_posix(),
                    "sha256": _file_sha256(source),
                }
            )
        for index, raw_source in enumerate(item.source for item in workload.file_mounts):
            relative = _safe_relative(raw_source, "support file")
            source = _trusted_source(source_root, relative, require_directory=False)
            inputs.append(
                {
                    "target": mounted_file_target(
                        workload,
                        index,
                        raw_source,
                        service_name=service_name,
                    ).as_posix(),
                    "sha256": _file_sha256(source),
                }
            )
    return canonical_digest(
        {
            "manifest_digest": manifest.canonical_digest(),
            "source_inputs": sorted(inputs, key=lambda item: item["target"]),
        }
    )


def _source_tree_digest(root: Path) -> str:
    """Digest a trusted source tree without following links or special files."""

    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"))]:
        relative = path.relative_to(root)
        metadata = path.lstat()
        digest.update((relative.as_posix() or ".").encode("utf-8") + b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            raise ManifestV2SourceError("Static artifacts may not contain symbolic links.")
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"directory\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            _update_digest_from_file(digest, path)
            digest.update(b"\0")
        else:
            raise ManifestV2SourceError(
                "Static artifacts may contain only files and directories."
            )
    return "sha256:" + digest.hexdigest()


def _runtime_workloads(manifest: ManifestV2) -> Iterator[Tuple[str, WorkloadV2]]:
    for workload in manifest.workloads:
        yield workload.name, workload
    for migration in manifest.migrations:
        yield "migration-" + migration.name, migration.workload


def _safe_relative(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or path == Path(".") or ".." in path.parts:
        raise ManifestV2SourceError("%s must be a safe relative path." % label.capitalize())
    return path


def _trusted_source(root: Path, relative: Path, *, require_directory: bool) -> Path:
    candidate = root / relative
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ManifestV2SourceError("Manifest source input is missing: %s" % relative) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ManifestV2SourceError("Manifest source inputs may not use symbolic links.")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestV2SourceError("Manifest source input escapes its source root.") from exc
    if require_directory and not resolved.is_dir():
        raise ManifestV2SourceError("Static artifact source must be a directory.")
    if not require_directory and not resolved.is_file():
        raise ManifestV2SourceError("Manifest file source must be a regular file.")
    return resolved


def _copy_tree(source: Path, target: Path, *, allowed_output: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ManifestV2SourceError("Manifest source targets must be unique.")
    _require_output(target, allowed_output)
    target.mkdir(mode=0o700, parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ManifestV2SourceError("Static artifacts may not contain symbolic links.")
        relative = path.relative_to(source)
        destination = target / relative
        _require_output(destination, allowed_output)
        metadata = path.stat()
        if stat.S_ISDIR(metadata.st_mode):
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_file(path, destination, allowed_output=allowed_output)
        else:
            raise ManifestV2SourceError("Static artifacts may contain only files and directories.")


def _copy_file(source: Path, target: Path, *, allowed_output: Path) -> None:
    _require_output(target, allowed_output)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise ManifestV2SourceError("Manifest source targets must be unique.")
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
        os.close(source_descriptor)
        raise ManifestV2SourceError("Manifest source file must remain a regular file.")
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(source_descriptor, "rb") as handle:
                source_descriptor = -1
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _update_digest_from_file(digest, path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ManifestV2SourceError("Manifest source file must remain a regular file.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_output(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ManifestV2SourceError("Manifest source target escapes its bundle.") from exc


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
