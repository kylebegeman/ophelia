---
id: 0072
title: Lumen deploy ergonomics
date: 2026-06-25
status: landed
areas: [deploy, caddy, verification, docs, tests]
change_type: feature
commits: []
---

## Summary

Ophelia deploys can now receive app-owned release metadata directly, shared
Caddy reload has structured container discovery diagnostics, and internal
verification accepts loopback URL compatibility while keeping path-first checks.

## Why

Lumen release deploys should not need shell glue to translate app release
identity into Ophelia runtime env, discover the active Caddy container, or
normalize internal service checks.

## Changed Areas

- `src/ophelia/commands/deploy.py`: adds `--release-id`, `--commit-sha`, and
  `--build-time`.
- `src/ophelia/runtime.py`: resolves deploy metadata from CLI input, env, or
  generated fallback and stops reading the Ophelia repo git SHA for app
  metadata.
- `src/ophelia/remote.py`: forwards deploy metadata flags through SSH deploys.
- `src/ophelia/caddy_manager.py`: validates then reloads the discovered shared
  Caddy container with structured diagnostics.
- `src/ophelia/manifest.py`: lets `type: internal` checks use loopback URLs as
  compatibility input and rejects external internal URLs.
- `README.md`, `docs/manifest-spec.md`, `docs/host-contract.md`: document the
  new operator workflow.
- `tests/test_release_history.py`, `tests/test_remote.py`,
  `tests/test_caddy_manager.py`, `tests/test_manifest.py`: focused coverage.

## Contract Impact

- `ship deploy` supports:
  - `--release-id`
  - `--commit-sha`
  - `--build-time`
- Env fallback order is CLI flag, then `OPHELIA_DEPLOY_*` or compatible
  `OPHELIA_*` env, then generated fallback. `GITHUB_SHA` is also accepted for
  commit SHA.
- `ship caddy reload --json` returns `kind`, `ok`, `container`, `validated`,
  `reloaded`, `runtime_root`, `config_path`, `warnings`, and `errors`.
- `verify[].type: internal` accepts `path` or loopback `url`. External URLs for
  internal checks are manifest errors.

## Safety Impact

Deploy metadata flags only affect rendered runtime metadata and release records.
They do not change confirmation-token behavior, deploy scope, or secret
handling.

`ship caddy reload --json` is mutating: it validates Caddy config first, then
reloads the running shared Caddy container. It does not edit files, recreate
containers, deploy apps, rotate secrets, or touch production data.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_release_history tests.test_remote tests.test_caddy_manager tests.test_manifest -v`

## Follow-Ups

- None.
