# 0062: Runtime-Injected Env Readiness

Date: 2026-06-23

Status: landed

## Summary

Readiness and drift reports no longer require retained app runtime env files to
contain Ophelia-injected metadata keys such as `OPHELIA_APP`. These keys are
still rendered into Compose by Ophelia, but they are not operator-managed
secret or config values.

## Changes

- `src/ophelia/templates.py`: defines the runtime-injected env key set.
- `src/ophelia/portability.py`: marks runtime-injected env metadata as optional
  in env-shape reports while preserving strict blockers for `required_env`,
  placeholder-backed secrets, addon URLs, and manifest-declared env.
- `src/ophelia/drift.py`: excludes non-placeholder runtime-injected metadata
  keys from required env drift checks.
- `tests/test_portability.py` and `tests/test_drift.py`: cover retained runtime
  env files that omit `OPHELIA_APP`.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.5`.

## Safety

This does not weaken secret enforcement. Placeholder-backed keys, explicit
`required_env` entries, and addon-generated connection strings remain blocking
when absent or still set to placeholders.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability.PortabilityTests.test_env_shape_diff_redacts_values_and_reports_statuses tests.test_drift.DriftTests.test_runtime_injected_env_absence_does_not_report_drift`
