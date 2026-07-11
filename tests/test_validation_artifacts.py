from __future__ import annotations

import dataclasses
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from ophelia.validation import (
    ArchiveInspectionError,
    ArchiveLimits,
    artifacts,
    inspect_archive,
)


class ArchiveInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _tar(
        self,
        members: list[tuple[tarfile.TarInfo, bytes]],
        *,
        name: str = "bundle.tar",
        mode: str = "w",
        format: int = tarfile.PAX_FORMAT,
    ) -> Path:
        path = self.root / name
        with tarfile.open(path, mode=mode, format=format) as archive:
            for info, payload in members:
                if info.isreg():
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    archive.addfile(info)
        return path

    @staticmethod
    def _file(name: str, payload: bytes = b"payload") -> tuple[tarfile.TarInfo, bytes]:
        return tarfile.TarInfo(name), payload

    @staticmethod
    def _directory(name: str) -> tuple[tarfile.TarInfo, bytes]:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        return info, b""

    def assert_rejected(
        self,
        path: Path,
        code: str,
        limits: Optional[ArchiveLimits] = None,
    ) -> ArchiveInspectionError:
        with self.assertRaises(ArchiveInspectionError) as raised:
            inspect_archive(path, limits)
        self.assertEqual(code, raised.exception.code)
        self.assertLessEqual(len(raised.exception.message), 240)
        return raised.exception

    def test_inspects_regular_files_and_directories_without_extracting(self) -> None:
        path = self._tar(
            [
                self._directory("app/"),
                self._file("app/config.json", b"{}"),
                self._file("./app/data.txt", b"abc"),
            ]
        )
        with patch.object(
            tarfile.TarFile,
            "extractall",
            side_effect=AssertionError("must not extract"),
        ), patch.object(
            tarfile.TarFile,
            "extract",
            side_effect=AssertionError("must not extract"),
        ):
            result = inspect_archive(path)

        self.assertEqual(path.stat().st_size, result.source_bytes)
        self.assertEqual(3, result.member_count)
        self.assertEqual(5, result.total_unpacked_bytes)
        self.assertEqual(
            ("app", "app/config.json", "app/data.txt"),
            tuple(member.path for member in result.members),
        )
        self.assertEqual(("directory", "file", "file"), tuple(member.kind for member in result.members))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.member_count = 4  # type: ignore[misc]

    def test_rejects_absolute_drive_unc_backslash_and_traversal_paths(self) -> None:
        cases = (
            ("/etc/passwd", "archive_member_path_absolute"),
            ("//server/share", "archive_member_path_absolute"),
            ("C:/Windows/file", "archive_member_path_absolute"),
            (r"folder\file", "archive_member_path_backslash"),
            ("../escape", "archive_member_path_invalid"),
            ("a/../escape", "archive_member_path_invalid"),
            ("a//b", "archive_member_path_invalid"),
            ("././a", "archive_member_path_invalid"),
            ("line\nfeed", "archive_member_path_control"),
        )
        for index, (name, code) in enumerate(cases):
            with self.subTest(name=name):
                path = self._tar([self._file(name)], name="case-%d.tar" % index)
                self.assert_rejected(path, code)

    def test_rejects_duplicate_normalized_and_unicode_destinations(self) -> None:
        duplicate = self._tar([self._file("a"), self._file("./a")])
        self.assert_rejected(duplicate, "archive_member_duplicate")

        unicode_duplicate = self._tar(
            [self._file("cafe\u0301"), self._file("caf\u00e9")],
            name="unicode.tar",
        )
        self.assert_rejected(unicode_duplicate, "archive_member_duplicate")

    def test_rejects_file_and_descendant_path_conflicts_in_both_orders(self) -> None:
        for index, members in enumerate(
            (
                [self._file("a"), self._file("a/b")],
                [self._file("a/b"), self._file("a")],
            )
        ):
            with self.subTest(order=index):
                path = self._tar(members, name="conflict-%d.tar" % index)
                self.assert_rejected(path, "archive_member_path_conflict")

    def test_rejects_links_devices_fifos_and_unknown_special_members(self) -> None:
        cases = (
            (tarfile.SYMTYPE, "archive_member_symlink"),
            (tarfile.LNKTYPE, "archive_member_hardlink"),
            (tarfile.CHRTYPE, "archive_member_device"),
            (tarfile.BLKTYPE, "archive_member_device"),
            (tarfile.FIFOTYPE, "archive_member_fifo"),
            (tarfile.CONTTYPE, "archive_member_special"),
        )
        for index, (member_type, code) in enumerate(cases):
            with self.subTest(member_type=member_type):
                info = tarfile.TarInfo("special-%d" % index)
                info.type = member_type
                info.linkname = "target"
                path = self._tar([(info, b"")], name="special-%d.tar" % index)
                self.assert_rejected(path, code)

    def test_rejects_sparse_metadata(self) -> None:
        info = tarfile.TarInfo("sparse.bin")
        info.pax_headers = {"GNU.sparse.map": "0,1"}
        path = self._tar([(info, b"x")], name="sparse.tar")
        self.assert_rejected(path, "archive_member_sparse")

    def test_enforces_source_member_total_count_path_and_depth_quotas(self) -> None:
        source = self._tar([self._file("a", b"1234")], name="source.tar")
        self.assert_rejected(
            source,
            "archive_source_size_exceeded",
            ArchiveLimits(max_source_bytes=1),
        )
        self.assert_rejected(
            source,
            "archive_member_size_exceeded",
            ArchiveLimits(max_member_bytes=3),
        )

        total = self._tar(
            [self._file("a", b"123"), self._file("b", b"456")],
            name="total.tar",
        )
        self.assert_rejected(
            total,
            "archive_total_size_exceeded",
            ArchiveLimits(max_total_unpacked_bytes=5),
        )
        self.assert_rejected(
            total,
            "archive_member_count_exceeded",
            ArchiveLimits(max_members=1),
        )

        long_path = self._tar([self._file("abcdef")], name="long.tar")
        self.assert_rejected(
            long_path,
            "archive_member_path_too_long",
            ArchiveLimits(max_path_bytes=5),
        )
        deep_path = self._tar([self._file("a/b/c")], name="deep.tar")
        self.assert_rejected(
            deep_path,
            "archive_member_path_too_deep",
            ArchiveLimits(max_path_depth=2),
        )

    def test_enforces_declared_expansion_ratio_for_compressed_tar(self) -> None:
        path = self._tar(
            [self._file("repeated.bin", b"a" * 32_768)],
            name="ratio.tar.gz",
            mode="w:gz",
        )
        self.assert_rejected(
            path,
            "archive_expansion_ratio_exceeded",
            ArchiveLimits(max_expansion_ratio=2),
        )

    def test_rejects_malformed_truncated_and_unsupported_zstd_archives(self) -> None:
        malformed = self.root / "malformed.tar"
        malformed.write_bytes(b"not a tar archive")
        self.assert_rejected(malformed, "archive_malformed")

        valid = self._tar([self._file("payload", b"x" * 4096)], name="valid.tar")
        truncated = self.root / "truncated.tar"
        truncated.write_bytes(valid.read_bytes()[:700])
        self.assert_rejected(truncated, "archive_malformed")

        zstd = self.root / "bundle.tar.zst"
        zstd.write_bytes(b"\x28\xb5\x2f\xfd" + b"not-inspected")
        self.assert_rejected(zstd, "archive_compression_unsupported")

    def test_rejects_symlink_sources(self) -> None:
        target = self._tar([self._file("a")], name="target.tar")
        link = self.root / "link.tar"
        link.symlink_to(target)
        self.assert_rejected(link, "archive_source_open_failed")

    def test_detects_same_inode_mutation_during_inspection(self) -> None:
        path = self._tar([self._file("a", b"payload")], name="mutable.tar")
        original = artifacts._inspect_open_source

        def inspect_then_mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            with path.open("ab") as output:
                output.write(b"mutation")
                output.flush()
                os.fsync(output.fileno())
            return result

        with patch.object(artifacts, "_inspect_open_source", inspect_then_mutate):
            self.assert_rejected(path, "archive_source_mutated")

    def test_rejects_invalid_limit_values_and_bounds_diagnostics(self) -> None:
        path = self._tar([self._file("a")])
        self.assert_rejected(
            path,
            "invalid_archive_limits",
            ArchiveLimits(max_members=True),
        )
        error = ArchiveInspectionError("bounded", "secret " * 1000)
        self.assertEqual("bounded", error.code)
        self.assertLessEqual(len(error.message), 240)


if __name__ == "__main__":
    unittest.main()
