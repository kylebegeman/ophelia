---
id: 0025
title: Read-only live readiness lane
date: 2026-06-22
status: landed
areas: [live-readiness, cli, hosts, placement, observability, secrets, drift, docs, tests]
change_type: feature
commits: []
---

## Summary

Added `ship live-readiness run`, a read-only aggregate report for real staging
and production runtime inspection before live mutation hardening.

## Why

After host inventory, placement, provider contracts, drift, and observability
landed, Ophelia needed a safe way to start using real values immediately. This
lane lets operators point at real runtime roots, manifests, host config, and
provider observation files without applying changes.

## Changed Areas

- `src/ophelia/live_readiness.py`: aggregates host inventory/readiness, GitHub
  provider status, secret provider status, state status, operator dashboard data,
  app readiness, placement, observability, secret-provider presence, and drift.
- `src/ophelia/commands/live_readiness.py`: adds
  `ship live-readiness run`.
- `src/ophelia/commands/__init__.py`: registers the new command group.
- `src/ophelia/command_catalog.py`: adds catalog example metadata for
  `live.readiness.run`.
- `src/ophelia/lumen_adapter.py`: advertises `live_readiness` as a surface.
- `docs/live-readiness-lane.md`: documents usage, probe policy, observation
  files, and the non-mutation contract.
- `README.md` and roadmap docs updated with the live-readiness lane.
- `tests/test_live_readiness.py`: verifies redaction, read-only behavior, and
  command catalog registration.

## Contract Impact

Additive CLI contract:

- `ship live-readiness run --json` emits
  `kind: "ophelia.live_readiness_report"`.
- HTTP and Docker probes are disabled by default. `--probe-http` and
  `--check-docker` opt into bounded read-only probes and are reflected in
  `probe_policy`.
- The report carries a `mutation_guard` showing that apply/create/rebuild/refresh
  operations are not called.

## Safety Impact

Net positive. The lane is read-only, requires no confirmation token, calls no
mutating operation, does not rebuild the state DB, and does not write
observability schedule artifacts. All child reports are passed through deep
redaction before output.

## Verification

- `python3 -m compileall -q src/ophelia/live_readiness.py src/ophelia/commands/live_readiness.py src/ophelia/commands/__init__.py src/ophelia/command_catalog.py src/ophelia/lumen_adapter.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_readiness`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `PYTHON=python3 make validate-examples`
- CLI smoke: `ship live-readiness run --json`
- Local baseline: `ship live-readiness run --runtime-root ~/ophelia-runtime --manifests-dir manifests --json`
- `git diff --check`

## Follow-Ups

- Use the lane against real staging/prod runtime roots and provider observation
  files to collect gaps before Phase 9 production hardening.
- Phase 7 provider/plugin contracts can add authenticated observation collectors
  behind this read-only report shape.
