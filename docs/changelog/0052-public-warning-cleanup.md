---
id: 0052
title: Public warning cleanup
date: 2026-06-22
status: landed
areas: [open-source, manifests, docs, tests, audit]
change_type: changed
commits: []
---

## Summary

Removes the remaining warning-class private and legacy product references from
tracked public source, tests, and documentation. The manifest console profile is
now described with public Ophelia terminology, fixture names are synthetic, and
the strict open-source gate enforces a zero-warning baseline.

## Why

The public repository should teach the reusable Ophelia contract, not preserve
private product names or old deployment labels in active APIs, examples, tests,
or historical planning docs. Warning cleanup also makes the audit useful as a
CI gate instead of a report that still needs manual interpretation.

## Changed Areas

- `src/ophelia/manifest.py`, `schema_export.py`, `templates.py`, and
  `verify.py`: replace the old private profile naming with `profile: console`,
  `console:` config, and `console.surface: console | root`.
- `src/ophelia/operator_reports.py` and `portability.py`: classify console
  env keys under the public `OPHELIA_CONSOLE_` prefix.
- `src/ophelia/actions.py`: rename wrapper artifact identifiers to generic
  console task/run ids.
- `src/ophelia/open_source_readiness.py`: tighten private term matching so
  underscore-delimited identifiers are caught.
- `src/ophelia/commands/open_source.py` and `Makefile`: add
  `--fail-on-warnings` and make `open-source-audit-strict` fail on warnings.
- `tests/`: convert fixture app, host, profile, and env names to synthetic
  public-safe terms.
- `docs/`: sanitize historical planning references, rename public doc paths,
  and update the release-readiness guide for the zero-warning gate.

## Contract Impact

Manifests now use the public console profile contract:

```yaml
profile: console
console:
  admin_domain: console.example.com
  surface: console
```

The generated `.env.example` keys for console profiles now use
`OPHELIA_CONSOLE_*`. The strict audit command now exits non-zero when warning
findings are present.

## Safety Impact

No runtime state, provider state, or deployed host state is mutated. The change
only updates repository source, tests, examples, and documentation.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_manifest tests.test_schema_export tests.test_open_source_readiness tests.test_command_catalog -v`
- `make test`
- `make compile docs-check validate-examples render-examples open-source-audit-strict`
- `./cli/ship open-source audit --fail-on-warnings --max-findings 5000 --json`

## Follow-Ups

- None.
