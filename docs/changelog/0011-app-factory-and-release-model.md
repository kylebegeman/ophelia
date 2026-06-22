---
id: 0011
title: App factory and default GitHub release/deploy model
date: 2026-06-21
status: landed
areas: [cli, app-factory, scaffolding, github, release, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 10 adds an app factory: a small template registry that turns a template
name (`static-site`, `docker-web`, `web-postgres`, `web-redis`, `worker`,
`critical-data`) into a complete, ready-to-commit app scaffold (an Ophelia
manifest, agent/runbook docs, a smoke check, and the GitHub Actions workflows +
repo policy for the default release model). A new `ophelia.app_factory` module
exposes `templates_list`, `templates_explain`, `create_plan`, `create_apply`,
and a `release_metadata` helper. New `ship app create plan|apply` and
`ship app templates list|explain` subcommands extend the existing `app` group.

The default release model is `next` -> staging (auto on merge) and `master` ->
production (release-label gated), with one required PR review and required status
checks.

## Why

Spinning up a new app should be a single planned, confirmable step that produces
a valid manifest and a known-good CI/release setup, instead of hand-copying
boilerplate. Standardizing the branch-to-environment mapping and the
release-label policy makes every app deploy and roll back the same way.

## Plan writes nothing; apply writes only under the target dir

- `create_plan` is **read-only**: it computes the full scaffold in memory,
  validates the generated manifest with `load_manifest`, and returns a plan
  envelope (kind `ophelia.app_create_plan`) with `files`, `manifest_preview`
  (text + parsed dict), `github` settings, `release_label_policy`,
  `deployment_environments`, `rollback`, `confirmation_required: true`, a
  `confirmation_token` derived from the canonical apply input, and
  `exact_apply_input`. It writes nothing to disk and calls no GitHub API. A test
  snapshots the temp tree before and after and asserts no file was created.
- `create_apply` recomputes the token from the plan's `exact_apply_input` and
  rejects a missing/wrong token (no file written). It then writes the generated
  files **only** under the explicit `--target-dir`, and refuses any target that
  resolves inside the runtime root or the Ophelia repo source tree
  (`unsafe_target_dir` blocker). It writes an apply receipt
  (`ophelia/create-receipt.json`) and returns a receipt envelope listing the
  written paths.

## No GitHub mutation; secrets by name only

GitHub provisioning (repo creation, branch protection, environments) is only
*described* in the plan as settings plus a typed, `executed: false` command
list for an operator or CI to apply later. No GitHub API call and no `gh`
invocation happens anywhere in this phase. Required secrets
(`OPHELIA_DEPLOY_HOST`, `OPHELIA_DEPLOY_PORT`, `OPHELIA_DEPLOY_USER`,
`OPHELIA_DEPLOY_KEY`, `GHCR_TOKEN`) are referenced by name only. Generated
workflows reference them solely as `${{ secrets.NAME }}`; docs name them as
text. A test asserts no generated file embeds a secret value.

## Changed Areas

- `src/ophelia/app_factory.py`: new module. `TEMPLATES` registry, `templates_list`
  / `templates_explain`, `create_plan` (writes nothing), `create_apply`
  (target-dir-only, token-gated, unsafe-target refusal, receipt), and the
  `release_metadata` contract helper.
- `src/ophelia/commands/app.py`: new `create` (`plan`/`apply`) and `templates`
  (`list`/`explain`) sub-subcommands on the existing `app` group, plus four CLI
  descriptors (`create plan` read-only with a plan/apply pairing; `create apply`
  `mutates_state: true` + `requires_confirmation: true`). Every existing `app`
  subcommand is unchanged.
- `tests/test_app_factory.py`: new coverage (see Verification). No existing test
  file was modified.

## Contract Impact

Additive only. New `ophelia.app_factory` module, four `ship app ...`
subcommands, and new JSON kinds (`ophelia.app_templates`, `ophelia.app_template`,
`ophelia.app_create_plan`, `ophelia.release_metadata`). No existing CLI, JSON,
manifest, API, receipt, runtime, or policy contract was changed or removed.

## Safety Impact

`create_apply` mutates the local filesystem, but only under the explicit
`--target-dir`, only after a confirmation token derived from the canonical plan
input matches, and never inside the runtime root or the repo source tree. The
dry-run plan is `ship app create plan`; the confirmation token comes from that
plan; the receipt is written to `<target-dir>/ophelia/create-receipt.json`. No
VPS, SSH, or GitHub mutation occurs in this phase, and no secret value is read or
printed.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `./cli/ship app templates list --json`
- `./cli/ship app create plan --app demo-app --template web-postgres --owner personal --json`
- `PYTHON=python3 make validate-examples`
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage: `create_plan` writes nothing (before/after temp-tree
snapshot); `create_apply` with the correct token writes only under `target_dir`
(nothing outside it) and a wrong/empty token is rejected with no files written;
a `target_dir` inside the runtime root or the repo tree is refused; every
template's generated `.ophelia.yml` loads via `load_manifest` and validates
against `manifest_json_schema()` (when jsonschema is installed); generated
`.github/workflows/*.yml` parse as YAML; the production release path surfaces a
`release_label_required` blocker when the release-label policy is empty; and no
generated file embeds a secret value (secrets appear only as names /
`${{ secrets.NAME }}`).

## Follow-Ups

- A future phase can execute the described GitHub provisioning command list
  (repo creation, branch protection, environments) behind its own plan/apply +
  confirmation gate.
