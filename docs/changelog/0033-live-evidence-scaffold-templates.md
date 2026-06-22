---
id: 0033
title: Live evidence scaffold templates
date: 2026-06-22
status: landed
areas: [live-hydration, cli, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Added `ship live-hydration scaffold`, a dry-run-first command that creates
non-secret template kits for one live hydration baseline.

## Why

After focused hydration reports, the next safe live-testing step is to collect
real evidence for one app. The scaffold makes that collection repeatable without
editing consumed runtime env files, release metadata, provider observation
paths, or host inventory by accident.

## Changed Areas

- `src/ophelia/live_hydration.py`: adds scaffold payload generation, template
  rendering, write guards, and redaction-safe target paths.
- `src/ophelia/commands/live_hydration.py`: adds
  `ship live-hydration scaffold` with `--write`, `--force`, and `--output-dir`.
- `src/ophelia/command_catalog.py`: adds scaffold examples.
- `Makefile`: adds `live-hydration-scaffold-quark-staging`.
- `tests/test_live_hydration.py`: covers dry-run, write, overwrite guard, CLI,
  catalog, and secret-value absence behavior.
- README, live-readiness, live-drill, live-hydration, roadmap, findings, and
  scratchpad docs updated.

## Contract Impact

Additive CLI contract:

- `ship live-hydration scaffold --json`
- JSON kind: `ophelia.live_hydration_scaffold`
- Operation: `live_hydration.scaffold`

The command is dry-run by default. With `--write`, it writes template files only
under the scaffold output directory. Existing scaffold files are refused unless
`--force` is supplied.

## Safety Impact

Net positive. Scaffold templates are not consumed by readiness and do not claim
fake env values, secret observations, release metadata, or host capabilities.
No HTTP/Docker probes, provider calls, state refreshes, workflows, or production
mutations are run.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration`
- `python3 -m compileall -q src/ophelia/live_hydration.py src/ophelia/commands/live_hydration.py src/ophelia/command_catalog.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_hydration tests.test_live_drills tests.test_live_readiness`
- `make live-hydration-quark-staging`
- `make live-hydration-scaffold-quark-staging`
- `python3 -m compileall -q src`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Use the scaffold to collect one real app's non-secret runtime env shape and
  release metadata.
- Replace secret-name templates with real provider observations before copying
  anything into provider observation paths.
- Re-run hydration and live drill profiles before enabling opt-in probes.
