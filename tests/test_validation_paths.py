from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ophelia.validation import (
    CanonicalValidationError,
    SourceRoot,
    host_path_capability,
    resolve_host_path,
    resolve_source_path,
)


class FilesystemPathValidationTests(unittest.TestCase):
    def test_fixture_manifest_relative_root_stays_within_declared_source(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        suite = repository / "fixtures" / "app-suite"
        root = SourceRoot.from_path(suite)

        result = resolve_source_path(
            root,
            "../apps/static-basic",
            relative_to=suite / "manifests",
            field="static_root",
        )

        self.assertEqual((suite / "apps" / "static-basic").resolve(), result.path)

    def test_rejects_relative_escape_after_normal_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = SourceRoot.from_path(directory)
            with self.assertRaises(CanonicalValidationError) as raised:
                resolve_source_path(root, "../outside")
            self.assertEqual("source_path_escape", raised.exception.code)

    def test_rejects_escape_after_symlink_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root_path = Path(directory)
            link = root_path / "external"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest("symlinks unavailable: %s" % error)

            with self.assertRaises(CanonicalValidationError) as raised:
                resolve_source_path(SourceRoot.from_path(root_path), "external/data")
            self.assertEqual("source_path_escape", raised.exception.code)

    def test_absolute_paths_require_separate_typed_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = SourceRoot.from_path(directory)
            absolute = Path(directory) / "published"

            with self.assertRaises(CanonicalValidationError) as raised:
                resolve_source_path(root, absolute, field="static_root")
            self.assertEqual("absolute_source_path_requires_capability", raised.exception.code)

            capability = host_path_capability([directory])
            accepted = resolve_host_path(capability, absolute, field="static_root")
            self.assertEqual(absolute.resolve(), accepted.path)
            self.assertEqual(Path(directory).resolve(), accepted.allowed_root)

            with self.assertRaises(CanonicalValidationError) as denied:
                resolve_host_path(capability, Path(outside) / "published")
            self.assertEqual("host_path_not_allowed", denied.exception.code)

    def test_allowlist_is_resolved_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability = host_path_capability([directory, directory])
            self.assertEqual((Path(directory).resolve(),), capability.allowed_roots)
            with self.assertRaises(AttributeError):
                capability.allowed_roots.append(Path("/tmp"))  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
