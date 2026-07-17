# Product Operations Bundles

Use `ship product validate` for a Forge-compatible `product.*` bundle. Use the
`release`, `rollback`, `backup`, and `restore-drill` plan/apply pairs for the
complete local execution lifecycle. Every apply requires the token from its
current plan.

The implementation is in:

- `src/ophelia/product_bundle.py`: strict independent wire-contract consumer;
- `src/ophelia/product_execution.py`: plan, approval, revision translation, and
  correlated release receipts;
- `src/ophelia/execution/process_backend.py`: isolated candidate processes,
  full replica-set health, atomic traffic selection and balancing, predecessor
  drain, and compensation;
- `src/ophelia/product_recovery.py`: quiesced backups and isolated restore
  drills under the same operation fence;
- `tests/test_product_operations.py`: all shared Forge profile fixtures plus the
  synthetic executable lifecycle, secret redaction, active-state reconciliation,
  startup migration and precondition evidence, filesystem and PostgreSQL
  recovery adapters, and post-drain compensation coverage.

Real Forge release profiles may declare startup migrations and multiple
replicas. Initial releases waive backup and expand-contract evidence because no
prior state exists. Upgrades require a verified backup of the active revision;
use `--evidence ID=PATH` for expand-contract or custom preconditions. Only the
evidence digest enters the plan.

Read [Product Operations Bundle Bridge](../../architecture/product-operations-bridge.md)
for commands, invariants, receipt correlation, and current v1 boundaries.
