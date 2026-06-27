---
id: 0048
title: Public README fixture-first cleanup
date: 2026-06-22
status: landed
areas: [open-source, docs, tests]
change_type: cleanup
commits: []
---

## Summary

Reworked the README's public setup path around fixture apps and generic hosts,
and removed private host/path examples from the public README.

## Why

The open-source audit still reported README blockers after public examples and
tests were sanitized. README is the first public surface, so it should describe
Ophelia's source-of-truth contract and fixture-first workflows without exposing
private deployment details.

## Changed Areas

- `README.md`: replaces private checkout paths, host examples, long product
  command sequences, and private legacy deployment instructions with generic
  fixture-first examples.
- `tests/test_caddy_manager.py`: keeps old private static mount sentinels
  runtime-composed so the test source itself stays public-clean.

## Contract Impact

None. Documentation-only README cleanup plus a test-source literal cleanup.

## Safety Impact

No runtime behavior changes.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_caddy_manager tests.test_open_source_readiness tests.test_command_catalog -v`
- `./cli/ship open-source audit --allow-blocked --max-findings 2000 --json`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Private operational docs, active manifests/config, scratchpad notes, the
  deploy workflow, and the license decision still block public release.
