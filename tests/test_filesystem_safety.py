from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia import filesystem
from ophelia.filesystem import (
    FilesystemSafetyCode,
    FilesystemSafetyError,
    ManagedRoot,
)


class FilesystemSafetyTests(unittest.TestCase):
    def test_managed_root_holds_descriptor_and_closes_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)

            managed = ManagedRoot.open(root)
            self.assertTrue(stat.S_ISDIR(os.fstat(managed.fileno()).st_mode))
            managed.close()
            managed.close()

            with self.assertRaises(FilesystemSafetyError) as failure:
                managed.mkdir("closed")
            self.assertEqual(FilesystemSafetyCode.CLOSED_ROOT, failure.exception.code)

    def test_root_must_be_existing_safe_non_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            directory = base / "directory"
            directory.mkdir(mode=0o700)
            symlink = base / "link"
            symlink.symlink_to(directory, target_is_directory=True)
            regular = base / "file"
            regular.write_text("not a directory")

            cases = (
                (symlink, FilesystemSafetyCode.SYMLINK_COMPONENT),
                (regular, FilesystemSafetyCode.NOT_DIRECTORY),
                (base / "missing", FilesystemSafetyCode.MISSING_COMPONENT),
            )
            for path, code in cases:
                with self.subTest(path=path):
                    with self.assertRaises(FilesystemSafetyError) as failure:
                        ManagedRoot.open(path)
                    self.assertEqual(code, failure.exception.code)

            directory.chmod(0o770)
            with self.assertRaises(FilesystemSafetyError) as failure:
                ManagedRoot.open(directory)
            self.assertEqual(FilesystemSafetyCode.UNSAFE_PARENT, failure.exception.code)

    def test_destinations_reject_ambiguous_or_escaping_paths(self) -> None:
        invalid_paths = (
            "",
            "/absolute",
            ".",
            "..",
            "./file",
            "dir/../file",
            "dir//file",
            "dir/",
            "control\x01name",
            "control\x7fname",
            b"bytes",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            with ManagedRoot(root) as managed:
                for path in invalid_paths:
                    with self.subTest(path=path):
                        with self.assertRaises(FilesystemSafetyError) as failure:
                            managed.write_bytes(path, b"blocked")
                        self.assertEqual(
                            FilesystemSafetyCode.INVALID_PATH,
                            failure.exception.code,
                        )
            self.assertEqual([], list(root.iterdir()))

    def test_intermediate_and_final_destination_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            outside = base / "outside"
            root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            (root / "parent-link").symlink_to(outside, target_is_directory=True)
            (root / "file-link").symlink_to(outside / "escaped")

            with ManagedRoot(root) as managed:
                with self.assertRaises(FilesystemSafetyError) as intermediate:
                    managed.write_bytes("parent-link/escaped", b"blocked")
                with self.assertRaises(FilesystemSafetyError) as final:
                    managed.write_bytes("file-link", b"blocked")

            self.assertEqual(
                FilesystemSafetyCode.SYMLINK_COMPONENT,
                intermediate.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.SYMLINK_COMPONENT,
                final.exception.code,
            )
            self.assertFalse((outside / "escaped").exists())

    def test_writable_destination_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            writable = root / "writable"
            writable.mkdir(mode=0o700)
            writable.chmod(0o770)

            with ManagedRoot(root) as managed:
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("writable/file", b"blocked")

            self.assertEqual(FilesystemSafetyCode.UNSAFE_PARENT, failure.exception.code)
            self.assertFalse((writable / "file").exists())

    def test_mkdir_is_descriptor_relative_and_uses_exact_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            parent = root / "parent"
            root.mkdir(mode=0o700)
            parent.mkdir(mode=0o700)

            with ManagedRoot(root) as managed:
                managed.mkdir("parent/created")
                managed.mkdir("restricted", mode=0o500)

                with self.assertRaises(FilesystemSafetyError) as missing:
                    managed.mkdir("missing/child")
                with self.assertRaises(FilesystemSafetyError) as existing:
                    managed.mkdir("parent/created")
                with self.assertRaises(FilesystemSafetyError) as invalid_mode:
                    managed.mkdir("unsafe-mode", mode=0o777)

            self.assertEqual(0o700, stat.S_IMODE((parent / "created").stat().st_mode))
            self.assertEqual(0o500, stat.S_IMODE((root / "restricted").stat().st_mode))
            self.assertEqual(
                FilesystemSafetyCode.MISSING_COMPONENT,
                missing.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.INVALID_DESTINATION,
                existing.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.INVALID_MODE,
                invalid_mode.exception.code,
            )

    def test_write_bytes_atomically_creates_and_replaces_with_exact_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            destination = root / "file"
            destination.write_bytes(b"old")
            destination.chmod(0o644)
            observations = []

            def hook(event: str, path: str) -> None:
                if event == "before_commit":
                    observations.append(
                        (
                            path,
                            destination.read_bytes(),
                            [entry.name for entry in root.glob(".ophelia-tmp-*")],
                        )
                    )

            with ManagedRoot(root, _test_hook=hook) as managed:
                managed.write_bytes("file", b"new")
                managed.write_bytes("empty", b"")

            self.assertEqual([("file", b"old", observations[0][2])], observations[:1])
            self.assertEqual(1, len(observations[0][2]))
            self.assertEqual(b"new", destination.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))
            self.assertEqual(b"", (root / "empty").read_bytes())
            self.assertEqual(0o600, stat.S_IMODE((root / "empty").stat().st_mode))
            self.assertEqual([], list(root.glob(".ophelia-tmp-*")))

    def test_replace_bytes_requires_an_existing_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            (root / "file").write_bytes(b"old")
            (root / "directory").mkdir()

            with ManagedRoot(root) as managed:
                managed.replace_bytes("file", b"new")
                with self.assertRaises(FilesystemSafetyError) as missing:
                    managed.replace_bytes("missing", b"new")
                with self.assertRaises(FilesystemSafetyError) as directory:
                    managed.write_bytes("directory", b"new")
                with self.assertRaises(FilesystemSafetyError) as mode:
                    managed.write_bytes("mode", b"new", mode=0o644)

            self.assertEqual(b"new", (root / "file").read_bytes())
            self.assertEqual(
                FilesystemSafetyCode.INVALID_DESTINATION,
                missing.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.INVALID_DESTINATION,
                directory.exception.code,
            )
            self.assertEqual(FilesystemSafetyCode.INVALID_MODE, mode.exception.code)

    def test_copy_in_streams_regular_source_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            root.mkdir(mode=0o700)
            source = base / "source"
            source.write_bytes((b"copy-data-" * 20000) + b"end")
            source.chmod(0o777)
            source_link = base / "source-link"
            source_link.symlink_to(source)

            with ManagedRoot(root) as managed:
                managed.copy_in(source, "copied")
                with self.assertRaises(FilesystemSafetyError) as symlink:
                    managed.copy_in(source_link, "blocked-link")
                with self.assertRaises(FilesystemSafetyError) as directory:
                    managed.copy_in(base, "blocked-directory")

            self.assertEqual(source.read_bytes(), (root / "copied").read_bytes())
            self.assertEqual(0o600, stat.S_IMODE((root / "copied").stat().st_mode))
            self.assertEqual(
                FilesystemSafetyCode.INVALID_SOURCE,
                symlink.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.INVALID_SOURCE,
                directory.exception.code,
            )
            self.assertFalse((root / "blocked-link").exists())
            self.assertFalse((root / "blocked-directory").exists())

    def test_parent_swap_to_symlink_before_mutation_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            outside = base / "outside"
            parent = root / "parent"
            leaf = parent / "leaf"
            held = root / "parent-held"
            root.mkdir(mode=0o700)
            leaf.mkdir(parents=True, mode=0o700)
            parent.chmod(0o700)
            leaf.chmod(0o700)
            (outside / "leaf").mkdir(parents=True, mode=0o700)
            swapped = False

            def hook(event: str, path: str) -> None:
                nonlocal swapped
                if event == "parent_opened" and not swapped:
                    parent.rename(held)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True

            with ManagedRoot(root, _test_hook=hook) as managed:
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.mkdir("parent/leaf/escaped")

            self.assertEqual(FilesystemSafetyCode.RACE_DETECTED, failure.exception.code)
            self.assertFalse((outside / "leaf" / "escaped").exists())
            self.assertFalse((held / "leaf" / "escaped").exists())

    def test_parent_swap_to_symlink_before_commit_cleans_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            outside = base / "outside"
            parent = root / "parent"
            leaf = parent / "leaf"
            held = root / "parent-held"
            root.mkdir(mode=0o700)
            leaf.mkdir(parents=True, mode=0o700)
            parent.chmod(0o700)
            leaf.chmod(0o700)
            (outside / "leaf").mkdir(parents=True, mode=0o700)
            (leaf / "file").write_bytes(b"old")
            swapped = False

            def hook(event: str, path: str) -> None:
                nonlocal swapped
                if event == "before_commit" and not swapped:
                    parent.rename(held)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True

            with ManagedRoot(root, _test_hook=hook) as managed:
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("parent/leaf/file", b"new")

            self.assertEqual(FilesystemSafetyCode.RACE_DETECTED, failure.exception.code)
            self.assertFalse((outside / "leaf" / "file").exists())
            self.assertEqual(b"old", (held / "leaf" / "file").read_bytes())
            self.assertEqual([], list((held / "leaf").glob(".ophelia-tmp-*")))

    def test_platform_without_descriptor_relative_operations_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)

            with mock.patch.object(filesystem.os, "supports_dir_fd", frozenset()):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    ManagedRoot(root)

            self.assertEqual(
                FilesystemSafetyCode.UNSUPPORTED_PLATFORM,
                failure.exception.code,
            )

    def test_fsync_failure_before_commit_preserves_old_destination_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            destination = root / "file"
            destination.write_bytes(b"old")

            with ManagedRoot(root) as managed, mock.patch.object(
                filesystem.os,
                "fsync",
                side_effect=OSError(errno.EIO, "sync failed"),
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("file", b"new")

            self.assertEqual(
                FilesystemSafetyCode.DURABILITY_UNAVAILABLE,
                failure.exception.code,
            )
            self.assertEqual(b"old", destination.read_bytes())
            self.assertEqual([], list(root.glob(".ophelia-tmp-*")))

    def test_parent_fsync_failure_reports_possible_visible_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            destination = root / "file"
            destination.write_bytes(b"old")
            calls = 0

            def fsync(_descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "directory sync failed")

            with ManagedRoot(root) as managed, mock.patch.object(
                filesystem.os,
                "fsync",
                side_effect=fsync,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("file", b"new")

            self.assertEqual(
                FilesystemSafetyCode.DURABILITY_UNAVAILABLE,
                failure.exception.code,
            )
            self.assertEqual(b"new", destination.read_bytes())
            self.assertEqual([], list(root.glob(".ophelia-tmp-*")))


if __name__ == "__main__":
    unittest.main()
