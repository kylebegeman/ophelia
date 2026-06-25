---
id: 0043
title: Pack command metadata redaction
date: 2026-06-22
status: landed
areas: [security, redaction, portability, adoption, tests, docs]
change_type: fix
---

## Summary

Redacts secret-looking literals from pack data-contract command metadata before
those commands are surfaced through pack validation, pack explanation, export
plans, or adoption plans.

## Why

Manifest data contracts can contain free-form command strings. Those strings
are useful as metadata, but they must not leak literal passwords, tokens, or
credential URLs in JSON plans and reports.

## Changed Areas

- `src/ophelia/portability.py`: deep-redacts `data_contract_dict` output and
  scrubs export command summaries with the central command-string redactor.
- `src/ophelia/adoption.py`: redacts embedded pack validation payloads before
  emitting adoption plans.
- `tests/test_portability.py`: covers pack validation and export planning with
  secret-looking command literals.
- `tests/test_adoption.py`: covers adoption-embedded pack validation redaction.
- `docs/portable-app-pack-spec.md`, `docs/app-adoption.md`, `README.md`, and
  `docs/ophelia-strategic-implementation-roadmap.md`: document the guarantee.

## Contract Impact

JSON command metadata may now contain `<redacted>` where a previous payload
would have displayed a secret-shaped argument or credential URL. Command shape,
names, non-secret arguments, and structural metadata are preserved.

## Safety Impact

Read-only redaction change. No commands are executed, no runtime/provider state
is touched, and no data-contract behavior is changed.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_portability tests.test_adoption -v`
- `python3 -m py_compile src/ophelia/portability.py src/ophelia/adoption.py tests/test_portability.py tests/test_adoption.py`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `git diff --check`

## Follow-Ups

- Continue routing any new free-form command metadata through the central
  command-string redactor before JSON output.
