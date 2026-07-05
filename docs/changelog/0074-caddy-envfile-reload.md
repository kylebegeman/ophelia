---
id: 0074
title: Envfile-aware shared Caddy reload
date: 2026-06-25
status: landed
areas: [caddy, deploy, static-sites, tests]
change_type: bug-fix
commits: []
---

## Summary

Shared Caddy reloads now adapt the Caddyfile with `/etc/caddy/env` before
reloading, then reload the adapted JSON config. This keeps `{$OPHELIA_*}`
placeholders available during reloads, matching container startup behavior.

## Why

Static apps can use relative `static_root` values that render Caddy roots with
`{$OPHELIA_STATIC_ROOT}`. Reloading the Caddyfile without the envfile can expand
that placeholder to an empty value and break static-site routing.

## Changed Areas

- `ship caddy reload`: validates first, discovers the shared Caddy container,
  adapts with `/etc/caddy/env`, and reloads the temporary adapted JSON config.
- `ship deploy --apply`: uses the same envfile-aware reload path when shared
  Caddy is already running and the Caddy env file did not change.
- Tests: cover the reload command shape, deploy-time reload behavior, and report
  redaction of envfile values.

## Contract Impact

The JSON report shape is unchanged. The `command` field now shows the
envfile-aware shell command, but it contains only file paths and does not include
envfile values.

## Safety Impact

Reload still validates Caddy config before mutating the live process. The
temporary adapted config is written inside the Caddy container under `/tmp` and
removed after the command exits.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_caddy_manager -v`
- `PYTHONPATH=src python3 -m unittest tests.test_apply_safety -v`
- `PYTHONPATH=src python3 -m unittest tests.test_caddy_manager tests.test_templates tests.test_planning -v`
- `./cli/ship self-test --json`

## Follow-Ups

- None.
