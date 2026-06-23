---
id: 0015
title: Codebase audit pass (safe inline fixes)
date: 2026-06-21
status: landed
areas: [redaction, provider_config, secrets_audit, app_factory, portability, state_db, conflicts, commands, observability, schema_export]
change_type: fix
commits: []
---

## Summary

A comprehensive 12-area read-only audit (76 findings across bugs, integrations,
performance, quality, UX, and cohesion) followed by inline fixes for the 45 items
that were safe and needed no product/architecture decision. 33 items requiring
decisions were deferred (see Follow-Ups).

## Why

After landing the full roadmap (phases 1-13), an active improvement pass to raise
correctness, security hygiene, and cohesion before further feature work.

## Changed Areas

- `redaction.py`: documented that `deep_redact` decides sensitivity per-key (not
  subtree-propagated) and why (propagation over-redacts metadata payloads).
- `provider_config.py`: raw-secret key detection now reuses `is_sensitive_key`
  (catches api_key/access_token/client_secret/... while allowing `*_env`);
  validate/explain route through `deep_redact`.
- `secrets_audit.py`: placeholder values count as missing required keys; provider
  discovery check is informational, not a false failure.
- `app_factory.py`: confirmation token binds the app/environment used for
  generation (from the token-bound apply input); app id validated as a DNS-safe
  label; internal-domain helper.
- `portability.py`: Cloudflare unsafe-delete shape mismatch fixed (skipped delete
  honestly recorded); `traffic_status` deep-redacts nested payload; normalized
  backup "latest" sort; dropped unused imports / f-string.
- `state_db.py`: rebuild counts from actual row counts (no collision overcount);
  fixed double-close and the `limit<=0` guard; dropped unused import.
- `conflicts.py` + `commands/inspect.py`: composed human messages (no raw dict
  reprs); extended conflict `path` fallback.
- `commands/policy.py`, `commands/app.py`, `commands/pack.py`: unified on
  `operation_schema.error_envelope`; `deploy --json` emits a JSON error envelope.
- Cleanup: removed dead functions/imports, deduped `SEVERITIES`, schema export no
  longer emits the internal `extra` capture field, observability honors
  `OPHELIA_SKIP_DOCKER_STATUS` and preserves 4xx/5xx probe status codes.

## Contract Impact

Additive/behavior-preserving. Error bodies for `ship policy`/`app`/`pack`/`deploy
--json` now carry the standard `ophelia.error` envelope keys; conflict envelope
messages are human strings (structured `conflicts`/`warnings` lists unchanged).
No manifest, receipt, or API contract was removed or renamed.

## Safety Impact

Cannot mutate VPS or runtime state. Net positive for the no-secret-leak invariant:
provider explain/validate and traffic_status now deep-redact, and provider
raw-secret detection is broader. A consolidated secret-leak sweep across all
agent surfaces is clean.

## Verification

- `PYTHON=python3 make compile`
- `PYTHON=python3 make test` (309 tests, +4 new regression tests in
  `tests/test_audit_fixes.py`)
- `PYTHON=python3 make validate-examples`
- `PYTHON=python3 make docs-check`
- `git diff --check`
- Secret-leak canary sweep across all `--json` surfaces

## Follow-Ups

Deferred (need product/architecture/UX decisions or larger scope):

1. App factory template fidelity: static-site emits Docker/GHCR workflows it
   cannot run + an undeclared secret; generated manifest hardcodes the operator
   home path and ignores `runtime_root`; `create apply` silently overwrites
   existing target files (no `--force`).
2. Restore-verification free-form manifest command strings can embed secrets that
   bypass `deep_redact`; a receipt can report `failed` with no explaining blocker.
3. Operation-graph move-app emits CLI command arrays that are not directly
   runnable for `manifest.validate`/`manifest.conflicts`; workflow plan uses a
   bespoke envelope instead of `plan_envelope`.
4. Readiness surfaces other apps' conflict warnings in a single app's report; the
   `image_digest` factor diverges between readiness and pack-validation for
   image-less apps; the production health-policy gate is satisfied by a
   configured-but-never-executed health URL.
5. CLI contract unification: 4+ distinct error-envelope shapes remain across
   command modules; several commands (backup/restore/gc/notes/rollback) print
   JSON regardless of `--json`; `operations list/run` lack `schema_version`/`kind`.
6. `command_catalog` action-derived `args_schema` lists ~40 generic properties for
   every command rather than each operation's real inputs; two CLI-only receipts
   descriptors are dead (shadowed by action-derived ones).
7. `backup.py` uses ad-hoc dict shapes, its own token/id helpers, and no redaction
   sweep, diverging from the canonical envelope discipline.
8. `state_db.query_receipts` does not actually mirror `receipt_timeline`
   content/filters despite its docstring; receipt/backup payload redaction is
   env-only (shallower than the manifest deep_redact path).
9. Schema vs parser/lock divergences: schema is stricter than the parser for
   `redirect_status` on non-redirect kinds; `to_lock_dict` does not validate
   against the published `/schema/manifest` (`tunnel_target: None`).
10. `deep_redact` subtree propagation (masking all scalars under a sensitive-named
    container) was attempted and reverted because it over-redacts metadata
    payloads (e.g. JSON-schema descriptors keyed by `confirm_token`); needs a way
    to distinguish data from metadata before adopting.
11. Policy `defaults` block is declared in `config/ophelia-policy.yml` but never
    enforced by the engine.
12. Minor cohesion: `lumen_adapter._HTTP_ENDPOINTS` is a hand-maintained mirror of
    `api.py` routes (drift risk); canonical receipt `operation_id` is
    second-granularity (sub-second double-applies could overwrite).
