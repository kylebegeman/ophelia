---
id: 0091
title: Runtime hardening audit and release 0.6.1
date: 2026-07-19
status: landed
areas: [release, manifest-v2, daemon, agent, caddy, security, reliability, performance, docs, tests]
change_type: fix
commits: []
---

## Summary

Harden the 0.6 host-runtime release after its first complete Lumen integration
pass, and publish the fixes as Ophelia 0.6.1.

## Correctness and Safety

- Bind remote execution to the shorter of the durable plan expiry and the
  signed Lumen Decision expiry, so valid short-lived remote approvals can be
  accepted without outliving their authority.
- Publish the first local approval key with create-only atomic semantics so
  concurrent first plans cannot return tokens signed by a key that was lost.
- Enforce `run_as_non_root` against the image's declared user when no explicit
  runtime UID is supplied.
- Use liveness probes for active workloads while retaining readiness probes for
  candidate activation.
- Reload Caddy from a freshly adapted environment-aware configuration, prune
  credentials no longer referenced by active routes, and restore both route and
  credential state when validation or reload fails.
- Validate complete control-plane command batches before committing event or
  result acknowledgements, and remove acknowledged source bundles only after
  the control plane has durably received their terminal results. Persist the
  cleanup marker so later polling cycles do not rescan completed bundles.
- Reject malformed control-plane URL ports and control characters during
  configuration or enrollment rather than failing later in the transport.

## Reliability and Performance

- Drain subprocess output continuously while retaining only the configured
  bounded prefix, preventing large command output from consuming unbounded
  temporary storage. Fence descendants that retain output pipes so collection
  cannot hang after the command process exits.
- Stream static-tree digests in bounded chunks instead of loading each file
  into memory.
- Surface unexpected local API and inbox-cleanup failures in daemon health.

## Mutation and Compatibility

This change can affect VPS runtime state only through the already approved
manifest execution path. It does not add a new mutating command, change the
Manifest V2 schema, or weaken plan, Decision, receipt, or recovery boundaries.
The non-root assertion is now fail-closed as its name promises. Existing
manifests that set `run_as_non_root: true` must either declare `run_as_user` or
use an image with a non-root `USER`.

## Verification

- focused manifest, Compose, daemon-agent, configuration, enrollment, and
  subprocess regression suites
- complete unit suite
- Python compilation
- documentation link validation
- strict open-source audit
- real Caddy environment adaptation and stdin reload probe
