---
id: 0042
title: Adoption fixture repos
date: 2026-06-22
status: landed
areas: [fixtures, adoption, tests, docs]
change_type: test
---

## Summary

Adds committed future-app repository fixtures for the Ophelia adoption contract,
plus a Make target and tests that validate those repos through
`ship app adoption plan`.

## Why

Future product behavior should be proven against Ophelia-first fixtures rather
than old deployments. Adoption fixtures give the planner stable service and
static app-repo shapes to validate without collecting live values.

## Changed Areas

- `fixtures/adoption/future-service`: service app repo fixture with manifest,
  data contract, runbook, agent notes, hooks, and data check.
- `fixtures/adoption/future-static`: static app repo fixture with manifest,
  static asset root, runbook, agent notes, hooks, and data check.
- `Makefile`: adds `validate-adoption-fixtures`.
- `tests/test_adoption_fixtures.py`: verifies every adoption fixture is
  blocker-free with complete required artifacts.
- `README.md`, `docs/app-adoption.md`,
  `docs/ophelia-source-of-truth.md`, and
  `docs/ophelia-strategic-implementation-roadmap.md`: document the fixture
  suite and validation path.

## Contract Impact

No CLI or JSON contract change. The fixture suite exercises the Phase 21
`app.adoption.plan` contract with committed future-app repo examples.

## Safety Impact

Fixtures and validation only. No VPS, provider, runtime, GitHub, env value,
secret value, probe, receipt, or deployment state is touched.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_adoption tests.test_adoption_fixtures -v`
- `make validate-adoption-fixtures PYTHON=python3`
- `python3 -m py_compile tests/test_adoption_fixtures.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Keep new adoption behavior fixture-first. Add more synthetic repo shapes when
  the contract grows.
