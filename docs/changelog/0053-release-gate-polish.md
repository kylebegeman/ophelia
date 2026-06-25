---
id: 0053
title: Release gate polish
date: 2026-06-22
status: landed
areas: [open-source, ci, packaging, docs, tests]
change_type: changed
commits: []
---

## Summary

Aligns local setup, CI, README guidance, PR checklist, and LLM docs around the
same public validation gate. CI and `make venv` now install test extras so
schema conformance tests run instead of skipping when `jsonschema` is absent.

## Why

The public release gate should exercise the same contract checks locally and in
CI. Packaging metadata should also avoid deprecated setuptools license forms
before the project is published.

## Changed Areas

- `pyproject.toml`: switches to the SPDX license string form, includes license
  files explicitly, bumps the setuptools build requirement, and updates the
  package description for the public project.
- `Makefile`: upgrades pip during `make venv` and installs editable test extras.
- `.github/workflows/ci.yml`: installs test extras and runs the full fixture
  validation gate.
- `.github/pull_request_template.md`, `README.md`, `CONTRIBUTING.md`, and
  `docs/llm/`: align validation commands for contributors and agents.
- `docs/open-source-readiness.md`: replaces stale warning-cleanup wording with
  the current zero-warning policy.

## Contract Impact

No CLI, JSON, manifest, runtime, receipt, or API contract changes. The CI/local
development contract now expects `.[test]` extras for full validation.

## Safety Impact

No runtime, provider, or deployed host state is mutated. The only local state
mutation during verification was refreshing the repository `.venv` dependency
set with test extras.

## Verification

- `.venv/bin/python -m ensurepip --upgrade && .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -e ".[test]"`
- `make test`
- `make validate-examples validate-manifests validate-fixtures validate-adoption-fixtures validate-fixture-plugins render-examples render-manifests compile docs-check open-source-audit-strict`

## Follow-Ups

- None.
