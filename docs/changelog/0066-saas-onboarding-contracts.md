# 0066: SaaS Onboarding Contracts

Date: 2026-06-24

Status: landed

## Summary

Ophelia now has the first-class contracts needed before the next SaaS
onboarding: app-owned checks can run inside the app container network,
health/release JSON can be asserted directly, non-live apps can be reset through
a guarded fresh-install workflow, backup rehearsals have a one-shot artifact
command, backup policy warnings name concrete missing fields, and release
metadata is injected into service containers.

## Changes

- `src/ophelia/manifest.py` and `src/ophelia/schema_export.py`: add
  `verify[].type: internal`, `path`, `method`, `json_assertions`, actionable
  offsite backup policy fields, and `lifecycle`.
- `src/ophelia/verify.py`: adds internal service verification, schema-aware JSON
  assertions, app-owned check selection, and JSON-aware redacted excerpts.
- `src/ophelia/templates.py` and `src/ophelia/runtime.py`: inject
  `OPHELIA_ENVIRONMENT`, `OPHELIA_APP`, `OPHELIA_SERVICE`,
  `OPHELIA_RELEASE_ID`, `OPHELIA_IMAGE_REF`, `OPHELIA_IMAGE_DIGEST`,
  `OPHELIA_COMMIT_SHA`, and `OPHELIA_BUILD_TIME`.
- `src/ophelia/portability.py` and `src/ophelia/commands/app.py`: add
  `ship app fresh-install plan|apply`, lifecycle reset gating, pre-reset export
  preservation, reset evidence, and post-reset verification.
- `src/ophelia/commands/backup.py`: adds direct
  `ship backup rehearse <artifact> --manifest ... --json` while preserving the
  existing plan/apply forms.
- `src/ophelia/adoption.py`: reports standard app-owned endpoints and npm
  scripts during adoption planning.
- `examples/`, `fixtures/`, and `docs/`: update manifests and operator docs for
  the new contracts.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_verify tests.test_manifest tests.test_schema_export tests.test_adoption tests.test_adoption_fixtures tests.test_portability -v`
- `python3 -m py_compile src/ophelia/manifest.py src/ophelia/schema_export.py src/ophelia/verify.py src/ophelia/templates.py src/ophelia/runtime.py src/ophelia/portability.py src/ophelia/adoption.py src/ophelia/commands/app.py src/ophelia/commands/backup.py tests/test_verify.py tests/test_manifest.py tests/test_portability.py tests/test_adoption.py`
