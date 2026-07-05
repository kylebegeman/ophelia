# 0037: Reviewed Hydration Fixture Evidence

Date: 2026-06-22

Status: landed

## Summary

Adds a committed reviewed hydration evidence kit for the fixture Postgres app.
The kit lets validation, promotion planning, and probe-gate review flows run
against deterministic non-production evidence before any real runtime values are
collected.

## Changes

- Added `fixtures/app-suite/hydration/fixture-postgres-api/staging/` with env
  shape, observed secret names, synthetic release metadata, host capability
  facts, and operator notes.
- Added tests proving the reviewed kit validates as `ok`, promotion planning is
  warning-only, the probe gate reaches `review`, probes are not executed, and
  fixture secret values are not emitted.
- Added `make live-hydration-reviewed-fixture`.
- Updated live hydration docs, fixture README, README, product findings,
  roadmap, scratchpad notes, and changelog index.

## Safety

- The evidence kit is synthetic and committed outside fixture runtime state.
- The kit stores no runtime values.
- The Make target runs read-only validation and promotion planning only.
- No probes, provider calls, workflow execution, state refresh, runtime writes,
  or production mutations are performed.
