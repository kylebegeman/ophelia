---
id: 0034
title: Hydration evidence validation
date: 2026-06-22
status: landed
areas: [live-hydration, cli, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added read-only validation for live hydration scaffold/evidence directories.

## Why

Scaffolds make evidence collection repeatable, but operators need a guardrail
before any collected facts are copied into consumed runtime paths. Validation
checks structure and safety without promoting, copying, or printing values.

## Changed Areas

- `src/ophelia/live_hydration.py`: adds evidence validation over env templates,
  secret-name observation JSON, release metadata, host capability templates, and
  recursive sensitive-value detection.
- `src/ophelia/commands/live_hydration.py`: adds
  `ship live-hydration validate-evidence`.
- `src/ophelia/command_catalog.py`: adds validation examples.
- `Makefile`: adds `live-hydration-validate-evidence-legacy-console-staging`.
- `tests/test_live_hydration.py`: covers missing directories, template warnings,
  secret-value blocking, CLI output, and catalog visibility.
- README, live-hydration docs, roadmap, findings, and scratchpad docs updated.

## Contract Impact

Additive CLI contract:

- `ship live-hydration validate-evidence --json`
- JSON kind: `ophelia.live_hydration_evidence_validation`
- Operation: `live_hydration.evidence.validate`

Template-only kits validate as `warning`. Missing, malformed, or unsafe kits
validate as `blocked`.

## Safety Impact

Net positive. The command is read-only and emits no evidence values. It does
not copy scaffold files into runtime env, release metadata, provider
observations, host inventory, state DB files, or workflow artifacts.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration`
- `make live-hydration-validate-evidence-legacy-console-staging`
- `python3 -m compileall -q src/ophelia/live_hydration.py src/ophelia/commands/live_hydration.py src/ophelia/command_catalog.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add a no-probe promotion/readiness gate that combines hydration and evidence
  validation before any opt-in HTTP/Docker checks.
- Only after a reviewed app passes the file-based gate, run bounded probes with
  explicit flags.
