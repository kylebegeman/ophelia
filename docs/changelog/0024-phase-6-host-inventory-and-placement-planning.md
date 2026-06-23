---
id: 0024
title: Phase 6 host inventory and placement planning
date: 2026-06-22
status: landed
areas: [hosts, placement, workflows, lumen, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 6 of the strategic roadmap: read-only host inventory,
host readiness checks, app placement planning, placement-aware move workflows,
and private operator UI placement surfaces.

## Why

Ophelia could already reason about app movement, but hosts were not first-class
placement targets. Operators and agents now get a structured way to compare
host capability, capacity, backup readiness, data locality, and provider
metadata before planning migrations or cutovers.

## Changed Areas

- `src/ophelia/host_inventory.py`: added host inventory collection, host
  readiness, app placement requirement derivation, and placement scoring.
- `src/ophelia/operator_reports.py`: routes the legacy host inventory surface
  through the richer inventory wrapper while preserving historical top-level
  keys.
- `src/ophelia/commands/host.py`: added `ship host inventory` options and
  `ship host readiness`.
- `src/ophelia/commands/app.py`: added `ship app placement`.
- `src/ophelia/workflows.py`: added an `app.placement.plan` node to `move-app`
  before export planning.
- `src/ophelia/lumen_adapter.py`: exposed `host_inventory` and `placement`
  surfaces.
- `config/ophelia-hosts.yml`: added sparse default local host metadata.
- `docs/host-inventory-and-placement.md`: documented the inventory config,
  CLI surfaces, scoring, contracts, workflow integration, and safety boundary.

## Contract Impact

Additive CLI contract:

- `ship host inventory --json` emits `kind: "ophelia.host_inventory"`.
- `ship host readiness [host-id] --json` emits
  `kind: "ophelia.host_readiness"`.
- `ship app placement <app> --environment <env> --json` emits
  `kind: "ophelia.app_placement_plan"`.
- `move-app` workflow graphs now include an `app.placement.plan` node after
  readiness and before export planning.

The existing `/host/inventory` and `operator_reports.host_inventory` shape keeps
legacy top-level keys such as `docker`, `disk_usage`, `networks`, and
`shared_service_status` while also carrying the richer `hosts` list.

## Safety Impact

Net positive. Host inventory and placement are read-only by default. Placement
plans do not reserve hosts, write inventory files, mutate runtime state, call
provider APIs, move DNS, or touch backups. All emitted payloads run through the
central deep redaction sweep.

## Verification

- `python3 -m compileall -q src/ophelia/host_inventory.py src/ophelia/operator_reports.py src/ophelia/commands/host.py src/ophelia/commands/app.py src/ophelia/workflows.py src/ophelia/lumen_adapter.py`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_host_inventory`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_workflows`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest tests.test_command_catalog`
- `PYTHONPATH=src OPHELIA_SKIP_DOCKER_STATUS=1 python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `PYTHON=python3 make validate-examples`
- CLI smoke tests for `host inventory`, `host readiness`, and `app placement`.
- `git diff --check`

## Follow-Ups

- Begin Phase 7: contracted plugin system.
- Future host adapters can replace static host records with provider-backed
  observations without changing the read-only placement contract.
