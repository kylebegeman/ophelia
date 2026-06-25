---
id: 0014
title: Production traffic controller hardening
date: 2026-06-21
status: landed
areas: [cli, traffic, policy, provider-config, redaction, portability, foundation, docs, changelog]
change_type: hardening
commits: []
---

## Summary

Phase 13 hardens the existing production traffic controller without changing its
public surface. The traffic plan now validates any supplied provider config
through the single canonical validator before it will mint an apply token, binds
the confirmation token to every behavior-changing input (including a non-secret
digest of the provider config), gates a production apply on the deterministic
safety policy (target health + rollback availability), and refuses a rollback
that would delete a DNS record or Caddy file the forward apply created unless an
explicit approval flag is set. A new read-only `ship traffic status` command
summarizes an app's traffic state from receipts and local files with no provider
credentials and no live probing. All existing traffic behavior and the existing
traffic test suite are preserved unchanged.

## Token input set (computed identically by plan and apply)

The `app.traffic.apply` confirmation token is derived from a canonical input that
now includes every behavior-changing field, so a stale or mismatched plan can
never confirm a different apply:

- `app`, `environment`, `from` (source host), `to` (target host)
- `target_origin`
- `dns_provider`, `caddy_provider`, `ttl`
- `provider_config` (path) **and** `provider_config_digest` (a truncated SHA-256
  of the provider config file bytes, never the raw secrets)
- `execute_provider_mutation`
- `target_health_url`, `run_target_health`, `target_health_timeout`,
  `target_health_expect_status`

`traffic_apply` recomputes this identical canonical input by re-running
`traffic_plan` with the same arguments and comparing `confirm` to the freshly
derived `confirmation_token`. Because both paths build the token from the same
fields in the same way, a plan -> apply round trip using the returned token still
validates, while an apply whose target origin, provider config (digest), or
health parameters differ from the plan that issued the token is rejected with
`confirmation_token_mismatch`. The provider digest is one-way and never carries a
secret; a literal secret in the config is independently blocked by the validator.

## Provider-config validation gate

When a provider config is supplied for mutation, the plan runs it through
`provider_config.validate_provider_config` (the single validator) before any
apply token is produced. If the validator returns `blocked` (for example, a
literal `api_token`/`token`/`secret` value, a missing `zone_id`, an invalid TTL,
or a Caddy `validate`/`reload` `sites_dir` outside `<runtime_root>/caddy/sites.d`),
those blockers are surfaced under the plan blockers and the apply token is
**withheld** (`confirmation_token` is `null`, `provider_config_blocked` is true).
Manual / no-mutation plans and plans without a provider config are unaffected.

To keep the validator and the executor from disagreeing, the validator's Caddy
`sites_dir` constraint was aligned to fire on `validate`/`reload` (the operations
that actually run `caddy validate`/`reload` against the runtime root), matching
`portability._plan_file_caddy_provider`. A plain local file write (`allow_mutation`
without `validate`/`reload`) is not constrained, exactly as the apply path has
always behaved. No existing test depended on the prior, broader trigger.

## Production-health policy gate

The plan builds a policy context reflecting whether a usable target health check
is present (`target_health_present`: a configured health URL that was not executed
or was executed and passed; a configured URL that was executed and failed does
*not* count) and whether a rollback path is available. The policy result is always
attached additively under `checks` (kind `ophelia.policy_result`). For a
**production** apply, the policy result is also a real gate: a policy blocker (for
example, the `production-traffic-health-check` rule when no health check is
present) is surfaced as a `traffic_policy_blocked` plan blocker and **withholds**
the apply token. For non-production environments the policy result stays purely
additive and never blocks, preserving prior behavior (and the existing
`app.traffic.plan` staging behavior is unchanged).

## Rollback refuses unsafe deletes

`traffic_rollback_plan`/`traffic_rollback_apply` read the prior provider state
captured by the forward apply receipt (previous DNS records, the previous Caddy
site file presence and snapshot, previous Cloudflare records). A rollback that
would **delete** a record or file that did not exist before the forward apply
(i.e. the apply created it, so there is no prior state to restore) is refused with
a `rollback_unsafe_delete` blocker, and the rollback token is withheld. Passing an
explicit `--approve-unsafe-delete` flag authorizes the delete: the flag is part of
the rollback token input (so plan and apply must agree), and the local file
providers then remove the created record/file. Restore-only rollbacks (the common
case, where prior state exists) are unaffected.

## `ship traffic status`

A new top-level read-only `traffic` group exposes
`ship traffic status --app X --environment production [--runtime-root] [--manifest] [--json]`.
The backing `portability.traffic_status` combines the latest traffic apply
receipt, the latest rollback receipt, the provider plan recorded on the latest
apply, route ownership (from the manifest and conflict scan), the target health
status captured on the latest receipt (no live probe), and any active blockers.
It reads receipts and local files only, never the network, and works without any
provider credentials (`credentials_required: false`, `live_probe_performed:
false`). Output kind is `ophelia.traffic_status` and is swept through
`redact_mapping`.

## Redaction

All provider outputs continue to redact credentials. The Cloudflare token VALUE
is never stored: only the `api_token_env` env-ref name is recorded, and the
provider digest is a one-way hash of the file. The new `provider_config_digest`,
`confirmation_token` (already a non-secret SHA), and `traffic_status` output carry
no secret values. Caddy live reload stays opt-in via provider config
(`allow_reload`).

## Changed Areas

- `src/ophelia/portability.py`: extended the `app.traffic.apply` token input with
  the provider digest and all behavior-changing fields; added the
  provider-config validation gate (`_traffic_provider_config_validation`),
  `_traffic_provider_config_digest`, and `_traffic_target_health_present`; made the
  production policy result a token-withholding gate; threaded
  `approve_unsafe_delete` through `traffic_rollback_plan`/`apply` and the rollback
  change/executor helpers with a `rollback_unsafe_delete` refusal; added the
  read-only `traffic_status` and `_latest_record_for`.
- `src/ophelia/provider_config.py`: aligned the Caddy `sites_dir` constraint to
  fire on `validate`/`reload` (matching the executor), not plain `allow_mutation`.
- `src/ophelia/commands/traffic.py`: new `ship traffic status` group and read-only
  CLI descriptor; registered in `commands/__init__.py`.
- `src/ophelia/commands/app.py`: added `--approve-unsafe-delete` to the traffic
  rollback plan/apply subcommands and passed it through.
- `tests/test_traffic_hardening.py`: new coverage (see Verification). No existing
  test file was modified.

## Contract Impact

Additive. The new token input fields are computed identically by plan and apply,
so a plan-issued token still validates. The provider-validation and
production-health gates only *withhold* a token in scenarios that already provide
the needed input (valid provider config, target health on production); they never
weaken an existing gate. New `ship traffic status` command, new
`ophelia.traffic_status` JSON kind, new `--approve-unsafe-delete` flag, and new
plan/blocker fields (`provider_config_digest`, `provider_config_blocked`,
`target_health_present`). No existing CLI, JSON, manifest, API, receipt, runtime,
or policy contract was removed or changed.

## Safety Impact

No VPS, SSH, or secret mutation; no production overwrite. Plan is read-only. A
blocked provider config and a production apply without a target health check both
withhold the apply token. Rollback refuses to delete records/files the forward
apply created unless explicitly approved. The Cloudflare token value is never
stored. `ship traffic status` performs no network probe and needs no credentials.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (304 tests, OK)
- `PYTHON=python3 make validate-examples`
- `./cli/ship traffic status --app demo-service --environment production --json`
  (parses as JSON)
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage (`tests/test_traffic_hardening.py`): plan mutates no active
runtime files (before/after identical); apply rejects a token from a plan with a
different target origin and from a plan with a different provider-config digest; a
blocked provider config (literal secret) withholds the apply token and never
echoes the secret; a production apply without a target health check is blocked by
policy with no token, while one with health + rollback proceeds with a token; a
Cloudflare canary token VALUE never appears in the plan, receipt, on-disk
checkpoint files, or status (only the env-ref name does); a rollback that would
delete a forward-created DNS record is blocked with `rollback_unsafe_delete` and
no token unless `approve_unsafe_delete` is set, after which it deletes the created
record; and `traffic_status` returns parseable JSON with no apply receipt and
after one, with no provider credentials present.

## Follow-Ups

- A future phase can add an opt-in, bounded live health re-probe to
  `ship traffic status` (still defaulting to receipt-only), and richer route
  ownership reconciliation against the running edge.
