---
id: 0003
title: Manifest JSON schema export and install self-test
date: 2026-06-21
status: landed
areas: [cli, api, schema, diagnostics, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 2 adds two read-only surfaces. `ship schema manifest` exports a JSON
Schema (draft 2020-12) derived from the manifest dataclasses, with every parser
enum encoded. `ship self-test` runs local install diagnostics (entrypoint,
packaged runtime templates via `importlib.resources`, PyYAML, runtime-root
resolution, Python version) without touching docker, the network, or any VPS.
Both are exposed over the CLI, registered in the command catalog, and the schema
is also served at `GET /schema/manifest`.

## Why

Agents (Lumen, Legacy Console) need a machine-readable manifest contract they can
validate against before authoring or deploying a manifest, and operators need a
fast first smoke command that confirms an install is healthy on a fresh host
before running anything that could mutate state. The schema is derived from the
same dataclasses the parser uses, so it stays in lockstep with the parser
instead of drifting as a hand-maintained artifact.

## Changed Areas

- `src/ophelia/schema_export.py`: new. `manifest_json_schema()` builds a draft
  2020-12 schema from `ophelia.manifest` dataclasses using `dataclasses.fields`
  and type hints, with an explicit enum side-table keyed by dotted field path
  (kind, environment, profile, redirect_status, edge.catch_all.http_redirect_status,
  pack.portability, networking.edge/internal, edge.tls.mode, console.surface,
  verify_policy.failure_mode). `manifest_json_schema_text()` serializes it with
  `indent=2, sort_keys=True`. Top-level `required` is `["version", "app",
  "kind", "routes"]`; `additionalProperties` is `true` everywhere to match the
  parser, which silently ignores unknown keys. No secret defaults.
- `src/ophelia/self_test.py`: new. `run_self_test(*, check_docker, check_git,
  check_api, runtime_root)` returns `{schema_version, kind: "ophelia.self_test",
  status, package, checks, blockers, warnings}`. Default checks never reach the
  network or docker; optional probes are warnings, never blockers. Output never
  contains environment values or secrets.
- `src/ophelia/commands/schema.py`: new `ship schema manifest [--json]
  [--output PATH]`. `--output` writes the schema and prints a small
  `ophelia.schema_export` confirmation. Registers a CLI descriptor on import.
- `src/ophelia/commands/self_test.py`: new `ship self-test [--json]
  [--check-docker] [--check-git] [--check-api] [--runtime-root PATH]`. Exits 0
  unless `status` is `blocked`. Registers a CLI descriptor on import.
- `src/ophelia/commands/__init__.py`: registers the `schema` and `self-test`
  command groups.
- `src/ophelia/api.py`: adds `GET /schema/manifest` returning the schema dict.
- `pyproject.toml`: adds `[project.optional-dependencies] test = ["jsonschema>=4"]`.
  Runtime `dependencies` unchanged.
- `README.md`, `docs/lumen-vps-ophelia-2-handoff.md`, `docs/job-action-api.md`:
  document `ship self-test` as the first smoke command and `ship schema
  manifest --json` near the manifest/discovery surfaces.
- `tests/test_schema_export.py`, `tests/test_self_test.py`: new coverage.

## Contract Impact

Additive only. New CLI commands `ship schema manifest` and `ship self-test`, new
endpoint `GET /schema/manifest`, and two new command-catalog descriptors. New
JSON kinds `ophelia.schema_export` (output confirmation) and `ophelia.self_test`.
No existing JSON key, CLI behavior, or test was changed.

## Safety Impact

None. Both surfaces are read-only. `self-test` resolves paths but creates
nothing and never reads env values into its output; `schema manifest --output`
writes only the file named on the command line. No VPS or runtime state is
mutated.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests` (158 tests, OK)
- `./cli/ship schema manifest --json | python3 -m json.tool` (SCHEMA_OK)
- `./cli/ship self-test --json | python3 -m json.tool` (SELFTEST_OK)
- `PYTHON=python3 make validate-examples`
- jsonschema conformance tests RAN (jsonschema 4.25.1 installed): all six
  `examples/*.ophelia.yml` validate against the exported schema, including the
  demo-service critical pack.
- `git diff --check`

## Follow-Ups

- None.
