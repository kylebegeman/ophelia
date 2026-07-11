# 0067: SaaS Onboarding Bug Bash

Date: 2026-06-24

Status: landed

## Summary

This patch hardens the new SaaS onboarding and migration surfaces before the
next downstream adoption run. Fresh-install reset planning is safer and more
consistent with manifest volume sources, app-owned verification is used after
non-live resets, JSON assertion reports avoid leaking sensitive path values,
and the package metadata is bumped to `0.3.9`.

## Changes

- `src/ophelia/portability.py`: resolves declared `data.volumes[].source`
  values as manifest-relative host paths for fresh-install reset planning,
  blocks reset targets that resolve to the manifest directory or an ancestor,
  and runs only app-owned verification after reset apply.
- `src/ophelia/verify.py`: redacts HTTP error bodies, handles invalid JSON
  assertion regexes as assertion failures instead of crashes, redacts assertion
  actual and expected values for sensitive JSON paths, and stops echoing regex
  patterns in assertion condition messages.
- `src/ophelia/schema_export.py`: publishes parser-aligned schema patterns for
  `verify[].path` and `verify[].method`.
- `tests/`: adds regression coverage for fresh-install relative sources, reset
  path safety, app-owned post-reset verification, HTTP error redaction, invalid
  regex assertions, sensitive assertion paths, and schema export precision.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.9`.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_verify tests.test_portability.PortabilityTests.test_fresh_install_plan_and_apply_reset_non_live_host_volume tests.test_portability.PortabilityTests.test_fresh_install_resolves_relative_volume_source_as_host_path tests.test_portability.PortabilityTests.test_fresh_install_runs_app_owned_verifier_not_public_route_checks tests.test_portability.PortabilityTests.test_fresh_install_blocks_manifest_directory_reset_target tests.test_schema_export -v`
