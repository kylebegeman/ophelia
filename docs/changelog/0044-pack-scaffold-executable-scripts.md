---
id: 0044
title: Pack scaffold executable scripts
date: 2026-06-22
status: landed
areas: [pack, adoption, fixtures, tests, docs]
change_type: fix
---

## Summary

Makes generated pack hook/check scripts executable and teaches adoption
planning to warn when required hook/check scripts are present but not
executable.

## Why

`ship pack init` scaffolds shell hooks and checks that are intended to be run by
operators or future automation. Writing them as non-executable files creates
friction and lets fixture/app repos drift away from the runnable contract.

## Changed Areas

- `src/ophelia/portability.py`: `pack_init_report(..., write=True)` now chmods
  generated `.sh` files under `ophelia/checks` and `ophelia/hooks` to `0755`
  and marks them as executable in `planned_files`.
- `src/ophelia/adoption.py`: adoption required artifacts now report
  `required_executable` and `executable` for hook/check scripts, and warn when
  mode is wrong.
- `fixtures/adoption/*/ophelia/**/*.sh`: fixture scripts are executable.
- `tests/test_portability.py`, `tests/test_adoption.py`, and
  `tests/test_adoption_fixtures.py`: cover generated script modes and adoption
  warnings.
- `docs/portable-app-pack-spec.md`, `docs/app-adoption.md`, `README.md`, and
  `docs/ophelia-strategic-implementation-roadmap.md`: document the behavior.

## Contract Impact

Additive metadata only:

- `pack.init` `planned_files[]` entries can include `executable`.
- `app.adoption.plan` `required_artifacts[]` entries for hook/check scripts
  include `required_executable` and `executable`.

Generated script file modes change from default text-file mode to executable
mode.

## Safety Impact

No hook/check execution is added. The change only affects scaffold file mode and
read-only adoption validation metadata.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability tests.test_adoption tests.test_adoption_fixtures -v`
- `make validate-adoption-fixtures PYTHON=python3`
- `python3 -m py_compile src/ophelia/portability.py src/ophelia/adoption.py tests/test_portability.py tests/test_adoption.py tests/test_adoption_fixtures.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Keep hook/check scripts executable in any new app-repo fixtures or scaffold
  templates.
