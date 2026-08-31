---
id: 0100
title: Compensatable legacy route takeover for release 0.6.10
date: 2026-07-20
status: landed
areas: [release, manifest-v2, caddy, migration, recovery, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.10 with a crash-safe route transition for the first
Manifest V2 activation of an application previously managed by an Ophelia V1
Caddy fragment. Before validating the combined edge, Ophelia temporarily
displaces the exact legacy `{app}.caddy` include so it cannot compete with the
V2 `{app}-{environment}.caddy` include for the same hostname.

The original legacy include and any pre-existing V2 target bytes are stored in
digest-bound private runtime state before the live directory changes. Failed
validation, failed activation, explicit compensation, and interrupted-operation
recovery restore the predecessor byte-for-byte. Successful V2 activation keeps
the predecessor evidence available until the first-deploy compensation window
has closed.

## Safety Invariants

- Legacy takeover runs only when no V2 active revision exists for the scope.
- Only the exact Ophelia legacy include named for the manifest application is
  eligible for displacement.
- Predecessor bytes are written durably before the live legacy include moves.
- Persisted displacement state is schema-checked, size-bounded, symlink-safe,
  and digest-verified before reuse.
- Candidate validation never observes both the legacy and V2 owners.
- Any failed activation or first-deploy compensation restores the exact legacy
  include and removes the failed V2 include.

## Verification

- Test-first coverage for successful displacement and exact compensation
- Failure-path coverage proving legacy removal occurs before Caddy validation
- Interrupted-operation coverage proving durable displacement resumes safely
- Packaged Caddy rehearsal with the real Lumen staging manifest, grouped product
  routes, mTLS control route, migrated V1 include, V2 activation, and legacy
  compensation
- Full 830-test Ophelia gate, compile, example, fixture, manifest, audit, and
  documentation checks
