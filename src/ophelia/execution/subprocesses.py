"""Bounded, cancellable external process execution for runtime backends."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ProcessResult:
    argv: Tuple[str, ...]
    exit_code: Optional[int]
    exit_reason: str
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int


class ProcessFailure(RuntimeError):
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or result.exit_reason
        super().__init__("External process failed (%s): %s" % (result.exit_reason, detail))


class SubprocessRunner:
    """Run one argv without a shell and retain only bounded, redacted output."""

    def __init__(
        self,
        *,
        max_output_bytes: int = 64 * 1024,
        redact_values: Sequence[str] = (),
        poll_interval_seconds: float = 0.05,
        terminate_grace_seconds: float = 2.0,
    ) -> None:
        if max_output_bytes < 1 or max_output_bytes > 16 * 1024 * 1024:
            raise ValueError("max_output_bytes must be between 1 and 16777216.")
        self.max_output_bytes = max_output_bytes
        self.redact_values = tuple(
            sorted(
                {value for value in redact_values if isinstance(value, str) and value},
                key=len,
                reverse=True,
            )
        )
        self.poll_interval_seconds = poll_interval_seconds
        self.terminate_grace_seconds = terminate_grace_seconds

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        cancellation_requested: Optional[Callable[[], bool]] = None,
        check: bool = True,
    ) -> ProcessResult:
        command = self._argv(argv)
        if timeout_seconds <= 0 or timeout_seconds > 86400:
            raise ValueError("timeout_seconds must be greater than zero and no more than 86400.")
        started = time.monotonic()
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
                close_fds=True,
            )
            reason = "exited"
            deadline = started + timeout_seconds
            while process.poll() is None:
                if cancellation_requested is not None and cancellation_requested():
                    reason = "cancelled"
                    self._terminate(process)
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    self._terminate(process)
                    break
                time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
            if process.poll() is None:
                self._terminate(process)
            exit_code = process.wait()
            result = ProcessResult(
                argv=tuple(self._redact(item) for item in command),
                exit_code=exit_code,
                exit_reason=reason if reason != "exited" else ("success" if exit_code == 0 else "nonzero_exit"),
                stdout=self._read(stdout_file),
                stderr=self._read(stderr_file),
                stdout_truncated=self._truncated(stdout_file),
                stderr_truncated=self._truncated(stderr_file),
                duration_ms=max(1, int((time.monotonic() - started) * 1000)),
            )
        if check and result.exit_reason != "success":
            raise ProcessFailure(result)
        return result

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + self.terminate_grace_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _read(self, handle) -> str:
        handle.flush()
        handle.seek(0)
        value = handle.read(self.max_output_bytes).decode("utf-8", errors="replace")
        return self._redact(value)

    def _truncated(self, handle) -> bool:
        handle.flush()
        handle.seek(0, os.SEEK_END)
        return handle.tell() > self.max_output_bytes

    def _redact(self, value: str) -> str:
        result = value
        for secret in self.redact_values:
            result = result.replace(secret, "[REDACTED]")
        return result

    @staticmethod
    def _argv(argv: Sequence[str]) -> Tuple[str, ...]:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("argv must be a non-empty sequence of strings.")
        parsed = tuple(argv)
        if any(not isinstance(item, str) or not item or "\x00" in item for item in parsed):
            raise ValueError("argv must contain non-empty strings without NUL bytes.")
        return parsed
