---
id: 0016
title: Roadmap follow-up hardening
date: 2026-06-21
status: landed
areas: [security, redaction, cli, app-factory, workflows, readiness, backup, state, schema, policy, traffic]
change_type: fix
commits: []
---

## Summary

The deferred findings from the codebase audit pass were implemented, then the
remaining decision-grade safety items were resolved with stricter defaults.

## Why

The Ophelia improvement roadmap had landed its core phases, but the audit pass
identified several consistency, contract, redaction, and data-safety gaps that
should be fixed before treating the roadmap as complete.

## Changed Areas

- `app_factory.py`: static templates avoid Docker/GHCR workflows, generated
  manifests use the requested runtime root, and scaffold apply refuses existing
  target files unless `--force` is supplied.
- `redaction.py`: command strings and secret-shaped URLs are scrubbed; data
  payload call sites can opt into propagated redaction without over-redacting
  metadata catalogs.
- `backup.py`, `state_db.py`, `restore_verification.py`: canonical envelopes,
  deeper redaction, timeline-aligned receipt queries, and guarded restore/backup
  payload handling.
- `workflows.py`: move-app nodes emit exact runnable argv and workflow plans use
  the shared plan envelope.
- `commands/*`: remaining ad-hoc JSON/error surfaces were moved to standard
  envelopes and commands honor `--json` consistently.
- `actions.py`: completion callbacks require explicit allowlisted hosts and
  callback URLs cannot carry credentials, query strings, or fragments.
- `manifest.py`, `app_registry.py`, `schema_export.py`: health/verify URLs now
  reject credentials, query strings, and fragments, and schema export matches.
- `path_safety.py`, `backup.py`, `portability.py`, `rollback.py`, `runtime.py`:
  external symlinks are blocked for backup, export, restore-preview, rollback,
  and support-file materialization.
- `policy.py`: declared defaults are enforced as implicit policy rules.
- `api_routes.py`, `lumen_adapter.py`: Lumen HTTP endpoint metadata is derived
  from the shared route list.

## Contract Impact

Security-tightening changes are intentional:

- Manifest `verify[].url`, registry `health_urls`, and action
  `completion_callback_url` reject embedded credentials, query strings, and
  fragments.
- Completion callbacks require `callbacks_enabled: true` plus
  `callback_allowed_hosts`.
- Backup/export/restore/rollback operations block external symlinks instead of
  preserving references to files outside the source root.
- CLI JSON errors consistently carry the `ophelia.error` envelope.

## Safety Impact

Net positive. The changes reduce secret-exposure risk, prevent symlink-based
backup/export leakage, avoid clobbering scaffold targets by default, and keep
mutating operations behind plan/confirmation gates.

## Verification

- `python3 -m compileall -q src`
- `git diff --check`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (344 tests)

## Follow-Ups

- Optional future work remains outside this completed roadmap: workflow
  execution, GitHub provisioning apply, scheduled observability, restore
  rehearsal cleanup/execution, traffic live re-probe, and broader platform ops.
