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
  health checks, atomic traffic selection, predecessor drain, and compensation;
- `src/ophelia/product_recovery.py`: quiesced backups and isolated restore
  drills under the same operation fence;
- `tests/test_product_operations.py`: shared Linklet contract fixture plus the
  synthetic executable lifecycle, secret redaction, active-state reconciliation,
  and post-drain compensation coverage.

Read [Product Operations Bundle Bridge](../../architecture/product-operations-bridge.md)
for commands, invariants, receipt correlation, and current v1 boundaries.
