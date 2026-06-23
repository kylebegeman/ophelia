---
id: 0009
title: Agent-native operation graph
date: 2026-06-21
status: landed
areas: [cli, api, workflows, planning, foundation, docs, changelog]
change_type: feature
commits: []
---

## Summary

Phase 8 adds a plan-only, agent-native *operation graph* ("workflow"): a small
DAG of nodes, each naming a real Ophelia operation from the command catalog and
the exact typed CLI argument array that would run it. A new `ophelia.workflows`
module builds and persists these graphs, a new `ship workflow` command group
(`plan` / `show` / `list`) exposes them, and read-only `GET /workflows` and
`GET /workflows/<id>` endpoints serve them over the local API. The first
template, `move-app`, chains the ten read/plan steps an operator runs before
moving an app between hosts.

## Why

Ophelia already exposes individual operations with rich safety metadata, but an
agent moving an app between hosts had to discover and order the prerequisite
steps itself (validate manifest, audit secrets, check route conflicts and
backups, score readiness, then plan export/import/restore-drill/traffic). Phase
8 encodes that sequence as an inspectable graph an agent can read, link to
private operator UI action descriptors (every node operation appears in the command catalog),
and reason about before acting, without giving it any new way to mutate state.

## Plan-only contract

This phase builds graphs but **never executes a node**. `plan_workflow` returns
a graph and writes it to disk; every node stays `status="planned"` with
`receipt_id`, `started_at`, and `completed_at` all `None`. There is no executor.

- **Typed argument arrays, not shell strings.** Each node's `command` is a
  `List[str]` (e.g. `["ship", "app", "readiness", "<app>", "--environment",
  "<env>", "--json"]`), built from the canonical command-catalog descriptor for
  the node's operation (the human `command` split into tokens) plus typed
  positional/flag tokens. A node command never contains a shell metacharacter
  (`&&`, `||`, `|`, `;`, backtick, `$(`, `>`, `<`); `plan_workflow` refuses to
  emit one and `command_has_shell_metacharacters` guards it.
- **Mutating nodes are gated.** Every `move-app` node maps to a read/plan
  operation, so each `mutates_state` is sourced from the catalog descriptor and
  is `False`. If a future template maps a node to a mutating operation, the node
  is still emitted with `mutates_state=True`, carries a `workflow_mutating_node`
  warning, and stays `status="planned"` (not runnable here; execution would
  require a separate plan + confirmation handled by a future executor).
- **Resumability fields exist for a future executor.** `WorkflowNode` carries
  `started_at`, `completed_at`, and `receipt_id` so a later phase can record
  per-node progress without changing this JSON contract. They are unset here.

## move-app node order

`validate-manifest -> validate-provider-config -> audit-secrets ->
check-route-conflicts -> check-backups -> readiness -> export-plan ->
import-plan -> restore-drill-plan -> traffic-plan`, mapping to operations
`manifest.validate`, `providers.validate`, `secrets.audit`,
`manifest.conflicts`, `backup.status`, `app.readiness`, `app.export.plan`,
`app.import.plan`, `app.restore-drill.plan`, and `app.traffic.plan`. Each node's
`depends_on` references only the previous (earlier) node, so the graph is a
strict chain in dependency order.

## Changed Areas

- `src/ophelia/workflows.py`: new module. `WorkflowNode` frozen dataclass
  (`id`, `operation`, `command`, `depends_on`, `mutates_state`, `status`,
  `blockers`, `artifacts`, plus resumability `started_at`/`completed_at`/
  `receipt_id`); `WORKFLOW_TEMPLATES` registry with the `move-app` template;
  `plan_workflow` (kind `ophelia.workflow_plan`, writes
  `runtime_root/workflows/<workflow_id>.json` best-effort, references it under
  `artifacts`); `show_workflow` (kind `ophelia.workflow_report`; missing id ->
  `workflow_not_found` blocker, no crash); `list_workflow_templates` (kind
  `ophelia.workflow_templates`). Pure JSON output, no secret values.
- `src/ophelia/commands/workflows.py`: new `workflow` command group with `plan`,
  `show`, and `list` subcommands. `--json` emits pure JSON. Registers three
  read-only CLI descriptors (`risk: low`, `mutates_state: false`).
- `src/ophelia/commands/__init__.py`: registers the new `workflow` group.
- `src/ophelia/api.py`: read-only `GET /workflows` (`list_workflow_templates`)
  and `GET /workflows/<id>` (`show_workflow`; missing id -> HTTP 200 with a
  `workflow_not_found` blocker, never 500). Existing endpoints unchanged.
- `tests/test_workflows.py`: new coverage (see Verification). No existing test
  file was modified.

## Contract Impact

Additive only. New `ophelia.workflows` module, three `ship workflow ...`
commands, and two read-only GET endpoints. No existing CLI, JSON, manifest, API,
receipt, runtime, or policy contract was changed or removed. The pre-existing
templated-operations concept (`ophelia.operations`, `GET /operations`,
`POST /operations/run`) is untouched and unrelated.

## Safety Impact

None to live infrastructure. All `workflow` operations are read-only; no node is
executed. The only side effect is writing an inspectable graph JSON under
`runtime_root/workflows/`, and that write is best-effort (an `OSError` degrades
to a `workflow_artifact_write_failed` warning, not a crash, and the plan is
still returned). No VPS mutation, no SSH, and no raw env value is read or
printed.

## Verification

- `PYTHON=python3 make compile`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `./cli/ship workflow list --json`
- `./cli/ship workflow plan move-app --app demo-service --from source-host --to target-host
  --environment production --target-origin https://origin.example.com
  --runtime-root <tmp> --json`
- `PYTHON=python3 make validate-examples`
- `PYTHON=python3 make docs-check`
- `git diff --check`

New test coverage: `move-app` plan emits exactly the expected node ids in
dependency order and every `depends_on` references an earlier node; every node
`command` is a list of strings with no shell metacharacters; the plan is
read-only (all nodes `status="planned"`, `receipt_id`/`started_at`/
`completed_at` are `None`, and the only file written is the graph artifact under
`runtime_root/workflows/`); every node `operation` appears in the command
catalog; all `move-app` nodes are `mutates_state=False`; `show_workflow`
round-trips a planned workflow; an unknown workflow id yields a
`workflow_not_found` blocker without crashing; an unknown template name yields a
`workflow_template_not_found` blocker with no artifact written; and
`list_workflow_templates` reports `move-app` with the right node count.

## Follow-Ups

- A future executor phase can populate the resumability fields
  (`started_at`/`completed_at`/`receipt_id`) per node, gated by per-node
  plan + confirmation, without changing the graph JSON contract.
- Additional templates (e.g. `deploy-app`, `rollback-app`) can be added to
  `WORKFLOW_TEMPLATES` as new read/plan chains.
