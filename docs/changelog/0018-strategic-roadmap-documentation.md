---
id: 0018
title: Strategic roadmap documentation
date: 2026-06-21
status: landed
areas: [docs, roadmap, findings, planning]
change_type: docs
commits: []
---

## Summary

Documented the selected next-stage Ophelia product improvements as product
findings and a dependency-ordered strategic implementation roadmap with
milestone phase boundaries.

## Why

The selected improvements include both near-term ergonomics and large strategic
systems. They need a durable plan that sequences foundations before dependent
capabilities and gives future implementation agents a clear handoff path.

## Changed Areas

- `docs/product-improvement-findings.md`: selected findings, rationale, and
  phase mapping.
- `docs/ophelia-strategic-implementation-roadmap.md`: phased implementation
  plan, dependencies, definitions of done, and milestone commit labels.
- `README.md`: core docs and next milestones now route to the new roadmap.
- `docs/ophelia-next-architecture.md`: architecture direction now links to the
  current strategic roadmap.
- `docs/ophelia-improvement-execution-plan.md`: marked as completed baseline
  and linked to the new roadmap.
- `docs/prompts/ophelia-improvement-implementation-agent.md`: updated agent
  handoff instructions to start from the new roadmap.
- `docs/changelog/INDEX.md`: added this record.

## Contract Impact

None. This is documentation and planning only. No CLI, JSON, manifest, API,
runtime, or receipt contract changed.

## Safety Impact

Net positive. The roadmap keeps production mutation behind existing
plan/confirmation/receipt gates and explicitly sequences redaction, state, and
contract foundations before larger integrations.

## Verification

- `git diff --check`
- `PYTHONPATH=src python3 -m ophelia.docs_check`

## Follow-Ups

- Begin Phase 1 of `docs/ophelia-strategic-implementation-roadmap.md` when Kyle
  approves implementation work.
