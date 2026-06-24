---
id: 0050
title: Public-safe operator docs
date: 2026-06-22
status: landed
areas: [open-source, docs]
change_type: cleanup
commits: []
---

## Summary

Replaced private operational handbooks and handoff notes with public-safe
operator guidance and generic migration material.

## Why

Private hostnames, personal paths, deployment handoffs, and product-specific
layout notes should not ship in the public repository. The public docs should
describe reusable Ophelia contracts while private operational state remains
outside Git.

## Changed Areas

- `docs/platform-handbook.md`: replaced private host inventory with a reusable
  public operator handbook.
- `docs/migration-plan.md`: replaced private migration notes with a generic
  adoption/migration outline.
- `docs/demo-docs-host-layout.md`: replaced product-specific layout details
  with a public-safe note that private product docs belong outside the repo.
- `docs/private-operator-vps-deployment-agent-packaging-note.md` and
  `docs/private-operator-vps-ophelia-2-handoff.md`: replaced private handoff content with
  public integration summaries.
- `docs/ophelia-improvement-execution-plan.md` and `docs/prompts/*.md`:
  replaced local private paths with generic paths.
- `README.md`: removes the product-specific host layout doc from core docs.

## Contract Impact

None. Documentation-only cleanup.

## Safety Impact

No runtime behavior changes. Active manifests, private config, and deployment
workflow files were intentionally left untouched.

## Verification

- `./cli/ship open-source audit --allow-blocked --max-findings 2000 --json`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Remaining blockers require approval: active manifests/config, private deploy
  workflow, governance files, and final license selection.
