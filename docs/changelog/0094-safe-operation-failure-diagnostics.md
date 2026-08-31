---
id: 0094
title: Safe operation failure diagnostics for release 0.6.4
date: 2026-07-20
status: landed
areas: [release, manifest-v2, execution, diagnostics, security, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.4 with durable, secret-safe failure diagnostics for
journaled Manifest V2 operations. A compensated deployment failure now records
the active phase, bounded failure category, stable error code, process action,
exit status, and output digests before compensation begins. The apply JSON
report returns that diagnostic beside the receipt.

The diagnostic deliberately excludes raw process output and exception text.
Known runtime failures such as permissions, missing paths, exhausted storage,
port conflicts, registry authorization, invalid mounts, unhealthy workloads,
and image-signature validation receive stable codes that operators and Lumen
can act on without exposing credentials.

## Safety Invariants

- A failure diagnostic is journaled before compensation starts.
- Raw stdout, stderr, and exception text are never persisted in the diagnostic.
- Output and exception details are represented only by one-way digests.
- Process actions are reduced to bounded command families such as
  `docker.compose.up`.
- Retrying a terminal operation returns the original receipt and diagnostic.

## Verification

- journaled executor process-failure and secret-redaction tests
- Manifest V2 failed-apply report coverage
- full Ophelia test, compile, example, fixture, manifest, and documentation gates
