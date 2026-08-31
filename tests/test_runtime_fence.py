from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.execution.runtime_fence import (
    FENCE_AREA_NAME,
    FENCE_LOCK_NAME,
    FENCE_RECORD_NAME,
    MAX_RECORD_BYTES,
    InvalidRuntimeFence,
    RuntimeFenceRecordError,
    RuntimeFenceRejected,
    RuntimeFenceSafetyError,
    RuntimeSideEffectFence,
)


HOST = "host_example-1"
APP = "demo-service"
ENVIRONMENT = "production"
OWNER = "worker_one"


def _fence(
    root: Path,
    token: int,
    operation: str = "operation_one",
    owner: str = OWNER,
) -> RuntimeSideEffectFence:
    return RuntimeSideEffectFence(
        root,
        HOST,
        APP,
        ENVIRONMENT,
        operation,
        owner,
        token,
    )


def _scope(root: Path) -> Path:
    return root / FENCE_AREA_NAME / HOST / APP / ENVIRONMENT


def _record(root: Path) -> dict:
    return json.loads((_scope(root) / FENCE_RECORD_NAME).read_text())


class RuntimeSideEffectFenceTests(unittest.TestCase):
    def test_monotonic_acceptance_and_same_operation_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with _fence(root, 4) as first:
                self.assertEqual(4, first.accepted_record["fencing_token"])
            with _fence(root, 7, "operation_two"):
                pass
            with _fence(root, 7, "operation_two", "worker_two"):
                pass

            record = _record(root)
            self.assertEqual(7, record["fencing_token"])
            self.assertEqual("operation_two", record["operation_id"])
            self.assertEqual("worker_two", record["owner_id"])

    def test_lower_and_equal_other_operation_are_rejected_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with _fence(root, 73331, "operation_one", "private_owner"):
                pass

            with self.assertRaises(RuntimeFenceRejected) as lower:
                with _fence(root, 73330, "operation_two", "other_owner"):
                    self.fail("stale token entered mutation window")
            with self.assertRaises(RuntimeFenceRejected) as equal:
                with _fence(root, 73331, "operation_two", "other_owner"):
                    self.fail("other operation entered mutation window")

            for failure in (lower.exception, equal.exception):
                message = str(failure)
                self.assertNotIn("73331", message)
                self.assertNotIn("73330", message)
                self.assertNotIn("private_owner", message)
                self.assertNotIn("other_owner", message)
            self.assertEqual("operation_one", _record(root)["operation_id"])

    def test_record_remains_advanced_after_caller_failure(self) -> None:
        class CallerFailure(Exception):
            pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(CallerFailure):
                with _fence(root, 9, "operation_failed"):
                    self.assertEqual(9, _record(root)["fencing_token"])
                    raise CallerFailure()

            self.assertEqual(9, _record(root)["fencing_token"])
            with self.assertRaises(RuntimeFenceRejected):
                with _fence(root, 8, "operation_old"):
                    pass

    def test_thread_contention_holds_permission_for_entire_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_entered = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            second_entered = threading.Event()
            events = []
            failures = []

            def first() -> None:
                try:
                    with _fence(root, 1, "operation_one"):
                        events.append("first-enter")
                        first_entered.set()
                        release_first.wait(5)
                        events.append("first-exit")
                except BaseException as exc:
                    failures.append(exc)

            def second() -> None:
                try:
                    first_entered.wait(5)
                    second_started.set()
                    with _fence(root, 2, "operation_two"):
                        events.append("second-enter")
                        second_entered.set()
                except BaseException as exc:
                    failures.append(exc)

            thread_one = threading.Thread(target=first)
            thread_two = threading.Thread(target=second)
            thread_one.start()
            thread_two.start()
            self.assertTrue(first_entered.wait(5))
            self.assertTrue(second_started.wait(5))
            time.sleep(0.15)
            self.assertFalse(second_entered.is_set())
            release_first.set()
            thread_one.join(5)
            thread_two.join(5)

            self.assertFalse(thread_one.is_alive())
            self.assertFalse(thread_two.is_alive())
            self.assertEqual([], failures)
            self.assertEqual(
                ["first-enter", "first-exit", "second-enter"],
                events,
            )

    def test_separate_process_contention_orders_mutation_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            started = root / "child-started"
            entered = root / "child-entered"
            source_root = Path(__file__).resolve().parents[1] / "src"
            script = """
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from ophelia.execution.runtime_fence import RuntimeSideEffectFence
root = pathlib.Path(sys.argv[2])
started = pathlib.Path(sys.argv[3])
entered = pathlib.Path(sys.argv[4])
started.write_text("started")
with RuntimeSideEffectFence(
    root, "host_example-1", "demo-service", "production",
    "operation_two", "worker_two", 2
):
    entered.write_text("entered")
"""
            process = None
            with _fence(root, 1, "operation_one"):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(source_root),
                        str(root),
                        str(started),
                        str(entered),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                time.sleep(0.2)
                self.assertFalse(entered.exists())
                self.assertIsNone(process.poll())

            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual("", stdout)
            self.assertEqual("", stderr)
            self.assertEqual(0, process.returncode)
            self.assertTrue(entered.exists())
            self.assertEqual(2, _record(root)["fencing_token"])

    def test_symlinked_root_and_managed_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "runtime"
            root.mkdir(mode=0o700)
            root_link = base / "runtime-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(RuntimeFenceSafetyError):
                with _fence(root_link, 1):
                    pass

            outside = base / "outside"
            outside.mkdir()
            (root / FENCE_AREA_NAME).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeFenceSafetyError):
                with _fence(root, 1):
                    pass
            self.assertEqual([], list(outside.iterdir()))

    def test_lock_and_record_reject_symlinks_and_special_files(self) -> None:
        attacks = ("lock-symlink", "lock-fifo", "record-symlink", "record-fifo")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with _fence(root, 1):
                    pass
                scope = _scope(root)
                outside = root / "outside"
                outside.write_text("outside")

                if attack.startswith("lock"):
                    target = scope / FENCE_LOCK_NAME
                else:
                    target = scope / FENCE_RECORD_NAME
                target.unlink()

                if attack.endswith("symlink"):
                    target.symlink_to(outside)
                else:
                    os.mkfifo(target, 0o600)

                with self.assertRaises(RuntimeFenceSafetyError):
                    with _fence(root, 2, "operation_two"):
                        pass
                self.assertEqual("outside", outside.read_text())

    def test_malformed_duplicate_and_oversized_records_fail_closed(self) -> None:
        payloads = (
            b"{not-json",
            b'{"schema_version":1,"schema_version":1}',
            b"x" * (MAX_RECORD_BYTES + 1),
        )
        for payload in payloads:
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with _fence(root, 1):
                    pass
                (_scope(root) / FENCE_RECORD_NAME).write_bytes(payload)
                with self.assertRaises(RuntimeFenceRecordError):
                    with _fence(root, 2, "operation_two"):
                        pass

    def test_private_modes_are_created_and_repaired_under_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous_umask = os.umask(0o777)
            try:
                with _fence(root, 1):
                    pass
            finally:
                os.umask(previous_umask)

            scope = _scope(root)
            directories = [
                root / FENCE_AREA_NAME,
                root / FENCE_AREA_NAME / HOST,
                root / FENCE_AREA_NAME / HOST / APP,
                scope,
            ]
            files = [scope / FENCE_LOCK_NAME, scope / FENCE_RECORD_NAME]
            for directory in directories:
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            for file in files:
                self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))
                file.chmod(0o000)
            for directory in reversed(directories):
                directory.chmod(0o000)

            with _fence(root, 2, "operation_two"):
                pass
            for directory in directories:
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            for file in files:
                self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))

    def test_invalid_identifiers_tokens_and_unsafe_root_fail_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cases = (
                dict(host_id="../host"),
                dict(app="Bad-App"),
                dict(environment="prod/../../x"),
                dict(operation_id="operation/one"),
                dict(owner_id="owner value"),
                dict(fencing_token=0),
                dict(fencing_token=True),
            )
            defaults = {
                "runtime_root": root,
                "host_id": HOST,
                "app": APP,
                "environment": ENVIRONMENT,
                "operation_id": "operation_one",
                "owner_id": OWNER,
                "fencing_token": 1,
            }
            for override in cases:
                with self.subTest(override=override):
                    arguments = dict(defaults)
                    arguments.update(override)
                    with self.assertRaises(InvalidRuntimeFence):
                        RuntimeSideEffectFence(**arguments)
            self.assertFalse((root / FENCE_AREA_NAME).exists())

            root.chmod(0o777)
            with self.assertRaises(RuntimeFenceSafetyError):
                with _fence(root, 1):
                    pass

    def test_managed_directory_and_record_directory_attacks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            area = root / FENCE_AREA_NAME
            area.mkdir()
            (area / HOST).write_text("not a directory")
            with self.assertRaises(RuntimeFenceSafetyError):
                with _fence(root, 1):
                    pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with _fence(root, 1):
                pass
            record = _scope(root) / FENCE_RECORD_NAME
            record.unlink()
            record.mkdir()
            with self.assertRaises(RuntimeFenceSafetyError):
                with _fence(root, 2, "operation_two"):
                    pass


if __name__ == "__main__":
    unittest.main()
