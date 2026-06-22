# 0039: Quark Staging Partial Live Evidence

Date: 2026-06-22

Status: landed

## Summary

Adds the first partial real evidence for Quark staging after the live snapshot
attempt. The runtime env now contains only non-secret structural keys, and the
runtime secret-observation store records name-only GitHub observations for the
three Prism staging secrets verified from the upstream Quark workflow.

## Changes

- Wrote `~/ophelia-runtime/apps/quark-ops-staging/env` with:
  - `OPHELIA_APP`
  - `PRISM_CONSOLE_ASSET_PATH`
  - `PRISM_CONSOLE_SURFACE`
- Wrote
  `~/ophelia-runtime/github/secret-observations/quark-ops-staging.staging.json`
  with name-only mappings for:
  - `QUARK_STAGING_SETUP_TOKEN` -> `PRISM_CONSOLE_SETUP_TOKEN`
  - `QUARK_STAGING_CREDENTIAL_ENCRYPTION_KEY` ->
    `PRISM_CREDENTIAL_ENCRYPTION_KEY`
  - `QUARK_STAGING_MFA_ENCRYPTION_KEY` -> `PRISM_MFA_ENCRYPTION_KEY`
- Reran hydration, probe gate, and focused live-readiness summaries.
- Added
  [`quark-ops-staging-live-evidence-pass-2026-06-22.md`](../scratchpad/quark-ops-staging-live-evidence-pass-2026-06-22.md)
  with commands, results, improved counts, and remaining blockers.
- Updated README, roadmap, live hydration docs, product findings, live baseline
  scratchpad, and changelog index.

## Safety

- No secret values were read, printed, written, or committed.
- Database and Redis values remain missing.
- Release metadata and host capability facts were not fabricated.
- Probe gate remains `no_go`; no probes, provider mutations, workflows,
  deploys, applies, or state refreshes were run.
