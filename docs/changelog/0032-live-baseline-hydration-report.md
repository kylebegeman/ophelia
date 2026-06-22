---
id: 0032
title: Live baseline hydration report
date: 2026-06-22
status: landed
areas: [live-hydration, live-readiness, live-drills, cli, api, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added focused live hydration reports for one app or one live drill profile. The
new report turns a blocked live baseline into ordered runtime, env, secret-name,
release, and host evidence steps before any probes or mutations run.

## Why

The first live baseline correctly produced broad no-go results, but operators
needed a smaller next step than "fix every app." Hydration reports make the next
safe live-testing gate explicit for one target app without creating runtime
files, reading secret values, calling providers, or running probes.

## Changed Areas

- `src/ophelia/live_hydration.py`: composes env shape, secret provider,
  placement, readiness, drift, and release metadata into a read-only hydration
  payload.
- `src/ophelia/commands/live_hydration.py`: adds
  `ship live-hydration report` with `--allow-blocked` for expected baseline
  audits.
- `src/ophelia/live_drills.py`: exposes profile resolution for reusable
  drill-backed reports.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: expose
  `/live-hydration/<profile>`.
- `Makefile`: adds `live-hydration-quark-staging`.
- `tests/test_live_hydration.py`: covers fixture, local blocked, CLI/catalog,
  API route, and no-app profile behavior.
- README, live readiness, live drill, API, roadmap, findings, scratchpad, and
  changelog docs updated.

## Contract Impact

Additive CLI/API contract:

- `ship live-hydration report --json`
- `GET /live-hydration/<profile>`
- JSON kind: `ophelia.live_hydration_report`
- Operation: `live_hydration.report`

`--allow-blocked` preserves blocked JSON status and only changes the process
exit code.

## Safety Impact

Net positive. Hydration reports are read-only and dry-run only. They do not
create runtime files, read secret values, run HTTP/Docker probes, refresh state,
execute workflows, or mutate providers.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration`
- `python3 -m compileall -q src/ophelia/live_hydration.py src/ophelia/commands/live_hydration.py src/ophelia/live_drills.py src/ophelia/api.py src/ophelia/api_routes.py src/ophelia/command_catalog.py src/ophelia/commands/__init__.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration tests.test_live_drills tests.test_live_readiness`
- `make live-hydration-quark-staging`
- `python3 -m compileall -q src`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Collect one app's non-secret runtime env shape and release metadata.
- Add secret-name observations for one target app without storing values.
- Re-run hydration and live drill profiles before enabling opt-in HTTP/Docker
  probes.
