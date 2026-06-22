---
id: 0031
title: Live test baseline and secret metadata
date: 2026-06-22
status: landed
areas: [live-readiness, live-drills, redaction, config, docs, tests]
change_type: hardening
commits: []
---

## Summary

Started read-only live testing against local manifests and `~/ophelia-runtime`,
added local live drill profiles, and fixed live-readiness over-redaction of
secret-provider metadata.

## Why

The first live test pass needs repeatable real profile commands and useful
blocker summaries. Compact secret-provider status/count metadata is safe to show
and necessary for operators to understand missing secret-name observations.

## Changed Areas

- `config/ophelia-live-drills.yml`: adds local file baseline plus Quark Ops
  production/staging focused profiles.
- `src/ophelia/live_drills.py`: expands `~` in profile paths.
- `src/ophelia/live_readiness.py`: preserves secret-provider metadata
  containers while still redacting actual secret-shaped values.
- `tests/test_live_drills.py` and `tests/test_live_readiness.py`: cover local
  profile loading, path expansion, and visible secret-provider metadata.
- Private operator notes recorded the first read-only live baseline and next
  safe steps; those scratchpad notes are intentionally not part of the public
  tree.
- README and live drill docs updated with local profile usage.

## Contract Impact

Additive local profile file:

- `config/ophelia-live-drills.yml`

Live-readiness JSON now keeps metadata visible under:

- `secret_provider_status`
- app `checks.secrets`

Actual secret values, connection strings, and sensitive command arguments remain
redacted.

## Safety Impact

Net positive. All live tests were read-only and file-based. HTTP probes, Docker
probes, provider mutation, workflow execution, state refresh, and apply/create
commands were not run.

## Verification

- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_live_drills tests.test_live_readiness`
- `./cli/ship live-readiness run --runtime-root ~/ophelia-runtime --manifests-dir manifests --host-config config/ophelia-hosts.yml --provider-config config/ophelia-integrations.yml --allow-blocked --json`
- `./cli/ship live-drills run quark-ops-production-file-baseline --profiles config/ophelia-live-drills.yml --json`
- `./cli/ship live-drills run local-file-baseline --profiles config/ophelia-live-drills.yml --json`
- `python3 -m compileall -q src/ophelia/live_readiness.py src/ophelia/live_drills.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add real host capability observations.
- Add secret-name observations, not secret values, for one target app.
- Add release/env shape snapshots for one target app before opt-in HTTP/Docker
  probes.
