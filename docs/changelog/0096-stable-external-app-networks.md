---
id: 0096
title: Stable external application networks for release 0.6.6
date: 2026-07-20
status: landed
areas: [release, manifest-v2, compose, networking, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.6 with the per-app Manifest V2 network declared as an
external Compose network. Ophelia creates and labels this stable network during
preflight, while every revision-specific Compose project consumes it without
claiming project ownership.

This resolves Compose's runtime label conflict and preserves connectivity
across blue-green revisions. It also ensures revision cleanup cannot remove the
stable application network.

## Safety Invariants

- Ophelia remains the lifecycle owner of the per-app network.
- Revision-specific Compose projects consume the network as external.
- Compose teardown cannot delete stable cross-revision connectivity.
- The network remains environment-scoped and carries Ophelia ownership labels.

## Verification

- Manifest V2 renderer external-network regression coverage
- real Docker Compose reproduction against an Ophelia-created network
- full Ophelia test, compile, example, fixture, manifest, and documentation gates
