---
id: 0107
title: Release 0.6.16 immutable installer source identity
date: 2026-08-08
status: landed
areas: [release, daemon, installer, packaging, reproducibility, reliability, docs, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.16 with stable source identity across daemon install plans
and applies. Python build backends may create `build/` and `*.egg-info` output
inside a source checkout during `pip install`. The installer excluded `build/`
from its source digest but did not exclude package metadata, so an otherwise
clean checkout could produce a different release identity after the first
install.

## Changed Behavior

- The installer copies trusted source into a private temporary directory below
  the release tree before invoking pip.
- Build-backend output is removed with the temporary copy and never mutates the
  trusted source checkout.
- Source identity excludes build directories, Python caches, virtual
  environments, distribution output, Git metadata, and `*.egg-info` package
  metadata.
- Real source file changes continue to change the digest and release identity.

## Verification

- Regression coverage confirms generated build and package metadata do not
  change the digest while a source file change does.
- Installer coverage confirms pip receives an isolated temporary source and the
  temporary copy is removed.
- `make test`
- `make compile`
- `make docs-check`
- `make open-source-audit-strict`
- `git diff --check`
