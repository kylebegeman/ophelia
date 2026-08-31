---
id: 0088
title: Recreate deployment readiness ordering
date: 2026-07-19
status: landed
areas: [manifest-v2, execution, recreate, readiness, rollback, tests]
change_type: fix
commits: []
---

## Summary

Start an approved `recreate` candidate before candidate inspection and
readiness verification. The backend now stops the retained active workloads,
starts all new long-running workloads, verifies them, and only then commits the
new traffic pointer.

## Why

The original recreate path deferred candidate startup to traffic activation,
but the journaled executor verifies readiness before activation. A web workload
using recreate could therefore never become ready. Blue-green behavior and
fenced worker handoff are unchanged.

## Recovery

If the new candidate fails after the predecessor is stopped, the existing
journal compensation path restores the retained predecessor runtime. Replayed
start and stop calls remain idempotent.

## Verification

- recreate candidate startup and readiness regression test
- Compose backend suite
- manifest v2 execution suite
