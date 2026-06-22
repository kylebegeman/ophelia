---
id: 0008
title: Deterministic safety-policy engine
date: 2026-06-21
status: landed
areas: [cli, policy, planning, traffic, portability, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 7 adds a deterministic, typed safety-policy engine. A declarative policy
file (`config/ophelia-policy.yml`) names operations, optional environments, and
typed `require` conditions; the new `ophelia.policy` engine validates the policy
structurally and evaluates an operation's context against the matching rules.
The result is surfaced three ways: a new `ship policy` command group
(`validate` / `explain` / `evaluate`), and an additive `policy` entry appended to
the `checks` list of the deploy, traffic, export, and import plan builders.

- New `ship policy validate`, `ship policy explain`, `ship policy evaluate`
  CLI commands (all read-only, `risk: low`, no mutation).
- New `config/ophelia-policy.yml` repo-default policy with conservative
  production-safety rules.
- Plan builders embed a best-effort policy evaluation under `checks` only; no
  plan's top-level `blockers`, `status`, or `confirmation_*` is changed.

## Why

Ophelia already encodes safety as per-action `policy_gates` strings and per-plan
blockers, but there was no single declarative place to assert "production
traffic cutover must verify target health and keep a rollback path" and no way
for an agent to evaluate that assertion against an operation's facts before
running it. Phase 7 makes those safety properties explicit, typed, and
auditable, while keeping evaluation deterministic (no expression language).

## Fail-closed and fail-open semantics

The engine distinguishes two kinds of "unknown" with opposite stances:

- **Fail closed (evaluation).** A rule whose `require` references a condition key
  the engine cannot evaluate is treated as a **blocker**, regardless of its
  declared severity, with code `policy_unknown_required_condition`. The engine
  cannot prove the safety property the operator intended, so it refuses to pass.
- **Fail open (validation/schema).** Unknown top-level keys, unknown rule keys,
  and `require` conditions that reference an unknown condition key produce
  **warnings** during `validate_policy`, never blockers. This lets the policy
  schema grow (new advisory metadata) without breaking older engines. Note the
  asymmetry is intentional: an unknown condition is a *warning* at validation
  time but the *same* condition fails *closed* (blocker) at evaluation time.

Known, evaluable condition keys: `target_health_check`, `rollback_available`,
`confirmation_required`, `plan_exists`, `json_receipts`, `image_digest_pinned`,
`provider_config_validated`, `restore_drill_present`, `readiness_clean`,
`backup_fresh`.

An operation with no matching rules evaluates to `ok` with an empty `rules`
list: the policy intentionally asserts nothing about operations it does not
mention. Rule selection is by exact `operation` match and (unset-or-equal)
`environment`; selected rules are sorted by `id` for deterministic output.

## Policy file resolution order

`load_policy(path, runtime_root)` resolves in this order:

1. An explicit `--policy PATH` when given. A missing or unparseable explicit
   path raises a clear `PolicyError` (it does **not** silently fall through to a
   default).
2. `<runtime_root>/policy/ophelia-policy.yml` (operator override).
3. The repo default `config/ophelia-policy.yml`.

## Changed Areas

- `config/ophelia-policy.yml`: new repo-default policy. `version: 1`,
  `defaults` (advisory), and conservative `rules`:
  `production-traffic-health-check` (blocker: `target_health_check` +
  `rollback_available` for `app.traffic.apply`),
  `production-deploy-confirmation` (blocker: `confirmation_required` +
  `plan_exists` for `deploy.apply`), `production-deploy-image-digest-pinned`
  (warning), `production-export-restore-drill` (warning),
  `production-export-provider-config` (info), and `traffic-plan-health-check`
  (warning for `app.traffic.plan`).
- `src/ophelia/policy.py`: new engine. `load_policy`, `validate_policy`
  (kind `ophelia.policy_validation`), `evaluate_policy` (kind
  `ophelia.policy_result`), `explain_policy` (kind `ophelia.policy_explanation`),
  and `policy_check_entry` (best-effort plan integration helper). Pure
  JSON-serializable output; no secret values.
- `src/ophelia/commands/policy.py`: new `policy` command group with `validate`,
  `explain`, and `evaluate` subcommands. `--json` emits pure JSON. `validate`
  and `evaluate` exit `1` when status is `blocked`. Registers three CLI
  descriptors (all `risk: low`, `mutates_state: false`). The `evaluate` context
  is built from optional `--has-…`/`--<fact>` boolean flags; any fact not
  supplied is treated as not-yet-satisfied, so an evaluation with no facts shows
  what *would* block.
- `src/ophelia/commands/__init__.py`: registers the new `policy` group.
- `src/ophelia/planning.py` (`deploy_plan`): seeds a `checks` list with a
  best-effort `policy` entry for `deploy.apply` (context:
  `confirmation_required`, `plan_exists`, `image_digest_pinned`,
  `json_receipts`).
- `src/ophelia/portability.py`: appends a `policy` check to `traffic_plan`
  (`app.traffic.apply` context: `target_health_check`, `rollback_available`,
  `confirmation_required`, `readiness_clean`), `export_plan`
  (`app.export.create` context: `restore_drill_present`,
  `confirmation_required`, `image_digest_pinned`, `json_receipts`,
  `provider_config_validated`), and `import_plan` (`app.import.apply` context:
  `confirmation_required`, `rollback_available`, `readiness_clean`,
  `json_receipts`).
- `tests/test_policy.py`: new coverage (see Verification). No existing test file
  was modified.

## Contract Impact

Additive only. New `policy` engine, three `ship policy ...` commands, one new
config file, and a `policy` entry appended to existing plans' `checks` lists. No
existing CLI, JSON, manifest, API, receipt, or runtime contract was changed or
removed. Crucially, **no existing plan's top-level `blockers`, `status`, or
`confirmation_*` was modified**: the policy result lives only under `checks`, so
existing plan tests stay green.

## Safety Impact

None to live infrastructure. All `policy` operations are read-only; evaluation
never executes the operation it scores. Plan integration is best-effort and
wrapped so a missing or broken policy file degrades to a non-blocking `policy`
check with a `message`, never a crash and never a new top-level blocker. No VPS
mutation, no SSH, and no raw env value is read or printed. The engine's
fail-closed stance means a policy that references a condition Ophelia cannot
prove will block rather than silently pass.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (216 tests, OK)
- `PYTHON=python3 make validate-examples`
- `./cli/ship policy validate --json` (pure JSON; exit 0)
- `./cli/ship policy explain --json` (pure JSON)
- `./cli/ship policy evaluate --operation app.traffic.apply --app dragon-writer
  --environment production` (blocked, exit 1); same with
  `--has-target-health-check --has-rollback` (ok, exit 0)
- `git diff --check`

New test coverage: repo-default policy validates with no blockers; `rules`
not-a-list and a rule missing `severity` both block; an unknown advisory/top-level
key is a warning, not a blocker (fail open); an unknown required condition makes
`evaluate_policy` return `blocked` with `policy_unknown_required_condition`
(fail closed); production `app.traffic.apply` with no `target_health_check` is
blocked by `production-traffic-health-check`, and ok with health + rollback
present; an unknown operation matches no rules and is `ok`; environment scoping
excludes non-matching environments; and `deploy_plan` includes a `policy` entry
under `checks` carrying `kind: "ophelia.policy_result"`.

## Follow-Ups

- The `policy_results` SQLite table (seeded empty in Phase 6) is intentionally
  left empty here; populating it via `state_db.rebuild_state` from receipts/plans
  is a clean future addition that does not require changing the engine.
