---
id: 0081
title: Manifest v2 and journaled Compose kernel
date: 2026-07-19
status: landed
areas: [manifest-v2, workloads, compose, caddy, journal, cli, security, docs, tests]
change_type: feature
commits: []
---

## Summary

Implement the strict manifest v2 workload contract and route its service
execution through the existing journaled kernel. New applications can model
web, worker, cron, task, migration, internal, and static lifecycles without
silently changing valid manifest v1 behavior.

## Why

The daemon and Lumen need one exact, reusable application contract before they
can safely submit or recover host operations. The legacy Compose path treats a
whole app as one mutable project and cannot prove workload-specific overlap,
background handoff, migration, traffic, or revision behavior.

## Changed Areas

- `src/ophelia/manifest_v2.py`: strict parser, workload model, canonical digest,
  kernel revision compiler, and v1 migration candidate generator.
- `src/ophelia/manifest_v2_renderer.py`: revision-isolated Compose, Caddy,
  artifact lock, secret references, and runtime metadata.
- `src/ophelia/manifest_v2_execution.py`: read-only staging plans, exact local
  approvals, idempotent journal acceptance, and receipt projection.
- `src/ophelia/manifest_v2_sources.py`: contained source validation, static and
  env-file staging, and source-byte integrity binding.
- `src/ophelia/execution/compose_backend.py`: image and candidate preflight,
  web candidate overlap, fenced background handoff, exactly-once revision
  migrations, replica enforcement, revision-isolated static publication,
  Caddy activation and compensation, cron registry, rollback, and cleanup.
- `src/ophelia/execution/subprocesses.py`: bounded, cancellable, redacted process
  execution used by new runtime work.
- `src/ophelia/commands/manifest.py`: versioned check, migrate, plan, and apply
  commands.
- Manifest, roadmap, and README documentation plus focused tests.

## Contract Impact

Manifest version 2 is a new strict pre-1.0 contract. The kernel adds
`local_operator` authorization evidence and accepts multi-workload v2
revisions. Existing manifest v1, static, and Forge product-operation behavior
remains available and retains its prior validation rules.

## Safety Impact

Planning may write only immutable operation staging, plan evidence, and a
private plan index. It does not change active app, Caddy, Docker, or journal
state. Apply requires an exact HMAC-bound confirmation, journals acceptance
before returning, uses a monotonic runtime fence for every side effect, and
commits success only after active workload and route verification. Secret
values are materialized only into private runtime env files and never enter
plans, operation inputs, receipts, or logs.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_manifest_v2 tests.test_manifest_v2_renderer tests.test_manifest_v2_execution tests.test_compose_backend tests.test_subprocess_runner -v`
- Existing journal, operation-store, and product-operation suites.

## Follow-Ups

- Promote the kernel into `opheliad`, add its Unix-socket protocol and reboot
  reconciliation, then attach Lumen through the authenticated host-agent path.
