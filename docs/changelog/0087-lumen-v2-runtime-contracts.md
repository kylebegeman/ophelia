---
id: 0087
title: Lumen-ready manifest v2 runtime contracts
date: 2026-07-19
status: landed
areas: [manifest-v2, lumen, secrets, caddy, mtls, daemon, releases, workloads, tests, docs]
change_type: feature
commits: []
---

## Summary

Remove the remaining compatibility gaps that prevented Lumen from running as a
native manifest v2 application. V2 now models named workload endpoints,
plan-bound release metadata, host-resolved environment and file secrets, and
certificate-authenticated machine routes with edge-derived identity headers.

## Runtime Impact

- `opheliad` and direct recovery commands resolve opaque references from
  private runtime env providers and block planning when a binding is missing.
- Multiline key and certificate values can be base64-decoded into
  revision-private, read-only file mounts.
- Web workloads may expose additional named ports without creating duplicate
  containers.
- Caddy routes can use internal TLS or verify client certificates, materialize
  their trust pool, and overwrite upstream authorization and fingerprint
  headers from trusted runtime state.
- Container metadata includes the exact release, source, image, manifest, and
  revision identities bound by the plan.
- Outbound host agents preserve system server-certificate roots while adding
  the independent Lumen client-certificate CA.

## Security Invariants

- Secret values never enter committed manifests, rendered Compose, plan
  evidence, command payloads, journals, receipts, or diagnostics.
- Provider files must be real, privately owned mode-0600 files beneath the
  selected runtime root and are parsed without shell evaluation.
- Protected Caddy headers are overwritten after certificate verification.
- Machine client authentication with enrollment uses `verify_if_given`; Lumen
  remains responsible for rejecting certificate-free enrolled exchanges.

## Verification

- strict manifest parsing and rejection cases
- deterministic Compose/Caddy rendering
- secret-provider safety and non-interpolation
- exact plan blocking and file/route secret materialization
- daemon, execution, and complete unit suites
