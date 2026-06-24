---
id: 0046
title: Open-source readiness audit
date: 2026-06-22
status: landed
areas: [open-source, cli, docs, command-catalog, tests]
change_type: feature
commits: []
---

## Summary

Added a read-only open-source readiness lane that scans the tracked public
surface for release blockers and documents the recommended licensing and launch
prep direction.

## Why

Ophelia is moving toward a public repository. The project needs a repeatable
gate that catches private hostnames, personal paths, tracked scratchpad notes,
private deploy workflows, missing governance files, and high-confidence secret
literals before publication.

## Changed Areas

- `src/ophelia/open_source_readiness.py`: adds the read-only audit report.
- `src/ophelia/commands/open_source.py`: adds `ship open-source audit`.
- `src/ophelia/commands/__init__.py`: registers the open-source command group.
- `Makefile`: adds `open-source-audit` and `open-source-audit-strict` targets.
- `tests/test_open_source_readiness.py`: covers report behavior, CLI JSON, and
  command catalog registration.
- `docs/open-source-readiness.md`: documents the license recommendation,
  contribution model, architecture cleanup, and prep phases.
- `README.md`: links the readiness doc and command.
- `docs/changelog/INDEX.md`: records this change.

## Contract Impact

New read-only CLI command:

```bash
ship open-source audit [--root PATH] [--max-findings N] [--allow-blocked] [--json]
```

JSON output uses `kind: ophelia.open_source_audit_report`, operation
`open_source.audit`, and the usual schema-versioned report posture.

## Safety Impact

Read-only. The audit does not mutate files, runtime state, providers, Git
history, or deployment infrastructure.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_open_source_readiness tests.test_command_catalog -v`
- `PYTHONPATH=src python3 -m unittest tests.test_open_source_readiness tests.test_hardening tests.test_command_catalog -v`
- `./cli/ship open-source audit --allow-blocked --json`
- `python3 -m compileall src`
- `PYTHONPATH=src python3 -m ophelia.docs_check`

## Follow-Ups

- Approve the final license before adding `LICENSE`.
- Rewrite public docs and examples around fixtures and generic domains.
- Move or remove private migration notes, scratchpad docs, and private deploy
  workflows before making the repository public.
