---
id: 0099
title: Grouped same-host Caddy routes for release 0.6.9
date: 2026-07-20
status: landed
areas: [release, manifest-v2, caddy, routing, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.9 with deterministic Caddy rendering for applications that
route multiple path surfaces through one hostname. The renderer now emits one
Caddy site block per domain, orders the most-specific path handlers before the
catch-all handler, and applies shared TLS and client-auth policy once at the
site boundary.

Manifest validation rejects combinations that would otherwise render an
ambiguous or misleading edge configuration: duplicate catch-all routes,
duplicate path prefixes, or incompatible site security policies on one domain.

## Safety Invariants

- A hostname is emitted at most once in a rendered manifest v2 Caddy bundle.
- Path handlers are ordered by specificity, with the catch-all handler last.
- Same-host routes must share one TLS and client-auth policy.
- Ambiguous same-host route declarations fail before any runtime mutation.
- Existing single-route and distinct-host rendering remains unchanged.

## Verification

- Test-first regression coverage for the Lumen Core `/ophelia` route and the
  product catch-all route on one hostname
- Parser coverage for duplicate catch-all and incompatible TLS policies
- Generated Lumen-style route configuration validated with the packaged Caddy
  image locally
- Full 827-test Ophelia gate, compile, example, fixture, manifest, audit, and
  documentation checks
