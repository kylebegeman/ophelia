---
id: 0103
title: Product operations evidence and compatibility hardening
date: 2026-07-30
status: landed
areas: [forge, product-contracts, execution, backup, restore, rollback, receipts, security, cli, docs, tests]
change_type: fix
commits: []
---

## Summary

Harden the independent Forge product-operations bridge around exact
compatibility, confirmation, recovery provenance, rollback policy, and
decision-to-effect evidence. Ophelia now accepts reviewed descendant Forge
checkouts only when the exact locked bundle identities still match, and it
rejects copied, incomplete, corrupted, or receipt-less backups as upgrade and
restore evidence.

## Changed Areas

- `fixtures/compatibility/forge-products.json`: lock the reviewed Forge history
  boundary, exact profile set, replica counts, and provider recovery identities.
- `src/ophelia/product_bundle.py`: enforce Forge-compatible canonical paths and
  cross-document dataset kind and quiescence semantics.
- `src/ophelia/product_execution.py`: bind confirmations to secret values and
  active generation, apply active-release rollback policy, validate complete
  backup provenance, and publish explicit correlation digests.
- `src/ophelia/product_recovery.py`: validate exact manifests, dataset bytes,
  terminal and correlated receipts, restore reports, stale approvals, and
  stable evidence reads without persisting secrets.
- `src/ophelia/execution/operation_store.py`: expose verified durable approval
  evidence for receipt correlation.
- `src/ophelia/commands/product.py`: preserve structured error envelopes for
  kernel, journal, fence, and process-backend failures.
- `tests/test_product_operations.py`: cover all reviewed Forge profiles,
  replica/provider declarations, canonical contracts, forged and tampered
  recovery evidence, rollback/redeploy behavior, secret binding, receipt
  correlation, and CLI failure handling.

## Contract Impact

Product plan confirmation tokens are now host-keyed HMACs over the complete
secret-bearing configuration digest and active revision generation. Restore
confirmations also bind the exact backup manifest and dataset digests. Product
operation and recovery receipts add explicit request, plan, approval,
operation, and verification digests. The compatibility fixture now uses
`producer.reviewed_commit` and records exact replicas and recoverable provider
datasets for each profile.

## Safety Impact

Release, rollback, backup, and restore commands remain mutating plan/apply
operations. Apply still requires the current plan token and writes a terminal
kernel receipt plus a correlated product receipt. Secret values remain
host-owned and are neither emitted nor persisted. Upgrade and restore planning
now fail closed unless backup bytes and both receipt layers reconcile with the
exact active revision.

## Verification

- `.venv/bin/python -m unittest tests.test_product_operations -v`
- `make test`
- `make compile`
- `make docs-check`
- `git diff --check`

## Follow-Ups

- None.
