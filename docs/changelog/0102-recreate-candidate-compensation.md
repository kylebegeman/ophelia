---
id: 0102
title: Recreate candidate compensation for release 0.6.12
date: 2026-07-21
status: landed
areas: [release, manifest-v2, execution, recreate, compensation, health, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.12 with truthful compensation for candidates that fail
during `start_candidate`. A recreate deployment may stop its predecessor before
the candidate can be inspected. The journal previously restored a predecessor
only after `switch_traffic` began, so an earlier readiness failure removed the
candidate, left the predecessor stopped, and still emitted a
`failed_compensated` receipt claiming that predecessor as active.

Compensation now reconciles the retained predecessor whenever candidate startup
began, regardless of whether traffic switching began. Candidate cleanup happens
only after the exact predecessor has been restored. Compose restoration also
checks the predecessor's retained replica inventory and health probes in its own
Compose project, preventing a transient or crash-looping container from being
reported as recovered.

## Safety Invariants

- A candidate-start failure with a retained predecessor calls the backend's
  idempotent restore operation before candidate cleanup.
- A compensated receipt names the predecessor only after the backend verifies
  the exact predecessor digest.
- Compose compensation checks the retained manifest, project, replica counts,
  and liveness/readiness contract of the predecessor revision.
- A predecessor that is missing, unhealthy, or crash-looping produces
  `failed_uncompensated`, never a false successful rollback.
- First-deploy failures still remove the failed candidate without inventing a
  predecessor.
- Replayed compensation remains idempotent and journal-fenced.

## Verification

- Regression coverage for a recreate candidate that stops its predecessor and
  then fails candidate verification
- Existing external-verification, cancellation, cleanup-recovery, and
  first-deploy compensation coverage
- Compose candidate and active probe coverage across retained projects
- Full 834-test Ophelia gate, compile, example, fixture, manifest, audit, and
  documentation checks
