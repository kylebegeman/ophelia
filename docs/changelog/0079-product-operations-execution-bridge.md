# 0079: Product operations execution bridge

- Date: 2026-07-14
- Status: landed
- Areas: product-contracts, forge, execution-kernel, process-runtime, traffic,
  backup, restore, rollback, receipts, cli, docs, tests

## Summary

Ophelia now independently validates and executes Forge-compatible `product.*`
operations bundles. The first complete process vertical supports strict bundle
and artifact validation, journaled release and rollback, isolated candidate
ports behind an atomic TCP switch, predecessor drain, quiesced contract-driven
backup, isolated restore verification, and receipts correlated to both Forge
and Ophelia digests.

## Safety properties

- Forge remains an upstream contract producer, never an imported runtime
  dependency.
- Unknown fields, duplicate JSON keys, unsafe paths, digest mismatches, platform
  mismatches, and unsupported process shapes fail closed.
- Environment values remain host-owned and are never included in plans,
  operation inputs, receipts, or logs.
- Deploy, rollback, backup, and restore operations share the same monotonic
  per-app execution fence, with lease heartbeats during long recovery copies.
- Runtime traffic evidence must reconcile with journal active revision truth;
  interrupted backup and restore commands reconcile published evidence on
  replay.
- Post-drain compensation can reconstruct and restart the exact retained
  predecessor before restoring traffic.
- Success requires observed health, exact active revision identity, and durable
  terminal evidence.
- Backup honors application-stop quiescence and restore drills boot the exact
  artifact against isolated restored data.
- One to 32 replicas must all pass health before their complete upstream set is
  selected. New connections are balanced across that set.
- Forward startup migrations, active-revision backup evidence, and explicit
  digest-bound precondition evidence replace blanket release blockers.
- Filesystem and local object snapshots restore into isolated bindings.
  PostgreSQL backup and restore use provider-native tools, a drill-specific
  database name, and applied migration verification without exposing its URL
  in command arguments or evidence.

## Verification

- The shared committed SQLite, PostgreSQL, and React Linklet operations bundles
  validate at their exact current digests without importing Forge.
- A synthetic executable product deploys v1, creates and verifies a backup,
  boots an isolated restore, upgrades to v2, and rolls back to v1 through the
  same journaled kernel.
- Fault injection verifies that a commit failure after predecessor drain
  restarts v1, restores traffic, and records a compensated terminal outcome.
- Kernel, operation journal, static integration, command catalog, and product
  lifecycle test suites pass together.
