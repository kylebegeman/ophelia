---
id: 0101
title: Local origin route verification for release 0.6.11
date: 2026-07-20
status: landed
areas: [release, manifest-v2, caddy, verification, cloudflare, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.11 with route activation checks that verify the newly
selected origin through the host's `ophelia-edge` network. The previous default
made a public HTTPS request from the host, which conflated origin activation
with public DNS, Cloudflare Access, and public certificate trust. A healthy
Lumen staging activation could therefore be compensated because all protected
public URLs correctly rejected the unauthenticated host request.

The new verifier runs the existing digest-pinned curl helper under the same
read-only, capability-free resource confinement used by HTTP workload probes.
It connects to the Caddy service alias while preserving the route hostname in
TLS SNI and HTTP routing. HTTP responses below 500 prove that Caddy selected a
live application boundary, including intentional authentication responses.
Internal-CA routes skip public certificate validation only for this local
origin liveness check. Deployment orchestration remains responsible for a
separate credentialed public-edge smoke test after Ophelia succeeds.

## Safety Invariants

- Activation verification never depends on public DNS or crosses Cloudflare.
- The original route hostname remains the SNI and HTTP host identity.
- Public TLS routes retain normal certificate validation.
- Internal TLS bypasses public trust only inside the local origin probe.
- HTTP 5xx responses, connection failures, TLS failures, timeouts, and invalid
  status output fail verification and trigger compensation.
- The probe image is digest-pinned and acquired during preflight.
- The probe container is read-only, drops all capabilities, has no new
  privileges, and has strict CPU, memory, PID, and timeout limits.

## Verification

- Test-first coverage for route-driven probe-image preflight
- Command-contract coverage for edge-network routing, SNI preservation,
  internal TLS, public TLS validation, authentication responses, and 5xx
  rejection
- Real local Docker and Caddy rehearsal using an internal certificate and a 401
  application boundary response
- Full 833-test Ophelia gate, compile, example, fixture, manifest, audit, and
  documentation checks
