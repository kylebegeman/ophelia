---
id: 0095
title: Compose memory normalization and candidate validation for release 0.6.5
date: 2026-07-20
status: landed
areas: [release, manifest-v2, compose, preflight, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.5 with portable Manifest V2 memory rendering and an
executable Compose candidate gate. Manifest memory quantities now compile to
exact byte counts, preserving IEC, Docker shorthand, and decimal SI semantics
without passing runtime-specific suffixes such as `Gi` into Docker Compose.

Compose candidates are validated during preflight with environment-file and
path resolution disabled. Invalid rendered configuration now returns the stable
`compose_candidate_invalid` blocker before image pulls, network creation,
migrations, or workload startup can begin.

The same pass also makes command probes valid Compose health checks and escapes
Compose interpolation in manifest-owned strings. Container-side shell
variables such as `$DATABASE_URL` therefore remain intact instead of being
expanded from the deployment host.

## Safety Invariants

- A Manifest V2 memory quantity has one documented byte value.
- The renderer emits exact bytes accepted across supported Compose versions.
- Compose schema validation runs against the immutable reviewed candidate.
- Preflight substitutes only missing secret env-file paths in a mode-0600,
  short-lived validation projection; it never resolves or materializes values.
- Command probes compile to explicit Compose `CMD` health checks.
- Manifest-owned dollar signs remain literal through Compose interpolation.
- An invalid candidate cannot advance into runtime side effects.

## Verification

- memory quantity, command-probe, and interpolation renderer tests
- Compose backend invalid-candidate preflight coverage
- real Docker Compose validation of the generated Lumen staging candidate
- full Ophelia test, compile, example, fixture, manifest, and documentation gates
