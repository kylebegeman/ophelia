---
id: 0057
title: Private UI public-surface wording
date: 2026-06-23
status: landed
areas: [docs, operator-console, open-source]
change_type: docs
---

## Summary

Reframes public docs and help text so Ophelia describes generic
operator-console/private-UI contracts instead of presenting a private UI repo as
a public dependency.

## Why

The planned public repository should not imply that private repositories or UI
products are available to users. Ophelia can expose stable JSON contracts for
private or internal operator UIs without making those UIs part of the public
project.

## Changed

- Renamed the active console doc to `docs/operator-console-adapter.md`.
- Updated README, roadmap, architecture, fixture, readiness, hardening, plugin,
  host-placement, and LLM docs to use public-safe operator-console wording.
- Added an open-source readiness rule that private/internal UIs must be framed
  as optional consumers, not public dependencies.
- Sanitized historical changelog/archive prose that described a private UI as a
  public product direction.
- Updated source comments and CLI descriptor summaries to avoid public-facing
  private product references.
- Left the existing `ship lumen ...`, `/lumen/...`, and `ophelia.lumen.*`
  compatibility names documented only as historical compatibility details.

## Safety

Documentation and text-only help/comment cleanup. This does not mutate VPS,
runtime state, provider state, or command behavior.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_lumen_adapter tests.test_command_catalog tests.test_open_source_readiness -v`
- `make docs-check open-source-audit-strict`
- `make compile`
- `git diff --check`
- Capitalized private-product-name scan across README, docs, source, tests,
  config, and GitHub metadata returned no matches.
