# 0036: Live Evidence Promotion Plan

Date: 2026-06-22

Status: landed

## Summary

Adds a read-only live hydration promotion plan for reviewed evidence kits. The
plan maps evidence files to consumed runtime, provider observation, release
metadata, and host inventory targets with source hashes and target existence
checks, but it does not copy or promote files.

## Changes

- Added `ship live-hydration promotion-plan`.
- Added `ophelia.live_hydration_promotion_plan` payloads with source metadata,
  target metadata, compact evidence/probe-gate child summaries, and explicit
  future-apply boundaries.
- Added command catalog metadata and examples for downstream agents.
- Added `make live-hydration-promotion-plan-legacy-console-staging` using a temporary
  scaffold directory.
- Added tests for missing evidence, warning template kits, secret-looking value
  blocking, CLI output, and command catalog discovery.
- Updated live hydration docs, roadmap, product findings, README, and live-test
  scratchpad notes.

## Safety

- The command is always read-only.
- File contents and runtime values are never emitted.
- Secret-looking evidence values still block through evidence validation.
- No runtime env, release metadata, provider observation, host inventory, state
  DB, workflow, probe, provider, or production mutation is performed.
- Any future automated promotion path must be a separate confirmed apply
  command.
