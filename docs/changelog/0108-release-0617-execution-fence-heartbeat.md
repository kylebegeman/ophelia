---
id: 0108
title: Release 0.6.17 long-operation execution fence heartbeat
date: 2026-08-27
status: landed
areas: [release, execution, leases, reliability, lumen, docs, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.17 with continuous execution-fence renewal during long
backend phases. The journaled executor previously renewed its five-minute lease
only between phases. A legitimate image pull, container start, readiness probe,
or external verification that ran longer than the lease could therefore finish
under a stale fencing token even though the same worker remained healthy.

## Changed Behavior

- The executor starts one bounded background heartbeat after acquiring the
  operation and application-scope fence.
- Renewal runs at one third of the lease lifetime, capped at 60 seconds.
- Both the operation lease and application-scope lease continue to use the same
  fencing token and are renewed atomically by the journal.
- Any heartbeat failure is surfaced before additional journal state or a
  terminal receipt is committed. Recovery retains the existing reconcile-first
  behavior.
- The heartbeat is stopped before the executor releases its fence.

## Verification

- Regression coverage runs a backend phase for three lease lifetimes and
  verifies the operation still commits successfully under the original fence.
- Existing crash recovery, compensation, cancellation, and scope-fencing tests.
- `make test`
- `make compile`
- `make docs-check`
- `make open-source-audit-strict`
- `git diff --check`
