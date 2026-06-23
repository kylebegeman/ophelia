---
id: 0047
title: Public surface sanitization
date: 2026-06-22
status: landed
areas: [open-source, docs, examples, tests, caddy, app-factory]
change_type: cleanup
commits: []
---

## Summary

Reduced open-source readiness blockers by sanitizing public examples, specs,
tests, generated app domains, and shared Caddy static mount defaults.

## Why

The first open-source audit correctly found private domains, personal paths,
private registry owners, and scanner test sentinels in public-facing material.
Those are safe to clean without touching active private manifests, private
deployment workflows, or operational handbooks.

## Changed Areas

- `src/ophelia/app_factory.py`: generated default domains now use
  `example.com` instead of a private domain.
- `src/ophelia/caddy_manager.py`: Caddy validation uses configurable
  `OPHELIA_STATIC_ROOT`/`static_root` mounts instead of a hardcoded personal
  website directory.
- `platform/shared/compose.yml`: shared Caddy defaults now use generic
  `/opt/ophelia-runtime` paths and configurable `OPHELIA_STATIC_ROOT`.
- `platform/scripts/validate-caddy.sh`: validates with a configurable static
  root and creates that directory for local validation.
- `docs/manifest-spec.md`, `docs/portable-app-pack-spec.md`,
  `docs/architecture.md`, and `examples/*.ophelia.yml`: public examples now use
  generic domains, paths, and registry owners.
- `tests/*.py`: tests now use generic fixture domains/paths or runtime-composed
  audit sentinels.
- `tests/test_caddy_manager.py`: covers the configurable static mount.

## Contract Impact

Shared Caddy validation now accepts a configurable static root:

- environment: `OPHELIA_STATIC_ROOT`
- Python API: `validate_caddy(..., static_root=Path(...))`

Generated app scaffolds use `*.example.com` defaults until users provide real
domains.

## Safety Impact

No runtime mutation is introduced. Caddy validation already creates local
runtime support directories; it now also creates the configured static mount
directory so Docker validation has a real read-only host path to mount.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_caddy_manager tests.test_open_source_readiness tests.test_manifest tests.test_portability tests.test_app_factory tests.test_schema_export -v`
- `make validate-examples`
- `./cli/ship open-source audit --allow-blocked --max-findings 2000 --json`
- `python3 -m compileall src`
- `PYTHONPATH=src python3 -m ophelia.docs_check`

## Follow-Ups

- Active private manifests, private deploy workflows, and operational handbooks
  still require maintainer approval before removal or privatization.
