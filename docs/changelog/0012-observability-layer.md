---
id: 0012
title: Built-in observability and telemetry layer
date: 2026-06-21
status: landed
areas: [cli, api, observability, manifest, schema, lumen, redaction, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 11 adds a lightweight, read-only observability layer. A new optional
`observability` manifest block declares a health endpoint, a metrics endpoint,
and container-log retention. A new `ophelia.observability` module composes a
*configured + locally-derived* status for an app/environment, and a new
`ship observability plan|status|export` command group surfaces it. The Lumen
dashboard now fills its previously reserved per-app `observability` key with a
compact summary.

## Why

Operators and agents need a single read-only place to see whether an app is
configured for monitoring and whether its local signals (release present, backup
fresh, restore drill recorded, recent receipt failures) look healthy, without
running a full readiness report or touching the network.

## Read-only by default; probes are opt-in and bounded

- `observability status` is **read-only** and by **default makes no network or
  Docker call**. It reports the declared observability config plus statuses
  derived only from local files: release metadata (`active_release`), backup
  freshness (`backup_status_report`), restore-drill receipts
  (`_restore_drill_receipts`), and a rolled-up receipt failure count
  (`_receipt_records`, counting `failed`/`error`/`blocked`). These existing
  contracts are reused, not reimplemented.
- A live HTTP health probe runs **only** with `--probe-http`. It is a single
  `urllib` GET with a bounded timeout (default 5s, capped at 30s). Any failure or
  timeout becomes a **warning**, never a crash and never a hang. A test against a
  guaranteed-unreachable address (`http://127.0.0.1:1/health`) with a short
  timeout, and a monkeypatched `TimeoutError`, both confirm the probe degrades
  cleanly.
- A Docker check runs **only** with `--check-docker`, only when a `docker` binary
  is present, as a timeout-bounded read-only `docker ps`. It never starts, stops,
  or mutates any container; failures degrade to a warning.

## Health/metrics URL validation

Health and metrics URLs are validated by a single shared validator,
`observability.validate_health_url`, which reuses the exact rule from
`actions.py` (reject if the parsed URL carries credentials, a query string, or a
fragment, or is not `http(s)`). The manifest parser calls this validator, so a
credentialed/queried/fragmented `observability.health.url` (or
`observability.metrics.url`) raises `ManifestError` at load time. Because the URL
is validated to be credential-free, emitting it by value carries no secret.
`metrics.auth` is referenced by **mode name only**
(`none`/`bearer_env`/`basic_env`); no secret value is ever read or printed.

## Summaries, not logs

Every output stores **summaries, never unbounded logs**: counts, statuses,
freshness, a capped (last 5) list of failed-receipt identifiers, and scalar
snapshot fields. The full status payload and the Lumen per-app summary are both
swept with `deep_redact` (with `secrets_redacted` passed through as a safe key).

## Manifest block (optional, backwards-compatible)

The new `observability` block is fully optional with a `default_factory`, so
every existing manifest that omits it still loads, and `observability` is not in
the schema's top-level `required` list. `schema_export.py` derives the
`observability` object additively from the dataclasses with enum side-table
entries for `metrics.format` (`prometheus`/`json`/`none`) and `metrics.auth`
(`none`/`bearer_env`/`basic_env`). All `examples/*.ophelia.yml` continue to
validate.

## Changed Areas

- `src/ophelia/manifest.py`: new `ObservabilityHealth`/`ObservabilityMetrics`/
  `ObservabilityLogs`/`ObservabilityConfig` dataclasses, an optional
  `observability` field on `Manifest`, and `_parse_observability` (health/metrics
  URL validation via the shared validator; `format`/`auth` enums; unknown keys
  kept in `extra`).
- `src/ophelia/observability.py`: new module. `validate_health_url`,
  `observability_status` (read-only, opt-in bounded probes),
  `observability_plan`, `observability_export`, and a `compact_observability_summary`
  reducer.
- `src/ophelia/commands/observability.py`: new `observability` group with
  `plan`/`status`/`export` subcommands and three low-risk read-only CLI
  descriptors; registered in `commands/__init__.py`.
- `src/ophelia/schema_export.py`: additive `observability` enum entries.
- `src/ophelia/lumen_adapter.py`: per-app dashboard `observability` key filled
  from a default (no-probe) `observability_status`, best-effort.
- `tests/test_observability.py`: new coverage (see Verification). No existing
  test file was modified.

## Contract Impact

Additive only. New optional manifest block, new `ophelia.observability` module,
new `ship observability ...` subcommands, and new JSON kinds
(`ophelia.observability_status`, `ophelia.observability_plan`,
`ophelia.observability_export`). No existing CLI, JSON, manifest, API, receipt,
runtime, or policy contract was changed or removed.

## Safety Impact

No VPS, SSH, or secret mutation. `status` performs no network/Docker call by
default; the opt-in probes are bounded and degrade to warnings. Health/metrics
URLs are validated to be credential-free at manifest load. Only summaries are
emitted and the payloads are redacted.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHON=python3 make validate-examples`
- `./cli/ship schema manifest --json`
- `./cli/ship observability status --app dragon-writer --environment production --json`
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage: `validate_health_url` accepts a clean `https` URL and rejects
credentialed, query-string, fragment, and non-http URLs; a credentialed (and a
query-string) `observability.health.url` raises `ManifestError` while a clean one
loads; an invalid `metrics.format` is rejected; `observability_status` with
defaults makes no network call (guarded by a monkeypatched `urlopen` that fails
the test if called) and surfaces backup freshness and a failed-receipt count from
a fixture runtime root; `probe_http=True` against `http://127.0.0.1:1/health`
(and a monkeypatched `TimeoutError`) records a warning and degrades cleanly; a
declared-but-missing metrics endpoint is a warning, not a blocker; the dashboard
per-app row carries a non-null `observability` summary; and a secret in manifest
env does not leak into status or dashboard output.

## Follow-Ups

- A future phase can add scheduled probing/alerting and richer metric scraping
  behind its own plan/apply gate; this phase intentionally keeps the first
  version lightweight and read-only.
