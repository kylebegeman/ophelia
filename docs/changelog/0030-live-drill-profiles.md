---
id: 0030
title: Live drill profiles
date: 2026-06-22
status: landed
areas: [live-readiness, hardening, fixtures, cli, api, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added read-only live drill profiles: named scenarios that run live-readiness and
optional production hardening reports, then validate expected mixed states.

## Why

The fixture app suite and production hardening report made safe live-value work
possible, but operators needed reusable rehearsal profiles instead of repeating
long command lines and manually checking expected fixture states.

## Changed Areas

- `src/ophelia/live_drills.py`: adds profile loading, profile listing,
  single-profile runs, run-all aggregation, expectation checks, and redaction.
- `src/ophelia/commands/live_drills.py`: adds
  `ship live-drills list|run|run-all`.
- `fixtures/app-suite/live-drills.yml`: adds full-suite, Postgres-focused,
  static-focused, and incomplete-app profiles.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: expose
  `/live-drills` and `/live-drills/<profile>`.
- `src/ophelia/command_catalog.py`: adds live drill examples.
- `Makefile`: adds `live-drills-fixtures`.
- `tests/test_live_drills.py`: verifies profile expected states, CLI JSON,
  route/catalog registration, redaction, and missing-profile blockers.
- README, live-readiness docs, fixture docs, roadmap, findings, and changelog
  index updated.

## Contract Impact

Additive contracts:

- `ship live-drills list --json`
- `ship live-drills run <profile> --json`
- `ship live-drills run-all --json`
- `GET /live-drills`
- `GET /live-drills/<profile>`
- `kind: "ophelia.live_drill_profiles"`
- `kind: "ophelia.live_drill_result"`
- `kind: "ophelia.live_drill_run_results"`

## Safety Impact

Net positive. Live drills are read-only and accept no confirmation tokens. They
do not mutate providers, workflows, runtime state, or the state DB. HTTP and
Docker probes only run when a profile explicitly opts in.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_drills`
- `make live-drills-fixtures`
- `python3 -m compileall -q src/ophelia/live_drills.py src/ophelia/commands/live_drills.py src/ophelia/api.py src/ophelia/api_routes.py src/ophelia/command_catalog.py src/ophelia/commands/__init__.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add operator-reviewed real staging/prod profile files.
- Add authenticated provider collection behind explicit opt-in probes.
