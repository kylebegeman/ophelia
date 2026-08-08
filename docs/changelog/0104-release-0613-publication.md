---
id: 0104
title: Release 0.6.13 and public GitHub publication
date: 2026-08-08
status: landed
areas: [release, github, packaging, docs, open-source, forge, product-contracts, tests]
change_type: release
commits: []
---

## Summary

Publish Ophelia 0.6.13 as a current public GitHub Release. This release includes
the product operations evidence and compatibility hardening recorded in change
0103 and aligns package metadata, installation examples, repository links, and
the open-source readiness posture with the canonical public repository.

## Release Contents

- Exact reviewed Forge compatibility boundaries and canonical product bundle
  paths.
- Host-keyed confirmations bound to secret-bearing configuration digests and
  active revision generations.
- Complete backup, restore, rollback, and redeploy evidence validation.
- Correlated product, approval, operation, verification, and terminal kernel
  receipts.
- Structured CLI failure envelopes for product operation execution errors.
- Canonical public repository links and GitHub-only release guidance.

## Distribution

- Repository: `https://github.com/kylebegeman/ophelia`
- Release: `v0.6.13`
- Distribution: public GitHub source release
- PyPI: not published

## Verification

- `make validate-examples`
- `make validate-manifests`
- `make validate-fixtures`
- `make validate-adoption-fixtures`
- `make validate-fixture-plugins`
- `make render-examples`
- `make render-manifests`
- `make test`
- `make compile`
- `make docs-check`
- `make open-source-audit-strict`
- `git diff --check`
