# 0059: Volume Verify Hook Contract

Date: 2026-06-23

Status: landed

## Summary

Extends backup verification planning and receipts so volume-backed data
contracts can surface `data.volumes[].verify.command` the same way Postgres
contracts already surface `data.postgres.verify.command`.

This matters for retained app migrations that keep application state in mounted
runtime data directories rather than a Postgres database. The verify hook is
still recorded and redacted, not executed by default.

## Changes

- `src/ophelia/restore_verification.py`: collects app verify commands from
  `data.postgres.verify.command` and `data.volumes[].verify.command`.
- `tests/test_restore_verification.py`: covers a volume-only critical manifest
  and verifies secret-shaped command arguments are scrubbed from plan output.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.2`.

## Safety

No new mutation is introduced. Backup verify apply continues to execute archive
and metadata checks only; app-defined hooks remain recorded for operator review.
All command strings pass through the existing command redactor.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_restore_verification -v`
- `python3 -m py_compile src/ophelia/restore_verification.py tests/test_restore_verification.py`
