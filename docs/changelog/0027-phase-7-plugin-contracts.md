---
id: 0027
title: Phase 7 plugin contracts
date: 2026-06-22
status: landed
areas: [plugins, cli, api, lumen, fixtures, docs, tests]
change_type: feature
commits: []
---

## Summary

Added metadata-only plugin contracts with trusted-directory discovery,
validation, plugin catalog surfaces, operator UI/API exposure, and fixture plugin
coverage.

## Why

Ophelia needs extension points for app templates, workflow templates, policy
packs, provider adapters, secret providers, host inventory adapters, and private operator UI
surfaces. Before runtime plugin execution exists, the platform needs a safe
contract that can validate metadata without importing or executing code.

## Changed Areas

- `src/ophelia/plugin_contracts.py`: validates plugin manifests, capability
  descriptors, command descriptors, private operator UI surfaces, compatibility metadata, and
  safety notes.
- `src/ophelia/commands/plugins.py`: adds `ship plugins list`, `ship plugins
  catalog`, and `ship plugins validate`.
- `src/ophelia/api.py` and `src/ophelia/api_routes.py`: expose read-only
  `/plugins`.
- `src/ophelia/lumen_adapter.py`: includes plugin inventory in capabilities and
  advertises the `plugins` surface.
- `fixtures/app-suite/plugins/fixture-app-suite/ophelia-plugin.yml`: describes
  the fixture suite as plugin metadata.
- `Makefile`: adds `validate-fixture-plugins`.
- `tests/test_plugin_contracts.py`: covers trusted discovery, redaction,
  unsafe mutation rejection, unbounded schema rejection, literal secret
  rejection, command-catalog isolation, CLI JSON, and operator-console visibility.
- `docs/plugin-contracts.md`, README, fixture docs, roadmap, findings, and
  changelog index updated.

## Contract Impact

Additive CLI/API contracts:

- `ship plugins list --json`
- `ship plugins catalog --plugins-dir <dir> --json`
- `ship plugins validate <manifest> --trusted-root <dir> --json`
- `GET /plugins`

Plugin command descriptors are descriptive only and are not added to the
executable command catalog.

## Safety Impact

Net positive. Plugins are disabled by default and metadata-only. Validation
rejects literal secret-shaped values, unbounded argument schemas, and mutating
command descriptors that lack plan/confirmation metadata.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_plugin_contracts`
- `PYTHON=python3 make validate-fixture-plugins`
- `PYTHONPATH=src python3 -m unittest tests.test_command_catalog tests.test_lumen_adapter`
- `python3 -m compileall -q src/ophelia/plugin_contracts.py src/ophelia/commands/plugins.py src/ophelia/api.py src/ophelia/lumen_adapter.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Add signing, installation, sandboxing, and runtime execution only after the
  metadata contract has stabilized.
- Represent more built-in templates and provider adapters through the plugin
  manifest shape over time.
