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


TRUSTED_UID = os.geteuid()


class FilesystemSafetyTests(unittest.TestCase):
    def test_managed_root_holds_descriptor_and_closes_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)

            managed = ManagedRoot.open(root, trusted_owner_uid=TRUSTED_UID)
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
                        ManagedRoot.open(path, trusted_owner_uid=TRUSTED_UID)
                    self.assertEqual(code, failure.exception.code)

            directory.chmod(0o770)
            with self.assertRaises(FilesystemSafetyError) as failure:
                ManagedRoot.open(directory, trusted_owner_uid=TRUSTED_UID)
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
            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
                managed.mkdir("parent/created")
                with self.assertRaises(FilesystemSafetyError) as unsupported_mode:
                    managed.mkdir("restricted", mode=0o500)

                with self.assertRaises(FilesystemSafetyError) as missing:
                    managed.mkdir("missing/child")
                with self.assertRaises(FilesystemSafetyError) as existing:
                    managed.mkdir("parent/created")
                with self.assertRaises(FilesystemSafetyError) as invalid_mode:
                    managed.mkdir("unsafe-mode", mode=0o777)

            self.assertEqual(0o700, stat.S_IMODE((parent / "created").stat().st_mode))
            self.assertFalse((root / "restricted").exists())
            self.assertEqual(
                FilesystemSafetyCode.INVALID_MODE,
                unsupported_mode.exception.code,
            )
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

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
                _test_hook=hook,
            ) as managed:
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed:
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

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
                _test_hook=hook,
            ) as managed:
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

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
                _test_hook=hook,
            ) as managed:
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("parent/leaf/file", b"new")

            self.assertEqual(FilesystemSafetyCode.RACE_DETECTED, failure.exception.code)
            self.assertFalse((outside / "leaf" / "file").exists())
            self.assertEqual(b"old", (held / "leaf" / "file").read_bytes())
            self.assertEqual([], list((held / "leaf").glob(".ophelia-tmp-*")))

    def test_trusted_owner_uid_is_explicit_and_privileged_default_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed:
                self.assertEqual(TRUSTED_UID, managed.trusted_owner_uid)
                self.assertIn("no untrusted same-UID", managed.trust_boundary)

            with self.assertRaises(FilesystemSafetyError) as mismatch:
                ManagedRoot(root, trusted_owner_uid=TRUSTED_UID + 1)
            self.assertEqual(
                FilesystemSafetyCode.UNSAFE_OWNER,
                mismatch.exception.code,
            )

            with mock.patch.object(filesystem.os, "geteuid", return_value=0):
                with self.assertRaises(FilesystemSafetyError) as privileged:
                    ManagedRoot(root)
            self.assertEqual(
                FilesystemSafetyCode.UNSAFE_OWNER,
                privileged.exception.code,
            )

    def test_attacker_owned_0700_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            real_fstat = filesystem.os.fstat

            def foreign_fstat(descriptor: int) -> os.stat_result:
                return _stat_with_uid(real_fstat(descriptor), TRUSTED_UID + 1)

            with mock.patch.object(
                filesystem.os,
                "fstat",
                side_effect=foreign_fstat,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    ManagedRoot(root, trusted_owner_uid=TRUSTED_UID)

            self.assertEqual(
                FilesystemSafetyCode.UNSAFE_OWNER,
                failure.exception.code,
            )

    def test_attacker_owned_0700_traversed_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            attacker = root / "attacker"
            root.mkdir(mode=0o700)
            attacker.mkdir(mode=0o700)
            attacker_stat = attacker.stat()
            attacker_identity = (attacker_stat.st_dev, attacker_stat.st_ino)
            real_fstat = filesystem.os.fstat

            def foreign_directory_fstat(descriptor: int) -> os.stat_result:
                value = real_fstat(descriptor)
                if (value.st_dev, value.st_ino) == attacker_identity:
                    return _stat_with_uid(value, TRUSTED_UID + 1)
                return value

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "fstat",
                side_effect=foreign_directory_fstat,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("attacker/file", b"blocked")

            self.assertEqual(
                FilesystemSafetyCode.UNSAFE_OWNER,
                failure.exception.code,
            )
            self.assertFalse((attacker / "file").exists())

    def test_copy_in_rejects_concurrent_same_inode_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            source = base / "source"
            root.mkdir(mode=0o700)
            source.write_bytes(b"a" * (filesystem._COPY_BUFFER_SIZE * 2))
            original_inode = source.stat().st_ino
            real_read = filesystem.os.read
            mutated = False

            def read_then_mutate(descriptor: int, size: int) -> bytes:
                nonlocal mutated
                chunk = real_read(descriptor, size)
                if chunk and not mutated:
                    with source.open("r+b") as stream:
                        stream.write(b"b")
                        stream.flush()
                        os.fsync(stream.fileno())
                    mutated = True
                return chunk

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "read",
                side_effect=read_then_mutate,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.copy_in(source, "copied")

            self.assertTrue(mutated)
            self.assertEqual(original_inode, source.stat().st_ino)
            self.assertEqual(
                FilesystemSafetyCode.SOURCE_CHANGED,
                failure.exception.code,
            )
            self.assertFalse((root / "copied").exists())
            self.assertEqual([], list(root.glob(".ophelia-tmp-*")))

    def test_mkdir_supports_0300_and_restrictive_umask_without_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            staged_modes = []

            def hook(event: str, path: str) -> None:
                if event == "before_commit" and path == "write-only":
                    staged_modes.append(
                        stat.S_IMODE((root / "write-only").stat().st_mode)
                    )

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
                _test_hook=hook,
            ) as managed:
                managed.mkdir("write-only", mode=0o300)
                previous_umask = os.umask(0o777)
                try:
                    managed.mkdir("restrictive-umask", mode=0o700)
                finally:
                    os.umask(previous_umask)

            self.assertEqual([0o300], staged_modes)
            self.assertEqual(
                0o300,
                stat.S_IMODE((root / "write-only").stat().st_mode),
            )
            self.assertEqual(
                0o700,
                stat.S_IMODE((root / "restrictive-umask").stat().st_mode),
            )

    def test_mkdir_does_not_replace_a_concurrently_created_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            real_mkdir = filesystem.os.mkdir
            injected = False

            def competing_mkdir(name, mode=0o777, *, dir_fd=None):
                nonlocal injected
                if name == "contested" and not injected:
                    real_mkdir(name, 0o700, dir_fd=dir_fd)
                    injected = True
                return real_mkdir(name, mode, dir_fd=dir_fd)

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "mkdir",
                side_effect=competing_mkdir,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.mkdir("contested")

            self.assertTrue(injected)
            self.assertEqual(
                FilesystemSafetyCode.INVALID_DESTINATION,
                failure.exception.code,
            )
            self.assertTrue((root / "contested").is_dir())

    def test_copy_source_close_failure_preserves_primary_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "managed"
            outside = base / "outside"
            source = base / "source"
            root.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            source.write_bytes(b"source")
            (root / "blocked").symlink_to(outside / "escaped")
            source_identity = (source.stat().st_dev, source.stat().st_ino)
            real_close = filesystem.os.close
            real_fstat = filesystem.os.fstat

            def close_then_report(descriptor: int) -> None:
                try:
                    value = real_fstat(descriptor)
                    identity = (value.st_dev, value.st_ino)
                except OSError:
                    identity = None
                real_close(descriptor)
                if identity == source_identity:
                    raise OSError(errno.EIO, "source close failed")

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "close",
                side_effect=close_then_report,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.copy_in(source, "blocked")

            self.assertEqual(
                FilesystemSafetyCode.CLEANUP_FAILURE,
                failure.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.SYMLINK_COMPONENT.value,
                failure.exception.primary_error_code,
            )
            self.assertIn("close_copy_source", failure.exception.cleanup_failures)
            self.assertFalse((outside / "escaped").exists())

    def test_mkdir_post_publication_failure_retains_requested_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            injected = FilesystemSafetyError(
                FilesystemSafetyCode.RACE_DETECTED,
                "mkdir",
                "published",
                "injected post-publication failure",
            )

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                ManagedRoot,
                "_post_mutation_revalidate",
                side_effect=injected,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.mkdir("published", mode=0o300)

            self.assertIs(injected, failure.exception)
            self.assertEqual(
                0o300,
                stat.S_IMODE((root / "published").stat().st_mode),
            )

    def test_unlink_cleanup_failure_preserves_primary_error_and_syncs_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            destination = root / "file"
            destination.write_bytes(b"old")
            real_fsync = filesystem.os.fsync

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "replace",
                side_effect=OSError(errno.EIO, "replace failed"),
            ), mock.patch.object(
                filesystem.os,
                "unlink",
                side_effect=OSError(errno.EIO, "unlink failed"),
            ), mock.patch.object(
                filesystem.os,
                "fsync",
                wraps=real_fsync,
            ) as fsync:
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("file", b"new")

            self.assertEqual(
                FilesystemSafetyCode.CLEANUP_FAILURE,
                failure.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.IO_FAILURE.value,
                failure.exception.primary_error_code,
            )
            self.assertIn(
                "unlink_temporary_file",
                failure.exception.cleanup_failures,
            )
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(b"old", destination.read_bytes())
            self.assertEqual(1, len(list(root.glob(".ophelia-tmp-*"))))

    def test_post_mutation_identity_check_detects_same_uid_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)
            destination = root / "file"
            moved = root / "moved"
            real_replace = filesystem.os.replace

            def replace_then_swap(source, target, **kwargs) -> None:
                real_replace(source, target, **kwargs)
                destination.rename(moved)
                destination.write_bytes(b"same-uid replacement")

            with ManagedRoot(
                root,
                trusted_owner_uid=TRUSTED_UID,
            ) as managed, mock.patch.object(
                filesystem.os,
                "replace",
                side_effect=replace_then_swap,
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("file", b"published")

            self.assertEqual(
                FilesystemSafetyCode.RACE_DETECTED,
                failure.exception.code,
            )
            self.assertEqual(b"published", moved.read_bytes())
            self.assertEqual(b"same-uid replacement", destination.read_bytes())

    def test_platform_without_descriptor_relative_operations_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "managed"
            root.mkdir(mode=0o700)

            with mock.patch.object(filesystem.os, "supports_dir_fd", frozenset()):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    ManagedRoot(root, trusted_owner_uid=TRUSTED_UID)

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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed, mock.patch.object(
                filesystem.os,
                "fsync",
                side_effect=OSError(errno.EIO, "sync failed"),
            ):
                with self.assertRaises(FilesystemSafetyError) as failure:
                    managed.write_bytes("file", b"new")

            self.assertEqual(
                FilesystemSafetyCode.CLEANUP_FAILURE,
                failure.exception.code,
            )
            self.assertEqual(
                FilesystemSafetyCode.DURABILITY_UNAVAILABLE.value,
                failure.exception.primary_error_code,
            )
            self.assertIn(
                "fsync_destination_parent_after_cleanup",
                failure.exception.cleanup_failures,
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

            with ManagedRoot(root, trusted_owner_uid=TRUSTED_UID) as managed, mock.patch.object(
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


def _stat_with_uid(value: os.stat_result, uid: int) -> os.stat_result:
    fields = list(value)
    fields[4] = uid
    return os.stat_result(fields)


if __name__ == "__main__":
    unittest.main()
