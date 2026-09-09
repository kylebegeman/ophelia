---
id: 0109
title: Portability minor completion and major-feature program
date: 2026-08-30
status: landed
areas: [portability, readiness, backup, conflicts, runbook, receipts, docs, tests]
change_type: feature
commits: []
---

## Summary

Complete the remaining gaps across the eight selected minor portability
features and promote major features 1, 2, 3, 4, 5, 6, and 8 into a detailed
later-phase build specification. Feature 6 is explicitly recorded as the
working interpretation **Host Bootstrap And Reconcile**.

## Changed Behavior

- App runbooks now expose one structured, redacted `ophelia.app_runbook` model
  and render Markdown from that same model. Routes, services, data
  dependencies, operational commands, evidence, risks, and recovery notes stay
  aligned across both formats.
- Backup status now surfaces the latest successful `backup.verify.apply`
  evidence for the app/environment. If no successful evidence exists,
  validation remains explicitly `metadata-only`.
- Conflict scanning now blocks incompatible host-global on-demand TLS ask
  endpoints and multiple catch-all owners. Secret-bearing URL components remain
  redacted.
- Receipt browsing now unifies compatibility files, correlated product
  wrappers, and kernel terminal receipts from `host-state/operations.db`.
  Standalone wrappers and correlations are integrity-checked, kernel row
  digests and authoritative columns are verified, the journal wins on mismatch,
  implicit symlink paths are refused, arbitrary backup data is excluded, and
  display output is defensively redacted.
- The local state index consumes the unified receipt projection and indexes
  kernel verification checks.
- The detailed major-feature Markdown and static HTML specifications record
  current foundations, remaining scope, contracts, state, safety, failure and
  recovery semantics, implementation slices, acceptance tests, and dependency
  order.

## Safety

The report and browser paths do not write Ophelia runtime state. `app readiness`
can execute manifest-declared app-owned verification commands inside live
Compose services, so pack authors remain responsible for making those checks
observational. State refresh writes only its rebuildable local SQLite index. No
VPS, Caddy, provider, DNS, production data, or secret value is mutated by the
new browser behavior. SQLite journal discovery uses read-only mode, refuses
symlinked path components, and does not create a missing database. Existing plan
and confirmation gates for mutating operations are unchanged.

## Verification

- Focused portability, backup, conflict, receipt, reference, and state suites.
- `make docs-check`
- `make open-source-audit-strict`
