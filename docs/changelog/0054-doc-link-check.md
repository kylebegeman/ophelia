---
id: 0054
title: Doc link check
date: 2026-06-22
status: landed
areas: [docs, open-source, tests]
change_type: changed
commits: []
---

## Summary

Extends `make docs-check` to validate local inline Markdown links, and updates
the README clone example to use a publish-time repository placeholder.

## Why

Public-release cleanup renamed several documentation files. A local link check
keeps future doc and changelog renames from silently breaking the public
onboarding path.

## Changed Areas

- `src/ophelia/docs_check.py`: adds local Markdown link validation while keeping
  existing fence and secret-assignment checks.
- `tests/test_docs_check.py`: covers valid local/external links, missing local
  links, skipped generated/venv docs, and existing fence/secret failures.
- `README.md`: replaces the rough clone placeholder with a conventional
  `YOUR-ORG` GitHub URL placeholder.

## Contract Impact

No CLI, JSON, manifest, runtime, receipt, or API contract changes. The
`docs-check` developer gate is stricter.

## Safety Impact

No runtime, provider, or deployed host state is mutated.

## Verification

- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_docs_check -v`
- `make docs-check`

## Follow-Ups

- None.
