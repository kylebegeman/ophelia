---
id: 0040
title: Source-of-truth and fixture-first validation
date: 2026-06-22
status: landed
areas: [docs, fixtures, command-catalog, tests]
change_type: docs
---

## Summary

Clarifies that Ophelia defines the app/runtime contract first and that fixture
apps are the default validation substrate. Old deployments remain legacy
inventory unless an explicit migration or production deployment phase starts.

## Why

Using abandoned products as test anchors can accidentally turn their stale
runtime assumptions into new Ophelia features. The product direction is now
contract-first: future software conforms to Ophelia, and tests prove the
contract with synthetic fixtures before any retained product is migrated.

## Changed Areas

- `docs/ophelia-source-of-truth.md`: adds the durable source-of-truth rule.
- `README.md`: points active examples at fixtures and frames Quark as legacy.
- `docs/architecture.md`: updates the ownership boundary around Ophelia.
- `docs/live-hydration.md`, `docs/live-drill-profiles.md`, and
  `docs/live-readiness-lane.md`: move primary examples to fixtures.
- `Makefile`: removes Quark-specific hydration shortcut targets.
- `src/ophelia/command_catalog.py` and
  `src/ophelia/commands/live_hydration.py`: update catalog examples to
  fixture-backed commands.
- `tests/test_live_hydration.py` and `tests/test_live_drills.py`: stop using
  Quark as the active behavior test path.

## Contract Impact

No JSON schema or CLI argument contract changes. Active examples and Make
shortcuts now use fixture profiles rather than old product deployments.

## Safety Impact

Docs, tests, command examples, and Make targets only. No VPS, provider, runtime,
or production state is mutated.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_live_hydration tests.test_live_drills -v`
- `python3 -m py_compile tests/test_live_hydration.py tests/test_live_drills.py src/ophelia/commands/live_hydration.py src/ophelia/command_catalog.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Define retained-product adoption artifacts for `stillup` and `clearedtorun`
  when their deployment or migration phase begins.
