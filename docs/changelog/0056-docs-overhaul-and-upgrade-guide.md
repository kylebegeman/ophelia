---
id: 0056
title: Docs overhaul and 0.3.0 upgrade guide
date: 2026-06-23
status: landed
areas: [docs, release, llm, open-source]
change_type: docs
---

## Summary

Reworks the public documentation surface for the 0.3.0 release: adds a docs
index, replaces stale roadmap routing with a current roadmap, adds a copyable
0.3.0 upgrade prompt, and moves superseded planning and handoff material into
the archive.

## Why

The repo was release-ready from a code and audit perspective, but the docs tree
still mixed current references with historical implementation plans. That made
it harder for operators and agents to know where to start.

## Changed

- `README.md`: expanded release, install, upgrade, workflow, docs, and
  governance guidance.
- `docs/README.md`: adds a maintained documentation index.
- `docs/ROADMAP.md`: records current 0.3.0 status, next work, adoption flow,
  and explicit deferred decisions.
- `docs/llm/UPGRADE_TO_0_3_0_PROMPT.md`: adds a copyable upgrade prompt for
  existing users and agents.
- `docs/archive/`: moves superseded plans, prompts, and handoff notes out of
  the active docs surface.
- `docs/llm/START_HERE.md` and `docs/llm/manifest.json`: route agents through
  the new docs structure.
- `pyproject.toml`, README, and release docs: point package metadata and public
  repository references at `github.com/mrbagels/ophelia`.

## Safety

Documentation-only. This change does not mutate VPS or runtime state.

## Verification

- `.venv/bin/python -m pip install -e ".[test]"`
- `.venv/bin/python - <<'PY' ...` verified installed package version `0.3.0`
  and repository URL metadata for `github.com/mrbagels/ophelia`.
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_docs_check tests.test_open_source_readiness tests.test_self_test -v`
- `make validate-examples validate-manifests validate-fixtures validate-adoption-fixtures validate-fixture-plugins render-examples render-manifests test compile docs-check open-source-audit-strict`
- `git diff --check`
