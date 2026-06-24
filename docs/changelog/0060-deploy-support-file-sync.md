# 0060: Deploy Support File Sync

Date: 2026-06-23

Status: landed

## Summary

Deploys now carry repo-local hook and app data verification scripts into the
runtime app directory. This makes manifest references such as
`hooks.pre_export: ophelia/hooks/pre-export.sh` and
`data.volumes[].verify.command: ophelia/checks/data-verify.sh` usable after an
app is staged or applied.

## Changes

- `src/ophelia/runtime.py`: includes relative hook paths and relative command
  entrypoints from data export/import/verify contract blocks in the support-file
  map used by render, deploy, release bundle, and cleanup flows.
- `tests/test_planning.py`: verifies env files, hook scripts, and data verify
  scripts are deployed and preserved when re-deploying from `manifest.lock.json`.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.3`.

## Safety

Only relative repo paths that look like support files are copied. Ordinary
binary names such as `pg_dump` or `tar-zstd` are not treated as files. Existing
path-safety checks still prevent copying files outside the manifest directory.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_planning -v`
- `python3 -m py_compile src/ophelia/runtime.py tests/test_planning.py`
