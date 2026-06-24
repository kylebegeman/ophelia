# 0063: Export-Backed Backup Readiness

Date: 2026-06-23

Status: landed

## Summary

App export bundles now satisfy backup readiness when they complete successfully
and include all declared data archives. Export create can archive app-owned
Docker named volumes by mounting the volume read-only into a local helper image.

## Changes

- `src/ophelia/portability.py`: archives Docker named volumes during export
  create when no host source path is declared.
- `src/ophelia/portability.py`: marks export create as failed when declared
  data archives cannot be produced.
- `src/ophelia/portability.py`: lets `backup.status` count complete successful
  app export bundles as fresh backup evidence while ignoring incomplete exports.
- `tests/test_portability.py`: covers named-volume export, complete export
  backup freshness, and incomplete export filtering.
- `docs/manifest-spec.md` and `docs/portable-app-pack-spec.md`: document
  export-backed backup readiness and read-only Docker named-volume archiving.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.6`.

## Safety

Docker named-volume export uses read-only volume mounts and local helper images
only. Ophelia does not pull a helper image implicitly. If no helper image is
available or the archive command fails, export create records a failed data
archive instead of reporting success.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability.PortabilityTests.test_export_create_archives_docker_named_volume_without_local_source tests.test_portability.PortabilityTests.test_backup_status_counts_complete_export_bundle tests.test_portability.PortabilityTests.test_backup_status_ignores_incomplete_export_bundle`
