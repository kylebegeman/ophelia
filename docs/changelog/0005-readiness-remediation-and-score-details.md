---
id: 0005
title: Readiness remediation, next actions, and score details
date: 2026-06-21
status: landed
areas: [cli, readiness, scoring, remediation, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 4 makes the readiness report actionable without changing any existing
score. Every blocker/warning now carries a typed remediation when its finding
code is known, the report exposes a priority-sorted `next_actions` list, a
per-category `score_details` roll-up, and compact `source_reports` pointers to
the sub-reports it consumed. All additions are additive and the scoring logic is
unchanged.

- `ship app readiness <app> --json` blockers/warnings gain an optional
  `remediation` (summary, typed Ophelia `commands`, docs, approval flag,
  optional `manifest_patch_hint`) when the code is mapped.
- New top-level `next_actions`, `score_details`, and `source_reports` fields.
- `ship pack validate|explain --json` expose a manifest-only `score_details`
  roll-up by factor category.

## Why

Agents (Lumen, Legacy Console) and operators could read *what* was wrong but had to infer
the fix. Mapping each known finding code to a typed remediation, and rolling the
existing portability score up by category, turns the readiness report into a
ranked to-do list whose commands are safe, typed Ophelia plan forms (never raw
shell). The score itself, and the blocker-hides-nothing contract, are preserved
exactly so no existing test or behavior shifts.

## Changed Areas

- `src/ophelia/remediation.py`: new. `remediation_for(code, *, app, environment)`
  returns a typed `Remediation` for known finding codes or `None`. Codes were
  enumerated from the literal strings emitted by `pack_validation_report`,
  `env_shape_diff_report`, `backup_status_report`, the route-conflict path, and
  the direct readiness checks: `restore_drill_missing`, `release_missing`,
  `env_key_missing`, `env_key_placeholder`, `backup_not_fresh`,
  `backup_contract_missing`, `route_conflict`, `duplicate_domain`,
  `duplicate_route`, `missing_verification_checks`,
  `production_missing_verification`, `production_image_without_digest`,
  `production_writable_bind_without_backup`, `service_missing_image`,
  `hook_path_outside_allowlist`, `critical_volume_missing_export`,
  `critical_volume_missing_import`, `app_postgres_missing_volume`,
  `postgres_data_contract_inferred`, `redis_data_contract_inferred`,
  `critical_postgres_missing_export`, `critical_postgres_missing_import`,
  `critical_redis_missing_export`, `critical_redis_missing_import`,
  `restore_drill_not_required`, `offsite_backup_not_required`,
  `critical_app_shared_postgres`, `durable_redis_shared_logical_db`,
  `critical_postgres_missing_verify`, `critical_redis_missing_verify`,
  `extra_runtime_env_key`. Every command is a typed `ship ...` form
  parameterized with the app/environment; mutating remediations set
  `requires_human_approval=True`. Unknown codes return `None`.
- `src/ophelia/portability.py`:
  - New module constant `READINESS_SCORE_WEIGHTS` (factor name -> weight, summing
    to 100). `portability_score` now reads each weight from it; the values match
    the previous literals exactly, so scores are identical.
  - `app_readiness_report` is extended additively: blockers/warnings are
    replaced with enriched copies via `attach_remediation` (existing
    code/message/path preserved), and three new keys are added: `next_actions`
    (priority-sorted `{code, area, command, summary}`, blockers before
    warnings), `score_details` (per-category `{points, max_points, ok_factors,
    total_factors, reason}`; category points sum to the score and max_points sum
    to 100), and `source_reports` (compact pointers to `env_shape`,
    `backup_status`, `route_conflicts`, `secrets_audit`, `restore_drill`). The
    `secrets_audit` pointer is produced by calling the Phase 3
    `secrets_audit(...)` module (lazy import to avoid a cycle), names only.
  - `pack_validation_report` now includes a manifest-only `score_details`
    roll-up (`_pack_quality_score_details`) covering the factors pack validation
    can assess (pack metadata, explicit data contracts, image digests, checks
    pass); `pack_explain_report` surfaces it under `movement_readiness`.
- `src/ophelia/commands/pack.py`: `pack validate` non-JSON output prints the
  score-detail roll-up; `--json` already passes the full report through.
- `tests/test_readiness_remediation.py`: new coverage (see Verification).

## Contract Impact

Additive only. Readiness blockers/warnings gain an optional `remediation` key;
the report gains `next_actions`, `score_details`, and `source_reports`. Pack
validate/explain gain `score_details`. No existing JSON key was removed or
renamed, the portability score and `readiness_level` logic are unchanged, and no
existing test was modified.

## Safety Impact

None. The readiness and pack commands remain read-only. No VPS is touched, no
SSH is performed, and no secret value is printed: remediation `commands` are
typed Ophelia plan forms (never raw shell), mutating remediations set
`requires_human_approval=True`, and `source_reports`/`secrets_audit` carry names
and counts only with `values_redacted: True`. A high score never hides blockers:
`readiness_level` stays `blocked` whenever any blocker exists.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (180 tests, OK)
- `./cli/ship app readiness demo-service --environment production --json`
  (pure JSON; new keys present; `score_details` points sum to score, max_points
  sum to 100; `secrets_audit` pointer redacted)
- `./cli/ship pack validate examples/demo-docs-production-mirror.ophelia.yml
  --json` (`score_details` present)
- `PYTHON=python3 make validate-examples`
- `git diff --check`

## Follow-Ups

- Phase 5 receipt timeline and dry-run diff artifacts build on these
  `next_actions`/`source_reports` pointers.
