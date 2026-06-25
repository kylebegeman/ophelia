---
id: 0013
title: Backup and restore verification platform
date: 2026-06-21
status: landed
areas: [cli, api, backup, restore, verification, readiness, redaction, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 12 adds a backup *verification* platform that turns a recorded backup into
evidence it could actually be restored, without ever touching production data. A
new `ophelia.restore_verification` module exposes `backup_verify_plan` (read-only)
and `backup_verify_apply` (token-gated, rehearsal-isolated), plus
`restore_drills_list`/`restore_drills_show` readers. New `ship backup verify
plan|apply` subcommands and a new `ship restore-drills list|show` group surface
them. Readiness now consumes the latest successful verification receipt
additively.

## Plan is read-only

`backup verify plan` selects the named or latest backup (via `_backup_records`),
reports its freshness (via `backup_status_report`/`_freshness_status`), the
expected data contracts (from `manifest.data`), the isolated rehearsal target it
*would* use (`<runtime_root>/rehearsals/<app>/<backup_id>/`), the resources it
needs, and the typed verification commands it would run. It returns a
`confirmation_token` derived from the canonical apply input (app, environment,
backup id, the backup's checksum digest if present, and the rehearsal target) and
the matching `exact_apply_input`. It writes nothing; a before/after snapshot of
the runtime tree is identical. If no backup exists it emits a `backup_missing`
blocker and no token. The plan also records an explicit note that cleanup of
rehearsal artifacts is a separate future plan, never implicit deletion.

The `confirmation_token` is a non-secret SHA digest, so it is added to the
redaction safe-key list; everything else in the plan is swept with `deep_redact`.

## Apply is rehearsal-only, token-gated, and refuses production paths

`backup verify apply` recomputes the token and rejects an empty or mismatched
token with an `ophelia.error` envelope before doing any work (no receipt is
written). It then refuses any rehearsal target that resolves into a production
app dir (`<runtime_root>/apps/<app>`), the Ophelia repo, or anywhere outside the
rehearsals area, emitting an `unsafe_rehearsal_target` blocker and writing
nothing. Resolution uses fully resolved paths so symlinks and `..` cannot escape.

When safe, it runs file/archive/metadata-level checks and records each as
`{name, ok, message}`:

1. backup file/dir exists;
2. checksum match if a `checksums.sha256` is present (see severity below);
3. archive listing succeeds (`tarfile` listing only, never extracted over
   production; `.tar.zst` is treated as informational);
4. metadata matches the requested app/environment;
5. restore-command rehearsal: default `skipped`, recording the isolated command
   it *would* run (never executed against production);
6. app-defined verify hook: default `skipped`, recording the hook it *would* run.

It writes a receipt under `<runtime_root>/apps/<app>/restore-drills/<verify-id>.json`,
creates the isolated rehearsal target directory for artifacts, sets receipt
`status` to `succeeded`/`failed`, and records `rollback.note = "No source runtime
state was changed."`, `production_data_modified: false`, and
`deleted_anything: false`. It never overwrites production data and never deletes
anything.

## Checksum-missing severity by criticality

A missing `checksums.sha256` is a **blocker** when `pack.portability == "critical"`
(the verification fails) and a **warning** for non-critical packs (verification
can still succeed). This mirrors the rest of the pack-criticality model.

## Readiness consumes verification receipts (additive)

`app_readiness_report` now finds the latest *succeeded* `backup.verify.apply`
receipt (`_latest_successful_backup_verification`) and treats it as a stronger
restore drill: it satisfies the existing `restore_drill` factor and clears the
`restore_drill_missing` blocker even when no legacy restore-drill receipt exists.
A `backup_verification` entry (status + verify id + backup id, names only) is
added to `source_reports`. This is strictly additive: `portability_score` gained
an optional `restore_drill_satisfied` argument that defaults to the legacy
`bool(restore_drills)`, so apps without any verification receipt keep their exact
prior score and the `restore_drill_missing` blocker. `READINESS_SCORE_WEIGHTS`
values are unchanged.

## No secret leakage

Secret *values* from a backup are never read or printed: only names, paths, and
booleans are surfaced, and every plan/receipt/list/show payload is swept with
`deep_redact`. A backup seeded with a `postgres://user:pass@host/db` connection
string canary does not leak the value into any output.

## Changed Areas

- `src/ophelia/restore_verification.py`: new module. `backup_verify_plan`,
  `backup_verify_apply`, `restore_drills_list`, `restore_drills_show`, and the
  `_is_safe_rehearsal_target` safety gate. Reuses `_backup_records`,
  `backup_status_report`, `_freshness_status`, `_restore_drill_receipts`,
  `_read_json`, and the operation-schema envelopes/`token`.
- `src/ophelia/portability.py`: additive readiness integration
  (`_latest_successful_backup_verification`, `restore_drill_satisfied` on
  `portability_score`, a `backup_verification` `source_reports` entry).
- `src/ophelia/commands/backup.py`: `backup verify plan|apply` subcommands and
  two CLI descriptors (`backup verify apply` mutates_state=true,
  requires_confirmation=true, plan_command=`ship backup verify plan`).
- `src/ophelia/commands/restore.py`: new `restore-drills list|show` group plus
  two read-only CLI descriptors; registered in `commands/__init__.py`.
- `tests/test_restore_verification.py`: new coverage (see Verification). No
  existing test file was modified.

## Contract Impact

Additive only. New `ophelia.restore_verification` module, new `ship backup verify
...` and `ship restore-drills ...` subcommands, and new JSON kinds
(`ophelia.restore_drills`, `ophelia.restore_drill`; plan/receipt envelopes for
`backup.verify.plan`/`backup.verify.apply`). No existing CLI, JSON, manifest,
API, receipt, runtime, or policy contract was changed or removed.

## Safety Impact

No VPS, SSH, or secret mutation. Plan is read-only. Apply runs only in an
isolated rehearsal target, is token-gated, refuses any path resolving into a
production app dir or the repo, never overwrites production data, and never
deletes. Destructive DB restore is not executed against production. Only
summaries are emitted and payloads are redacted.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (295 tests, OK)
- `PYTHON=python3 make validate-examples`
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage: plan writes nothing (before/after runtime snapshot identical)
and emits no token when no backup exists; apply rejects empty and wrong tokens
with no work and no receipt, and writes a receipt under restore-drills only with
the correct token while leaving the production app bundle and the source backup
intact; apply refuses a rehearsal target resolving into the production app dir
(`unsafe_rehearsal_target`) and writes nothing; missing checksum is a blocker for
a critical pack and a warning for a non-critical pack; readiness consumes the
latest successful verification receipt (restore_drill factor satisfied,
`source_reports.backup_verification` populated) while an app without one keeps the
`restore_drill_missing` blocker; a connection-string canary never appears in any
plan/receipt/list/show output; and a malformed receipt is skipped rather than
crashing the listing.

## Follow-Ups

- A future phase can add an explicit, plan/apply-gated cleanup command for
  rehearsal artifacts (this phase intentionally never deletes), and an opt-in,
  bounded execution of the restore-rehearsal step inside the isolated target.
