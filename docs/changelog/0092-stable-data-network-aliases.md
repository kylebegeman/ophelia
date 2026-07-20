---
id: 0092
title: Stable data-network aliases and release 0.6.2
date: 2026-07-20
status: landed
areas: [release, manifest-v2, networking, data, compose, lumen, tests, docs]
change_type: feature
commits: []
---

## Summary

Add an explicit manifest v2 contract for stable DNS aliases on the shared data
network. This lets independently deployed stateful services, such as PostgreSQL,
retain a durable address while Ophelia continues to isolate application
revisions and Compose projects. Publish the contract as Ophelia 0.6.2 for the
first Lumen Platform staging deployment.

## Safety Invariants

- Aliases are accepted only for the declared `data` network.
- Every alias is a canonical lowercase DNS-style identifier.
- An alias can belong to only one workload in a manifest.
- Stable aliases require recreate updates, preventing old and candidate
  revisions from claiming the same address at once.
- Aliases participate in the canonical manifest lock and revision digest.

## Verification

- strict parser acceptance and rejection coverage
- deterministic Compose alias rendering
- full Ophelia test and static-analysis gates
