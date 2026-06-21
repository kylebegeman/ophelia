---
id: 0006
title: Receipt timeline and dry-run diff artifacts
date: 2026-06-21
status: landed
areas: [cli, receipts, planning, traffic, redaction, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 5 makes recent operations browsable as a timeline and makes dry-run plans
carry redacted before/after diffs. Both additions are read-only, additive, and
secret-free: nothing mutates the VPS, no SSH is performed, and no secret value is
ever written to a plan, a receipt, or a diff artifact file.

- New `ship receipts timeline` lists receipts newest-first with composable
  filters and per-entry artifact/rollback facets.
- `deploy --plan` now emits a structured `changes` list plus a redacted
  `compose-diff` artifact whose file contents are masked before the diff is even
  computed.
- `app traffic plan` now attaches structured, redacted provider/DNS/Caddy
  before-after `changes`.

## Why

Agents (Lumen, Quark) and operators could list receipts but not slice them by
time, operation, or status, and a deploy plan reported *which* files changed but
not *how*. Phase 5 closes both gaps while keeping the redaction contract intact:
diff content is masked at the source, and only artifact *paths* (never inline
content) ride along in the JSON, so `json.dumps(plan)` stays secret-free.

## Changed Areas

- `src/ophelia/receipt_index.py`: new. `receipt_timeline(runtime_root, *, app,
  environment, operation, status, since, until)` wraps
  `portability._receipt_records` (the single source of truth for receipt
  locations) and returns `{schema_version, kind:"ophelia.receipt_timeline",
  filters, receipts, warnings, summary}`. Behavior:
  - Newest-first sort by `(started_at, receipt_id)` descending.
  - Exact-match `operation`/`status` filters; inclusive ISO `since`/`until`
    bounds on `started_at` (a bare `until` date covers the whole day). Filters
    compose with AND semantics.
  - Each entry adds `artifact_paths` (artifact `path`/`name` strings only) and
    `rollback_available` (from payload `rollback.available`, else `False`).
  - A receipt that cannot be read or parsed becomes a
    `{code:"receipt_unreadable", message, path}` entry in `warnings` and is
    skipped; the function never raises. Because `_receipt_records` already drops
    malformed JSON silently, the artifact/rollback extraction re-parses each
    receipt defensively so a bad payload surfaces as a warning instead of being
    invisible.
  - `receipt_timeline_for_state(...)` returns just the entry list for Phase 6.
- `src/ophelia/commands/receipts.py`: adds the `timeline` subcommand
  (`--app --environment --operation --status --since --until --runtime-root
  --json`). `--json` prints pure JSON; the human form prints one line per receipt
  (`id  operation  status  started_at  rollback|no-rollback`) and one line per
  warning. Registers a `ship receipts timeline` CLI descriptor
  (`json_kind: ophelia.receipt_timeline`, read-only, no confirmation).
- `src/ophelia/operation_schema.py`: new `diff_artifact(name, path, description,
  redacted=True, media_type="text/x-diff")` returning
  `{name, kind:"ophelia.artifact.diff", media_type, path, redacted[, description]}`.
  Carries the artifact *path* only, never inline diff content.
- `src/ophelia/planning.py`: `deploy_plan` gains an optional `artifacts_dir`
  parameter and two additive plan keys:
  - `changes`: per-changed/removed file `{path, change, target}` where `target`
    is `compose|caddy|env|manifest|other`. Paths and change types only, no
    content.
  - `artifacts`: when `compose_changes` is non-empty, a `compose-diff` diff
    artifact. The current runtime `compose.yml` and the rendered desired
    `compose.yml` are both routed through `redaction.redacted_compose_text`
    *before* `difflib.unified_diff`, so the written file
    (`<artifacts_dir>/compose-diff.diff`) can never contain a secret value.
    `artifacts_dir` defaults to `runtime_root/plans/<operation-id>`; writing is
    best-effort (`OSError` skips the artifact, never crashes the plan).
- `src/ophelia/commands/deploy.py`: adds an optional `--artifacts-dir` flag
  threaded into the `deploy --plan` path. The apply path is unchanged.
- `src/ophelia/portability.py`: `traffic_plan` now attaches structured,
  redacted provider changes via the existing `plan_envelope(changes=...)`
  channel (`_traffic_provider_changes`): a `dns_record` and a `caddy_site` entry
  per route with `before`/`after` owners, plus a `provider_execution` summary
  whose `dns`/`caddy` provider dicts are passed through `redaction.redact_mapping`
  so a Cloudflare token value can never appear (the `api_token_env` reference is
  itself masked; only names/hosts/targets survive).
- `tests/test_receipt_timeline.py`, `tests/test_dryrun_diffs.py`: new coverage
  (see Verification). No existing test file was modified.

## Contract Impact

Additive only. New `receipt_timeline` read-model and `ship receipts timeline`
command. `deploy --plan` JSON gains `changes` and `artifacts`; the traffic plan
gains `changes`. `receipts list`/`receipts show` and the existing
`changed_files`/`removed_files`/`compose_changes` keys are untouched. No existing
JSON key was removed or renamed.

## Safety Impact

None to live infrastructure. The timeline and both plan paths are read-only: no
VPS, no SSH, no secret value emitted. Diff content is redacted at the source
before the diff is computed; plans carry only artifact paths, so
`json.dumps(plan)` stays secret-free. Malformed receipts degrade to warnings
rather than crashing.

## Diff artifact shape

```json
{
  "name": "compose-diff",
  "kind": "ophelia.artifact.diff",
  "media_type": "text/x-diff",
  "path": "<runtime-root>/plans/<operation-id>/compose-diff.diff",
  "redacted": true,
  "description": "Rendered Compose diff with env values redacted."
}
```

The referenced `.diff` file is a unified diff of the redacted current vs desired
`compose.yml`; env values appear only as `<redacted>`.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (195 tests, OK)
- `PYTHON=python3 make validate-examples`
- `./cli/ship receipts timeline --json | python3 -m json.tool` (pure JSON)
- `git diff --check`

## Follow-Ups

- Phase 6 imports `receipt_index` to back the runtime-state SQLite read-model.
