---
id: 0055
title: GitHub private 0.3.0 metadata
date: 2026-06-22
status: landed
areas: [release, packaging, open-source, docs, tests]
change_type: changed
commits: []
---

## Summary

Sets the project version to `0.3.0`, records the repository target as
`github.com/mrbagels/ophelia`, and documents that Ophelia is GitHub-only and
private until the public release decision is made.

## Why

The release metadata should match the intended repository owner and distribution
posture before further launch polish. The audit also needs to allow the
approved GitHub owner while still warning on private registry references.

## Changed Areas

- `pyproject.toml`: bumps the package version to `0.3.0` and adds repository
  and issue tracker URLs.
- `README.md` and `docs/open-source-readiness.md`: document the `mrbagels`
  GitHub repository target, private visibility, and GitHub-only distribution.
- `src/ophelia/open_source_readiness.py`: narrows private-owner detection so
  `github.com/mrbagels/ophelia` is allowed while private registries remain
  warnings.
- `tests/test_open_source_readiness.py`: covers the approved GitHub owner and
  private GHCR warning behavior.

## Contract Impact

No CLI, JSON, manifest, runtime, receipt, or API contract changes. Package
metadata now reports version `0.3.0`.

## Safety Impact

No runtime, provider, repository visibility, or deployed host state is mutated.
This only updates repository metadata, docs, and audit tests.

## Verification

- `.venv/bin/python -m pip install -e ".[test]"`
- `.venv/bin/python - <<'PY' ...` verified installed package version `0.3.0`
  and repository URL metadata.
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_open_source_readiness tests.test_self_test -v`
- `make validate-examples validate-manifests validate-fixtures validate-adoption-fixtures validate-fixture-plugins render-examples render-manifests compile docs-check open-source-audit-strict`
- `git diff --check`
- `.venv/bin/python -m pip check`

## Follow-Ups

- None.
