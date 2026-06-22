---
id: 0026
title: Fixture app suite and live-readiness harness
date: 2026-06-22
status: landed
areas: [fixtures, live-readiness, manifests, readiness, placement, drift, docs, tests]
change_type: feature
commits: []
---

## Summary

Added a committed synthetic app suite for deterministic Ophelia testing across
manifest validation, live readiness, placement, drift, provider observations,
secret-provider checks, backup status, restore drills, and blocked-state
handling.

## Why

Live-value and production-hardening work needs realistic multi-app coverage
without relying on production data, real provider credentials, or mutable VPS
state. The fixture suite gives operators and tests a safe baseline with mixed
healthy, warning, drifted, and intentionally blocked states.

## Changed Areas

- `fixtures/app-suite/`: new synthetic micro apps, manifests, runtime metadata,
  backup manifests, restore-drill receipts, GitHub observations, secret
  observations, SOPS-shaped key files, host inventory, and provider config.
- `src/ophelia/commands/live_readiness.py`: adds `--allow-blocked` so expected
  blocked reports can exit zero for fixture/CI harnesses.
- `src/ophelia/command_catalog.py`: adds a fixture-suite example for
  `live.readiness.run`.
- `Makefile`: adds `validate-fixtures` and `live-readiness-fixtures`.
- `tests/test_fixture_app_suite.py`: covers fixture manifest loading,
  read-only live-readiness execution, redaction, expected blocked state,
  placement recommendations, CLI exit behavior, and catalog discoverability.
- `docs/fixture-app-suite.md`, `README.md`, live-readiness docs, roadmap, and
  findings updated with the fixture testing lane.

## Contract Impact

Additive. `ship live-readiness run --allow-blocked` preserves the JSON report
status and only changes process exit behavior from nonzero to zero when blockers
are expected.

## Safety Impact

Net positive. The suite contains fake values only and is read-only. It improves
redaction and non-mutation coverage for future live-readiness and production
hardening work.

## Verification

- `PYTHON=python3 make validate-fixtures`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_fixture_app_suite`
- `PYTHON=python3 make live-readiness-fixtures`
- `python3 -m compileall -q src/ophelia/commands/live_readiness.py src/ophelia/command_catalog.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_fixture_app_suite tests.test_live_readiness`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add fixture cases as new provider adapters, workflow templates, plugin
  contracts, and production hardening checks land.
- Keep real staging/prod live-value runs separate from the committed fixture
  snapshot.
