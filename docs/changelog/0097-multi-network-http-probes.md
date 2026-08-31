---
id: 0097
title: Multi-network HTTP readiness probes for release 0.6.7
date: 2026-07-20
status: landed
areas: [release, manifest-v2, compose, networking, health, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.7 with correct HTTP readiness and liveness checks for
containers attached to multiple Docker networks. Ophelia now parses Docker's
network map, prefers the stable application network, and can try each valid
container address without concatenating them into an invalid host.

HTTP probes now connect directly to container addresses without environment
proxy routing. They also compare the actual response code, including declared
non-2xx statuses, instead of treating every HTTP error response as a transport
failure.

## Safety Invariants

- Probe addresses must be valid IP addresses reported by Docker inspection.
- The stable per-app network is preferred deterministically.
- Additional attached networks remain usable when the preferred path is not
  reachable.
- Health checks never publish a workload port on the host or require probe
  tooling inside application images.
- Probe failures remain bounded by the manifest timeout and fail closed.

## Verification

- Multi-network container regression coverage
- Exact expected-status coverage for a non-2xx response
- direct VPS reproduction of MinIO readiness over a private Docker address
- full Ophelia test, compile, example, fixture, manifest, and documentation gates
