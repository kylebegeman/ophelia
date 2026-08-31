# 0064: Export Backup Report Polish

Date: 2026-06-23

Status: landed

## Summary

Backup status reports now present export-backed backup evidence more cleanly.
When a complete export bundle is available, the backup-directory check passes
even if the legacy backup directory does not exist, and compressed archive
coverage can be inferred from the recorded bundle archive path.

## Changes

- `src/ophelia/portability.py`: treats export-bundle evidence as satisfying the
  backup-directory check.
- `src/ophelia/portability.py`: infers compressed archive coverage from either
  the receipt field or the recorded archive path.
- `tests/test_portability.py`: covers export-backed backup status report
  presentation.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.7`.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability.PortabilityTests.test_backup_status_counts_complete_export_bundle`
