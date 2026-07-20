from __future__ import annotations

import sys
import time
import unittest

from ophelia.execution.subprocesses import ProcessFailure, SubprocessRunner


class SubprocessRunnerTests(unittest.TestCase):
    def test_captures_bounded_output_and_structured_result(self) -> None:
        runner = SubprocessRunner(max_output_bytes=128)
        result = runner.run(
            [sys.executable, "-c", "print('x' * 1000)"],
            timeout_seconds=5,
        )

        self.assertEqual(0, result.exit_code)
        self.assertTrue(result.stdout_truncated)
        self.assertLessEqual(len(result.stdout.encode()), 128)
        self.assertGreater(result.duration_ms, 0)
        self.assertEqual(sys.executable, result.argv[0])

    def test_timeout_terminates_process_group(self) -> None:
        runner = SubprocessRunner()
        started = time.monotonic()

        with self.assertRaises(ProcessFailure) as raised:
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=0.1,
            )

        self.assertEqual("timeout", raised.exception.result.exit_reason)
        self.assertLess(time.monotonic() - started, 3)

    def test_successful_parent_does_not_wait_for_descendant_inherited_pipes(self) -> None:
        runner = SubprocessRunner()
        started = time.monotonic()

        result = runner.run(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys; "
                    "subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(30)']); print('done')"
                ),
            ],
            timeout_seconds=5,
        )

        self.assertEqual("success", result.exit_reason)
        self.assertIn("done", result.stdout)
        self.assertLess(time.monotonic() - started, 4)

    def test_nonzero_exit_is_structured_and_redacted(self) -> None:
        runner = SubprocessRunner(redact_values=("super-secret",))

        with self.assertRaises(ProcessFailure) as raised:
            runner.run(
                [sys.executable, "-c", "import sys; print('super-secret', file=sys.stderr); sys.exit(7)"],
                timeout_seconds=5,
            )

        self.assertEqual(7, raised.exception.result.exit_code)
        self.assertNotIn("super-secret", raised.exception.result.stderr)
        self.assertIn("[REDACTED]", raised.exception.result.stderr)


if __name__ == "__main__":
    unittest.main()
