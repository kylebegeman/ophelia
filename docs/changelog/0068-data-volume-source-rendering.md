# 0068: Data Volume Source Rendering

Date: 2026-06-24

Status: landed

## Summary

Ophelia now renders declared `data.volumes[].source` values as explicit host
bind sources. Bare relative values such as `uploads` become `./uploads` in
Compose output, matching fresh-install and export semantics while preserving
Docker named volumes for data volumes with no `source`. Package metadata is
bumped to `0.3.10`.

## Changes

- `src/ophelia/templates.py`: normalizes declared data volume sources before
  rendering Compose mounts.
- `tests/test_manifest.py`: covers bare relative data volume sources so they do
  not regress into Docker named volume syntax.
- `docs/manifest-spec.md` and `docs/portable-app-pack-spec.md`: document the
  explicit relative-bind behavior.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_manifest.ManifestTests.test_render_compose_uses_explicit_relative_bind_for_data_volume_source tests.test_manifest.ManifestTests.test_render_compose_mounts_declared_data_volume tests.test_portability.PortabilityTests.test_fresh_install_resolves_relative_volume_source_as_host_path -v`
