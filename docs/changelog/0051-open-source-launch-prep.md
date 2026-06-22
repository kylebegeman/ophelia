---
id: 0051
title: Open source launch prep
date: 2026-06-22
status: landed
areas: [open-source, docs, ci, manifests, examples, governance]
change_type: docs
commits: []
---

## Summary

Adds the approved Apache-2.0 and DCO public-governance surface, rewrites the
README as the public onboarding entrypoint, and replaces active private
manifests/config/workflows with generic fixture-first material.

## Why

Ophelia is being prepared for a public repository. The public tree needs clear
licensing, contribution rules, security/support guidance, agent entrypoints, and
no active private deploy workflow or host registry.

## Changed Areas

- `README.md`: public quick start, badges, safety model, docs map, and agent
  guidance.
- `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `SECURITY.md`, `SUPPORT.md`,
  `CODE_OF_CONDUCT.md`: public governance and legal surface.
- `.github/workflows/ci.yml`: runs the strict open-source readiness audit.
- `.github/pull_request_template.md`: adds DCO, verification, and safety
  checklist.
- `config/app-registry.example.json`, `manifests/`, `examples/`, `platform/`:
  public-safe demo material replaces private active deployment files.
- `docs/llm/`: adds an agent-first start page and machine-readable doc
  manifest.
- Core docs: host contract, architecture, live drills, manifest spec, portable
  app pack spec, source-of-truth notes, and open-source readiness were updated
  for public fixture-first direction.

## Contract Impact

The default app registry path now points to `config/app-registry.example.json`.
The public active manifests are synthetic demo manifests. The private deploy
workflow and product-specific helper scripts were removed from the public tree.

## Safety Impact

No runtime or VPS state is mutated. The change removes active private deployment
automation from the public repository and adds CI enforcement for the
open-source audit.

## Verification

- `make validate-examples`
- `make validate-manifests`
- `make render-examples`
- `make render-manifests`
- `make validate-fixtures`
- `make validate-adoption-fixtures`
- `make validate-fixture-plugins`
- `make docs-check PYTHON=python3`
- `make compile PYTHON=python3`
- `make open-source-audit-strict`
- `make test PYTHON=python3`
- `git diff --check`

## Follow-Ups

- Review warning-only open-source audit findings in historical changelogs,
  compatibility tests, and manifest profile compatibility strings during a
  later cleanup phase.
