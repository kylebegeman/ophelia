# 0039: Legacy Console Staging Partial Live Evidence

Date: 2026-06-22

Status: landed

## Summary

Adds the first partial real evidence for Legacy Console staging after the live snapshot
attempt. The runtime env now contains only non-secret structural keys, and the
runtime secret-observation store records name-only GitHub observations for the
three Legacy Runtime staging secrets verified from the upstream Legacy Console workflow.

## Changes

- Wrote `~/ophelia-runtime/apps/legacy-console-ops-staging/env` with:
  - `OPHELIA_APP`
  - `OPHELIA_CONSOLE_ASSET_PATH`
  - `OPHELIA_CONSOLE_SURFACE`
- Wrote
  `~/ophelia-runtime/github/secret-observations/legacy-console-ops-staging.staging.json`
  with name-only mappings for:
  - `LEGACY_CONSOLE_STAGING_SETUP_TOKEN` -> `OPHELIA_CONSOLE_SETUP_TOKEN`
  - `LEGACY_CONSOLE_STAGING_CREDENTIAL_ENCRYPTION_KEY` ->
    `OPHELIA_CONSOLE_CREDENTIAL_ENCRYPTION_KEY`
  - `LEGACY_CONSOLE_STAGING_MFA_ENCRYPTION_KEY` -> `OPHELIA_CONSOLE_MFA_ENCRYPTION_KEY`
- Reran hydration, probe gate, and focused live-readiness summaries.
- Recorded private operator notes with commands, results, improved counts, and
  remaining blockers. Those scratchpad notes are intentionally not part of the
  public tree.
- Updated README, roadmap, live hydration docs, product findings, live baseline
  scratchpad, and changelog index.

## Safety

- No secret values were read, printed, written, or committed.
- Database and Redis values remain missing.
- Release metadata and host capability facts were not fabricated.
- Probe gate remains `no_go`; no probes, provider mutations, workflows,
  deploys, applies, or state refreshes were run.
