---
id: 0004
title: Provider, secret, and route validation
date: 2026-06-21
status: landed
areas: [cli, providers, secrets, conflicts, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 3 adds three read-only validation surfaces that let agents (Lumen, Quark)
and operators check the risky edges of a move before any mutation is planned,
without ever exposing a secret value or touching a VPS.

- `ship providers validate --config PATH` and `ship providers explain --config
  PATH` validate and explain a traffic provider config (DNS/Caddy). Validation
  blocks unknown provider types, missing per-type required fields, invalid TTL,
  Caddy `sites_dir` outside the runtime root when a reload/mutation is
  requested, and any literal provider secret. It requires `*_env` references
  instead.
- `ship secrets audit <manifest-or-app>` produces a names-only view of which env
  keys an app needs, where each comes from, and whether it is present in the
  runtime env (and optionally the process env). It never emits a value.
- `ophelia.conflicts` now enriches every owner with `route_source` and, for
  active runtime owners, `runtime_bundle_path`, so two active apps claiming the
  same host+path surface as a blocker with full owner provenance.

## Why

A move is only as safe as its DNS/Caddy config and its secrets. Agents needed a
way to (a) confirm a provider config is well-formed and free of inline secrets
before planning provider writes, (b) confirm required secrets are in place
without reading values, and (c) tell two live apps fighting over the same route
apart from the same app appearing twice. These surfaces are the preflight gates
the readiness and policy phases build on.

## Changed Areas

- `src/ophelia/provider_config.py`: new. `validate_ttl(value)` is the single
  source of truth for the accepted Cloudflare TTL range (1, or 30..86400),
  matching what the traffic plan path has always enforced.
  `validate_provider_config(config_path)` emits kind
  `ophelia.provider_config.validation` with per-provider and aggregate
  `blockers`/`warnings` and a `status` of `ok|warn|blocked`. It blocks unknown
  provider types, missing required fields (`record_file`; `zone_id`/
  `api_token_env`; `sites_dir`), invalid TTL, non-https `base_url`, Caddy
  `sites_dir` outside `<runtime_root>/caddy/sites.d` on reload/mutation, and any
  literal `api_token`/`token`/`secret`/`password`/`private_key` value
  (`provider_token_must_be_env_ref`) referencing only the key path, never the
  value. `explain_provider_config(config_path)` (kind
  `ophelia.provider_config.explanation`) describes what each provider would do
  and lists env refs by NAME. Both route output through `redact_mapping`.
- `src/ophelia/portability.py`: imports `validate_ttl` and the Cloudflare TTL
  check in the traffic plan path (`_traffic_plan_core`, ~line 1538) now calls it
  instead of an inline `ttl != 1 and not 30 <= ttl <= 86400` literal, so the CLI
  and provider-config validator cannot drift. Accepted values are unchanged.
- `src/ophelia/secrets_audit.py`: new. `secrets_audit(manifest_or_app,
  environment, runtime_root, include_process_env)` (kind
  `ophelia.secrets_audit`) reuses `env_shape_diff_report` /
  `_desired_env_entries` for env scanning and optionally folds in provider env
  refs from a discoverable provider config (by NAME). Each key carries `name,
  required, source, sources, status, runtime_present, present, example_present,
  value_redacted:True` and never a value. `--include-process-env` only sets
  presence booleans from `os.environ`; it never reads a value. Missing required
  keys are blockers; extra runtime keys are warnings (names only).
- `src/ophelia/commands/providers.py`: new `ship providers validate|explain
  --config PATH [--json]`. Missing `--config` or missing file is a clear error
  envelope, exit 1; exit 1 when `status` is `blocked`. Registers two CLI
  descriptors (read-only, low risk) on import.
- `src/ophelia/commands/secrets.py`: adds `ship secrets audit <manifest-or-app>
  [--environment] [--runtime-root] [--include-process-env] [--json]` calling
  `secrets_audit`. The existing `secrets required` subcommand is unchanged.
  Registers a CLI descriptor on import.
- `src/ophelia/commands/__init__.py`: registers the `providers` command group.
- `src/ophelia/conflicts.py`: `_owner(...)` now adds `route_source` (mirrors
  `source`) and, for active runtime owners, `runtime_bundle_path` (the lock
  path's app dir). No existing key removed; route/domain collisions between two
  active apps already surface as blockers and now carry full owner provenance.
- `tests/test_provider_config.py`, `tests/test_secrets_audit.py`: new coverage.
  `tests/test_conflicts.py`: extended with a two-active-apps route-collision
  test asserting a blocker plus `route_source`/`runtime_bundle_path` on owners.

## Contract Impact

Additive only. New CLI commands `ship providers validate`, `ship providers
explain`, and `ship secrets audit`; three new command-catalog descriptors; new
JSON kinds `ophelia.provider_config.validation`,
`ophelia.provider_config.explanation`, and `ophelia.secrets_audit`. Conflict
owners gain `route_source` and (for active runtime owners) `runtime_bundle_path`
without removing or changing existing keys. No existing JSON key, CLI behavior,
or test was changed.

## Safety Impact

None. All three surfaces are read-only. No VPS is touched, no SSH is performed,
and no secret value is ever printed or returned: provider validation references
only key paths and runs through `redact_mapping`; the secrets audit reports
names and presence booleans only; `--include-process-env` checks presence
without reading values. TTL validation moved to a single source so the accepted
range cannot drift between the CLI plan path and the validator.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (174 tests, OK)
- `PYTHON=python3 make validate-examples`
- `ship providers validate --config <ok>.json --json` (status ok)
- `ship providers validate --config <raw-token>.json --json` (status blocked,
  `provider_token_must_be_env_ref`, token value absent from output)
- `ship secrets audit examples/dragonwriter.ophelia.yml --environment production
  --json` (pure JSON)
- `git diff --check`

## Follow-Ups

- Phase 4 readiness imports `secrets_audit` for the "required secrets present"
  check rather than re-scanning env.
