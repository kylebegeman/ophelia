---
id: 0098
title: Confined runtime probes and non-root support files for release 0.6.8
date: 2026-07-20
status: landed
areas: [release, manifest-v2, compose, health, security, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.8 with production-safe runtime probes that work for
loopback-only services without publishing internal ports or requiring tooling
inside application images. HTTP checks run through a digest-pinned, confined
curl helper in each target container's network namespace and compare the exact
declared response status.

Command and HTTP probes retry within their declared timeout. Manifest-owned
HTTP probes explicitly disable image-defined health checks so an unrelated
image default cannot contradict the deployment contract. Read-only support and
file-secret mounts are also materialized with container-readable modes while
their host parent trees remain owner-only.

## Safety Invariants

- HTTP checks use the target container's network namespace and loopback, never
  a host-published internal port.
- The helper image is pinned by digest, preflighted, read-only, capability-free,
  resource-bounded, and protected by no-new-privileges.
- Expected HTTP status comparison is exact and probe failures remain bounded.
- Command probes retry transient startup failures until the manifest deadline.
- Environment secret files remain mode 0600.
- Read-only mounted files are mode 0444 only beneath owner-only runtime trees so
  arbitrary non-root workload UIDs can read their declared bind targets.

## Verification

- Network-namespace helper command and exact-status regression coverage
- Pinned helper image preflight and pull coverage
- Command-probe retry coverage
- Support and secret file-mode coverage
- Production-shaped Lumen topology with data authority, both Core migrations,
  product, Core API, processor, and enrolled Runner
- Full 825-test Ophelia gate, compile, example, fixture, manifest, audit, and
  documentation checks
