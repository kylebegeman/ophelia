---
id: 0082
title: Durable opheliad single-host authority
date: 2026-07-19
status: landed
areas: [daemon, unix-api, recovery, workloads, cron, journal, installer, systemd, cli, security, docs, tests]
change_type: feature
commits: []
---

## Summary

Promote the manifest v2 executor into `opheliad`, a durable single-host runtime
authority with an authenticated Unix-socket API, reboot recovery, scheduled and
one-shot workload execution, replayable host events, host controls, and a
confirmation-bound systemd installer.

## Why

The CLI executor already made individual operations recoverable, but one host
still needed an always-on owner that could finish accepted work, schedule cron
workloads, preserve fencing, and expose one versioned transport to the CLI and
future Lumen host agent.

## Changed Areas

- `src/ophelia/daemon/`: strict configuration, durable host store, service
  loops, workload runner, Unix API and client, systemd notification, process
  entrypoint, and installer.
- `src/ophelia/execution/migrations.py`: host state, globally ordered events,
  delivery cursors, idempotent plan requests, and workload-run tables.
- `src/ophelia/execution/operation_store.py`: transactional host-event
  projection, event acknowledgement, operation listing, and integrity checks.
- `src/ophelia/cron.py`: strict five-field UTC cron validation and matching.
- `src/ophelia/commands/manifest.py`: daemon-first plan and apply with an
  explicit direct recovery lane.
- `src/ophelia/commands/daemon.py`: host inspection, events, controls, tasks,
  and install workflow.
- `src/ophelia/resources/systemd/opheliad.service`: hardened service,
  readiness notification, watchdog, and restart behavior.

## Contract Impact

The local protocol starts at version 1. It derives actor identity from Unix
peer credentials, requires request IDs and mutation idempotency keys, and
returns one versioned JSON envelope. Operation and workload events share a
monotonic host cursor suitable for at-least-once replay into Lumen.

## Safety Impact

Every deployment remains journaled before asynchronous execution. Recovery
workers use unique lease identities. Long workload runs renew their lease and
cannot commit through a stale fence. Plan retries bind the actor, idempotency
key, manifest, env-file bytes, and static source bytes. The installer binds
host observations and source bytes into its confirmation and quarantines an
incomplete release before atomic replacement.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_daemon tests.test_operation_store tests.test_manifest_v2 tests.test_manifest_v2_execution tests.test_compose_backend`
- `make test`, 764 tests passing
- Python compilation

## Follow-Ups

- Add the outbound mutually authenticated host-agent protocol, enrollment,
  rotation, revocation, event replay, and staged upgrade manager.
- Complete the encrypted off-site backup and clean-host restore path before the
  first production stateful adoption.
