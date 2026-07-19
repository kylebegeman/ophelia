---
id: 0078
title: Ophelia platform strategy and technical architecture
date: 2026-07-11
status: landed
areas: [docs, product, architecture, security, reliability, lumen, roadmap]
change_type: docs
commits: []
---

## Summary

Added one canonical strategy document consolidating the current-state audit,
product thesis, verified risk register, target runtime and fleet architecture,
security and recovery models, Lumen integration contract, implementation map,
test strategy, and dependency-ordered roadmap.

## Why

Ophelia's existing active and archived documentation described many individual
features and earlier planning phases, but did not provide one current,
decision-ready document connecting runtime truthfulness, fleet operations,
security, recovery, Lumen integration, and implementation sequencing.

## Changed Areas

- `docs/product/ophelia-platform-strategy.md`: canonical strategy and technical
  architecture source.
- `docs/product/ophelia-platform-strategy.html`: generated self-contained
  reading view with access to the Markdown source.
- `docs/assets/ophelia-platform-strategy.css`: reproducible source styles
  embedded into the self-contained HTML view.
- `README.md`, `docs/README.md`, `docs/ROADMAP.md`, `docs/llm/START_HERE.md`, and
  `docs/llm/manifest.json`: routing to the canonical document.

## Contract Impact

None. The document proposes future contracts but does not change current CLI,
manifest, API, receipt, policy, or runtime behavior.

## Safety Impact

Documentation only. No VPS, provider, application, or runtime state is mutated.

## Verification

- `make docs-check`
- `make open-source-audit-strict` (reached the pre-existing blocker in
  `docs/changelog/0076-release-043.md`; the new files produced no content-rule
  findings)

## Follow-Ups

- Track implementation of the accepted direction through the roadmap, focused
  change records, and release gates.
