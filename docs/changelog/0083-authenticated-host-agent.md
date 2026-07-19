---
id: 0083
title: Authenticated outbound host agent and lifecycle
date: 2026-07-19
status: landed
areas: [agent, enrollment, mtls, decisions, replay, certificates, upgrades, daemon, journal, systemd, cli, security, docs, tests]
change_type: feature
commits: []
---

## Summary

Add the host side of Ophelia's authenticated fleet protocol: one-time
enrollment, outbound mutual TLS, signed and scoped Lumen commands, durable
sequence and result replay, host-event acknowledgements, Decision-bound deploy
apply, certificate lifecycle, and rollback-protected staged self-upgrades.

## Why

`opheliad` already owned durable single-host execution, but Lumen had no secure
way to enroll, inspect, or operate independent hosts without inbound SSH or a
public host API. Fleet management also needed exact replay, revocation posture,
and an upgrade path that would not strand a host on code that cannot start.

## Changed Areas

- `src/ophelia/daemon/agent.py`, `agent_store.py`, and `agent_transport.py`:
  outbound exchange, strict command validation, sanitized persistence, durable
  ordering, at-least-once result delivery, and mutual-TLS transport.
- `src/ophelia/daemon/decisions.py`: canonical OpenSSL signature verification
  and exact Lumen Decision binding without durable raw nonces.
- `src/ophelia/daemon/enrollment.py` and `identity.py`: confirmation-bound CSR
  enrollment, trust publication, rollback, certificate status, authenticated
  rotation, and revoked-identity posture.
- `src/ophelia/daemon/upgrades.py` and the stable systemd launcher: bounded
  source-archive staging, digest and version checks, atomic release promotion,
  startup confirmation, replay recovery, and automatic rollback when a staged
  daemon cannot start.
- `src/ophelia/execution/`: schema version 5 agent state, signed-envelope
  digests, command history, result acknowledgement, and integrity checks.
- daemon configuration, installer, CLI, capabilities, health, documentation,
  and contract tests.

## Contract Impact

The remote protocol starts at schema and protocol version 1. Each exchange has
a unique exchange ID. Commands have a global per-host sequence, short expiry,
host audience, explicit scope, idempotency key, canonical signature, and
operation payload. Events and terminal command results have independent
monotonic acknowledgements.

Normal deploy apply requires both a valid signed command and a separate
short-lived signed Lumen Decision that binds the exact locally observed plan,
actor, host, revision, policy, and artifact digests.

## Safety Impact

The agent opens no inbound network port. Host identity comes from mutual TLS;
normal remote authority comes from the root-managed Lumen public key. Invalid
authorization never enters the command journal. Authorized but malformed
payloads become sanitized terminal rejections so they cannot block later
sequence progress. The database stores digests instead of raw signatures,
approval nonces, manifest bodies, source archives, or idempotency keys.

Enrollment deletes its one-time token only after service health succeeds and
rolls back local identity publication on failure. Certificate rotation verifies
the current key, returned certificate, and pinned CA before atomic publication.
Upgrade archives reject traversal, links, special files, duplicates, and quota
violations before installation.

## Verification

- focused daemon, agent, enrollment, upgrade, migration, replay, signature, and
  operation-journal tests
- complete unit suite
- Python compilation
- documentation link validation
- strict public-surface audit

## Follow-Ups

- Implement the matching Lumen certificate authority, enrollment endpoint,
  exchange authority, command queue, Decision signer, and DeployProvider.
- Exercise revocation, rotation, failed startup rollback, and disconnect replay
  on disposable Linux hosts.
- Complete encrypted backup, clean-host restore, and continuous host/runtime
  observations.
