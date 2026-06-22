---
id: 0019
title: Phase 1 operator readability and discovery
date: 2026-06-22
status: landed
areas: [operation-schema, planning, command-catalog, doctor, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 1 of the strategic roadmap: compact operation digests,
copyable command catalog examples, and a broader `ship doctor` diagnostic
report.

## Why

Operators and downstream agents already had rich JSON payloads, but common
workflows still required scanning large envelopes and stitching together
multiple diagnostics. Phase 1 makes existing contracts easier to inspect without
changing the dry-run, confirmation, receipt, or redaction safety model.

## Changed Areas

- `src/ophelia/operation_schema.py`: added a shared redacted `digest` builder
  and attached it to plan, report, and receipt envelopes.
- `src/ophelia/planning.py`: attaches a deploy-plan digest to the legacy deploy
  plan shape, which does not use `plan_envelope`.
- `src/ophelia/command_catalog.py`: extended command descriptors with
  `examples` and populated high-value operator examples.
- `src/ophelia/inspection.py`: expanded `doctor_report` with schema metadata,
  runtime layout, manifest registry, state DB, command catalog, docs, GitHub,
  provider config, and redaction smoke diagnostics.
- `tests/test_operation_schema.py`, `tests/test_command_catalog.py`,
  `tests/test_inspection.py`, `tests/test_json_output.py`, and
  `tests/test_planning.py`: added focused coverage for digests, examples, and
  doctor stability.
- `docs/job-action-api.md`, `docs/product-improvement-findings.md`,
  `docs/ophelia-strategic-implementation-roadmap.md`, and `README.md`: updated
  direct contract and roadmap documentation.

## Contract Impact

Additive JSON fields:

- `digest` on canonical plan, report, and receipt envelopes.
- `examples` on command catalog descriptors.
- `schema_version`, `kind`, and `status` on `ship doctor --json`.

Existing fields remain in place. JSON consumers that ignore unknown fields
remain compatible.

## Safety Impact

Net positive. Digest command strings are routed through token scrubbing and
`deep_redact`, and `ship doctor` now includes an explicit redaction smoke check.
Optional local prerequisites such as Docker, GitHub auth, provider config, and a
fresh state index warn instead of blocking fresh checkout diagnostics.

## Verification

- `python3 -m compileall -q src`
- `PYTHONPATH=src python3 -m unittest tests.test_operation_schema tests.test_command_catalog tests.test_inspection tests.test_json_output tests.test_planning`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `./cli/ship commands catalog --json | python3 -m json.tool`
- `OPHELIA_SKIP_DOCKER_STATUS=1 OPHELIA_SKIP_GH_STATUS=1 ./cli/ship doctor --json | python3 -m json.tool`
- `PYTHON=python3 make validate-examples`
- `git diff --check`

## Follow-Ups

- Begin Phase 2 of `docs/ophelia-strategic-implementation-roadmap.md`: workflow
  run preview plus shared plan/receipt reference aliases.
