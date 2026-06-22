---
id: 0045
title: Manifest bootstrap for app repos
date: 2026-06-22
status: landed
areas: [pack, adoption, cli, actions, docs, tests]
change_type: feature
---

## Summary

Extends `ship pack init` with an opt-in manifest bootstrap so app repositories
can preview or write a valid `.ophelia.yml` alongside the existing `ophelia/`
support artifacts.

## Why

The adoption flow previously detected a missing manifest but could only point
operators at support-file scaffolding. Future Ophelia-first repos need a
complete bootstrap path that creates the manifest contract first, without live
values or legacy-product assumptions.

## Changed Areas

- `src/ophelia/portability.py`: adds manifest file generation to
  `pack_init_report` behind `include_manifest`.
- `src/ophelia/commands/pack.py`: adds `--include-manifest`, `--kind`,
  `--domain`, `--image`, `--port`, and `--static-root`.
- `src/ophelia/actions.py`: exposes the new preview inputs through the action
  schema.
- `src/ophelia/adoption.py`: missing-manifest adoption plans now emit service
  and static bootstrap command alternatives.
- `src/ophelia/command_catalog.py`: adds real pack init bootstrap examples.
- `tests/test_portability.py` and `tests/test_adoption.py`: cover generated
  service/static manifests, overwrite refusal, and adoption next commands.
- `README.md`, `docs/manifest-spec.md`, `docs/portable-app-pack-spec.md`,
  `docs/app-adoption.md`, and
  `docs/ophelia-strategic-implementation-roadmap.md`: document the flow.

## Contract Impact

Additive CLI options for `ship pack init`:

- `--include-manifest`
- `--kind service|static`
- `--domain`
- `--image`
- `--port`
- `--static-root`

`pack.init` JSON reports now include `include_manifest`, `manifest_kind`, and
`manifest_path`. When `--include-manifest` is used, `.ophelia.yml` and any
static placeholder files are included in `planned_files`, `artifacts`, and
`written_files` according to preview/write mode.

## Safety Impact

Still preview-first. No files are written unless `--write` is passed, and
existing target files are refused unless `--force` is passed. No provider,
runtime, GitHub, env, secret, probe, or deployment state is touched.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability tests.test_adoption tests.test_actions -v`
- `python3 -m py_compile src/ophelia/portability.py src/ophelia/commands/pack.py src/ophelia/actions.py src/ophelia/adoption.py tests/test_portability.py tests/test_adoption.py tests/test_actions.py`
- CLI smoke for service and static `pack init --include-manifest --write`
  followed by `pack validate`.
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Use this bootstrap path for future apps and retained-product adoption phases
  instead of hand-writing starter manifests.
