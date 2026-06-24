# 0058: Required Env Contract

Date: 2026-06-23

Status: landed

## Summary

Adds `required_env` as a manifest field for env key names that must exist in
the runtime `env` file but must not be rendered as inline Compose environment
overrides. This supports retained-app migrations where existing runtime env
files already hold real secret values and Ophelia should validate key presence
without clobbering those values.

## Changes

- `src/ophelia/manifest.py`: parses and validates top-level `required_env`.
- `src/ophelia/templates.py`: emits `required_env` keys into `env.example` as
  placeholders while keeping them out of Compose inline environment blocks.
- `src/ophelia/runtime.py`: blocks apply when required placeholder-backed keys
  are missing from an existing runtime env file.
- `src/ophelia/portability.py`: attributes env diff requirements to
  `manifest.required_env`.
- `docs/manifest-spec.md`: documents the new field.
- Tests cover parser/lock round-trip, schema export, deploy planning, Compose
  non-overrides, and apply-time missing-key failures.

## Safety

No secret values are stored, printed, or inferred. The field stores key names
only. Applies become stricter for manifests that declare `required_env`: a
missing key in the runtime env file fails before Docker or Caddy activation.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_manifest tests.test_planning tests.test_apply_safety tests.test_schema_export -v`
- `python3 -m py_compile src/ophelia/manifest.py src/ophelia/templates.py src/ophelia/runtime.py src/ophelia/portability.py tests/test_manifest.py tests/test_planning.py tests/test_apply_safety.py tests/test_schema_export.py`
