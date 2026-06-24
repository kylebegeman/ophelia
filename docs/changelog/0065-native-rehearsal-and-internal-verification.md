# 0065: Native Rehearsal And Internal Verification

Date: 2026-06-23

Status: landed

## Summary

Ophelia now supports the app-side hardening contract needed for migration QA:
internal service checks can run inside the app Compose network, JSON health and
release payloads can be asserted directly, export artifacts can be rehearsed
through a native backup command, and offsite backup requirements now point to
concrete target metadata instead of only a boolean.

## Changes

- `src/ophelia/manifest.py`: adds `verify[].type: command`, `service`,
  `command`, `expect_exit`, `expect_json`, and structured
  `data.backups.offsite` fields.
- `src/ophelia/verify.py`: runs command verification with Docker Compose,
  preserves HTTP verification behavior, and supports JSON dot-path assertions
  for both check types.
- `src/ophelia/portability.py`: exports `runtime/ophelia/` support files,
  rehearses export bundles with safe archive extraction, runs app-owned volume
  verifier hooks, reports offsite target completeness, and redacts command
  output.
- `src/ophelia/commands/backup.py`: adds `ship backup rehearse plan` and
  `ship backup rehearse apply` as a manifest-facing wrapper over restore-drill
  rehearsal.
- `docs/manifest-spec.md`, `docs/portable-app-pack-spec.md`,
  `docs/stateful-app-migration-runbook.md`, `docs/job-action-api.md`, and
  `README.md`: document the new contracts and bump current package metadata to
  `0.3.8`.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_manifest tests.test_schema_export tests.test_verify tests.test_portability -v`
- `python3 -m py_compile src/ophelia/manifest.py src/ophelia/verify.py src/ophelia/runtime.py src/ophelia/portability.py src/ophelia/commands/verify.py src/ophelia/commands/deploy.py src/ophelia/commands/backup.py src/ophelia/schema_export.py`
