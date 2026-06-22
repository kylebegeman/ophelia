# Ophelia Improvement Execution Plan

Status: completed baseline roadmap

Date: 2026-06-21

Audience: implementation agents building the next Ophelia product improvement
batch after the Ophelia 2.0 portability foundation.

Note: this roadmap describes the completed Ophelia improvement foundation. New
strategic product work should start from
[Product Improvement Findings](product-improvement-findings.md) and
[Strategic Implementation Roadmap](ophelia-strategic-implementation-roadmap.md).

This plan turns the selected brainstorm items into a dependency-ordered
implementation roadmap. It is intentionally detailed so an agent can work
mechanically through the phases, add tests, update contracts, and leave receipts
without guessing product direction.

## Scope

The selected minor features are:

1. Agent-Readable Command Catalog
2. Readiness Remediation Hints
3. Manifest JSON Schema Export
4. Receipt Timeline
5. Provider Config Validator
6. Secrets Reference Audit
7. Pack Quality Score Details
8. Install Health Check
9. Command Output Consistency Pass
10. Dry-Run Diff Attachments

The selected major features are:

1. Built-In Observability And Telemetry Layer
2. Default GitHub Release And Deploy Model
3. Agent-Native Operation Graph
5. App Factory And Repo Scaffolder
6. Policy Engine
7. Runtime State Database
8. Production Traffic Controller
9. Backup And Restore Verification Platform
10. Lumen Ops Adapter

Major feature 4, Host Capability And Inventory Registry, was not selected in
this batch. Do not build a broad placement registry unless a selected feature
needs a narrow host capability read model.

## Non-Negotiable Constraints

- Do not rename Ophelia.
- Do not mutate a production VPS while implementing or testing this plan.
- Do not SSH into Spaceship, Hostinger, OVH, or any other host for mutation.
- Do not delete containers, images, volumes, backups, apps, DNS records, env
  files, runtime roots, or generated bundles.
- Do not print raw env values, database URLs, tokens, private keys, provider
  credentials, or backup credentials.
- Keep all risky commands dry-run-first.
- Every command that may mutate state must have a read-only `plan` form and a
  separate confirmed `apply`, `create`, or `execute` form.
- Every command must support stable `--json` output before it is considered
  usable by Lumen or other agents.
- Prefer typed arguments over shell snippets.
- Hook execution must be allowlisted, bounded, and recorded as artifacts.
- Keep existing manifests backwards compatible.
- Add or update a change record for every logical implementation commit. See
  [Ophelia Change Records](changelog/README.md).

## Existing References

Read these before implementation:

- [README](../README.md)
- [Ophelia Next Architecture](ophelia-next-architecture.md)
- [Portable App Pack Spec](portable-app-pack-spec.md)
- [Selected Portability Feature Roadmap](selected-portability-feature-roadmap.md)
- [Preflight and Safety](preflight-and-safety.md)
- [Releases and Rollback](releases-and-rollback.md)
- [Job / Action API Notes](job-action-api.md)
- [Lumen VPS Ophelia 2 Handoff](lumen-vps-ophelia-2-handoff.md)
- Lumen change-log precedent:
  `/Users/kyle/Developer/products/lumen/docs/changelog/README.md`
- Lumen change-log template precedent:
  `/Users/kyle/Developer/products/lumen/docs/changelog/TEMPLATE.md`

Primary source modules to inspect:

- `src/ophelia/main.py`
- `src/ophelia/commands/`
- `src/ophelia/operation_schema.py`
- `src/ophelia/actions.py`
- `src/ophelia/api.py`
- `src/ophelia/operations.py`
- `src/ophelia/portability.py`
- `src/ophelia/manifest.py`
- `src/ophelia/conflicts.py`
- `src/ophelia/backup.py`
- `src/ophelia/templates.py`

## System Shape

The desired product shape is:

```text
manifest and app pack
  -> static validation
  -> command catalog and schema export
  -> env, secrets, provider, route, backup, and release audits
  -> readiness score with remediation
  -> operation graph plan
  -> policy evaluation
  -> dry-run diff artifacts
  -> confirmation token
  -> apply/create with receipt
  -> receipt timeline
  -> Lumen Ops adapter surfaces the same contracts
```

Lumen should not depend on Kyle's local checkout or hidden conventions. It
should call Ophelia through installed CLI commands, `ship api serve`, future MCP
adapters, or stable JSON files. Ophelia owns the runtime contracts and plan
execution. Lumen owns operator UX, approval collection, dashboards, and
long-lived cockpit workflows.

## Shared Contract Rules

All new plan payloads should use the same envelope shape already documented in
`docs/selected-portability-feature-roadmap.md`:

```json
{
  "schema_version": 1,
  "kind": "ophelia.plan",
  "operation": "example.plan",
  "operation_id": "example.plan.app.production.20260621T120000Z",
  "app": "example",
  "environment": "production",
  "risk": "low",
  "dry_run": true,
  "summary": "Plan summary.",
  "blockers": [],
  "warnings": [],
  "checks": [],
  "changes": [],
  "artifacts": [],
  "confirmation_required": false,
  "confirmation_token": null,
  "exact_apply_input": null
}
```

All new receipts should use:

```json
{
  "schema_version": 1,
  "kind": "ophelia.receipt",
  "operation": "example.apply",
  "operation_id": "example.apply.app.production.20260621T120500Z",
  "plan_operation_id": "example.plan.app.production.20260621T120000Z",
  "status": "succeeded",
  "app": "example",
  "environment": "production",
  "started_at": "2026-06-21T12:05:00Z",
  "completed_at": "2026-06-21T12:05:05Z",
  "inputs_redacted": true,
  "artifacts": [],
  "checks": [],
  "rollback": {
    "available": false,
    "note": "No source runtime state was changed."
  }
}
```

Every command-specific payload may add a `details` object, but the top-level
keys must remain stable.

## Phase Order

Implement in this order:

1. Change records and contract inventory
2. Output consistency and command catalog baseline
3. Schema export and install health check
4. Provider, secret, and route validation refinements
5. Readiness remediation and score details
6. Receipt timeline and dry-run diff artifacts
7. Runtime state database
8. Policy engine
9. Agent-native operation graph
10. Lumen Ops adapter
11. App factory and GitHub release/deploy model
12. Observability and telemetry layer
13. Backup and restore verification platform
14. Production traffic controller hardening

The order is dependency-driven. Later phases should reuse earlier contract,
state, policy, and receipt primitives instead of defining parallel models.

## Phase 0: Change Records And Contract Inventory

Selected items covered:

- New project-wide change-record practice
- Foundation for all selected features

### Goal

Create a lightweight per-change record system for Ophelia and make it required
for future work. The record is not a replacement for Git history. It is an
agent-readable trail of intent, touched areas, verification, and safety notes.

### Implementation

Files:

- `docs/changelog/README.md`
- `docs/changelog/TEMPLATE.md`
- `docs/changelog/INDEX.md`
- `docs/changelog/NNNN-<slug>.md`

Rules:

- One file per logical change.
- Use monotonically increasing four-digit IDs.
- Name files as `NNNN-short-slug.md`.
- Keep entries small. A simple bug fix can be 20 lines.
- Every entry must include a summary, changed areas, safety notes, verification,
  and any follow-up that is intentionally deferred.
- Update `docs/changelog/INDEX.md` whenever a record is added.
- If several commits are part of one logical feature, one record may list
  multiple commit hashes after the commits exist.
- If a later change supersedes an earlier one, update the earlier record status
  instead of deleting history.

### Verification

- `PYTHON=python3 make docs-check`
- `git diff --check`

### Change Record

Create the first Ophelia change record for this roadmap documentation:

- `docs/changelog/0001-ophelia-improvement-roadmap.md`

## Phase 1: Output Consistency And Command Catalog Baseline

Selected items covered:

- Minor 1: Agent-Readable Command Catalog
- Minor 9: Command Output Consistency Pass
- Foundation for Major 3 and Major 10

### Goal

Make every existing command discoverable and predictable for agents. Before
adding more features, normalize the JSON envelopes and expose a machine-readable
catalog of commands, arguments, risks, output kinds, plan/apply relationships,
and required confirmations.

### Implementation

Primary files:

- `src/ophelia/operation_schema.py`
- `src/ophelia/actions.py`
- `src/ophelia/operations.py`
- `src/ophelia/commands/actions.py`
- `src/ophelia/commands/operations.py`
- `src/ophelia/api.py`

Add or refine a central model:

```python
@dataclass(frozen=True)
class CommandDescriptor:
    command: str
    operation: str
    summary: str
    risk: str
    mutates_state: bool
    requires_confirmation: bool
    plan_command: str | None
    apply_command: str | None
    json_kind: str
    args_schema: dict[str, object]
    output_schema: dict[str, object]
    artifacts: list[str]
    safety_notes: list[str]
```

Do not hardcode descriptors separately in the CLI, API, and docs. Prefer one
registry used by:

- `ship actions --json`
- `ship operations --json`
- `GET /actions`
- `GET /operations`
- future Lumen adapter commands

Add a command:

```bash
ship commands catalog --json
```

If adding a new top-level command group is too disruptive, reuse:

```bash
ship actions --json
```

with a richer `commands` or `descriptors` list. Preserve existing output keys
for backwards compatibility.

### JSON Shape

Example catalog item:

```json
{
  "command": "ship app traffic plan",
  "operation": "app.traffic.plan",
  "summary": "Plan production traffic movement without changing providers.",
  "risk": "critical",
  "mutates_state": false,
  "requires_confirmation": false,
  "plan_command": "ship app traffic plan",
  "apply_command": "ship app traffic apply",
  "json_kind": "ophelia.plan",
  "args_schema": {
    "type": "object",
    "required": ["app", "from", "to", "target_origin"],
    "properties": {
      "app": {"type": "string"},
      "environment": {"type": "string", "default": "production"}
    }
  },
  "output_schema_ref": "ophelia.plan.v1",
  "safety_notes": [
    "Planning is read-only.",
    "Apply requires a matching confirmation token."
  ]
}
```

### Consistency Pass

Audit every command in `src/ophelia/commands/`:

- `--json` output must be valid JSON and must not include prose before or after.
- Human output should remain readable and short.
- Error JSON should use `kind: "ophelia.error"` with `blockers`, `warnings`, and
  `next_actions`.
- All operation IDs should follow a consistent timestamped style.
- All app/environment-aware commands should include `app` and `environment`.
- All paths in JSON should be strings, preferably repo-relative or runtime-root
  relative when appropriate.
- All secret-sensitive fields should be redacted or expressed as key names and
  reference names only.

### Tests

Add tests such as:

- `tests/test_command_catalog.py`
- `tests/test_json_output_consistency.py`

Test expectations:

- Every descriptor has an operation name.
- Every mutating descriptor has a plan command or a documented reason.
- Every `--json` command used by Lumen docs can be parsed by `json.loads`.
- No command descriptor exposes a raw secret argument default.
- `ship actions --json` and `GET /actions` share the same descriptor source.

### Docs

Update:

- [Job / Action API Notes](job-action-api.md)
- [Lumen VPS Ophelia 2 Handoff](lumen-vps-ophelia-2-handoff.md)

Add a change record.

## Phase 2: Manifest JSON Schema Export And Install Health Check

Selected items covered:

- Minor 3: Manifest JSON Schema Export
- Minor 8: Install Health Check
- Foundation for Major 5, Major 6, and Major 10

### Goal

Give agents and editors a canonical schema for manifests and a quick way to
verify that an installed Ophelia package is usable without requiring local repo
paths.

### Manifest Schema Export

Primary files:

- `src/ophelia/manifest.py`
- `src/ophelia/commands/validate.py`
- new `src/ophelia/schema_export.py`
- new `src/ophelia/commands/schema.py`
- `src/ophelia/commands/__init__.py`

Command:

```bash
ship schema manifest --json
ship schema manifest --output ./ophelia-manifest.schema.json
```

Implementation guidance:

- Build the schema from Ophelia's manifest dataclasses or a single schema
  constant. Do not manually duplicate fields in several modules.
- Preserve support for existing manifests.
- Include optional `pack`, `host_requirements`, `data`, `hooks`, `networking`,
  `verify`, `addons`, `routes`, `services`, and release-related fields.
- Mark only truly required legacy fields as required.
- Include enum values where Ophelia enforces enums.
- Include `additionalProperties: true` only where the current parser allows
  extension fields.
- Include schema `$id`, `title`, `description`, and `schema_version`.

Example output:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ophelia.local/schemas/manifest.v1.json",
  "title": "Ophelia Manifest",
  "type": "object",
  "required": ["version", "app", "environment", "kind"],
  "properties": {
    "pack": {
      "type": "object",
      "properties": {
        "portability": {
          "type": "string",
          "enum": ["critical", "standard", "static"]
        }
      }
    }
  }
}
```

Tests:

- Existing examples validate against the exported schema.
- A Dragon Writer critical pack example validates.
- Unknown extension fields behave the same way in parser and schema.
- The schema command is stable under `json.loads`.

### Install Health Check

Command:

```bash
ship self-test --json
ship self-test
```

Implementation guidance:

- Read package metadata with `importlib.metadata`.
- Verify CLI entrypoint, package resource templates, docs paths, Python version,
  PyYAML availability, runtime root resolution, and default config loading.
- Do not require Docker or network access for the default check.
- Include optional checks behind explicit flags:
  - `--check-docker`
  - `--check-git`
  - `--check-api`
- Return blockers for broken installs and warnings for optional missing tools.

Example JSON:

```json
{
  "schema_version": 1,
  "kind": "ophelia.self_test",
  "status": "ok",
  "package": {
    "name": "ophelia",
    "version": "0.2.4",
    "location": "/path/to/site-packages/ophelia"
  },
  "checks": [
    {"name": "entrypoint", "status": "passed"},
    {"name": "templates", "status": "passed"}
  ],
  "blockers": [],
  "warnings": []
}
```

Tests:

- `ship self-test --json` parses.
- It finds templates from an installed package, not only from the source
  checkout.
- It never prints env values.

Docs:

- Add to README quick start.
- Add to Lumen handoff docs as the first smoke command.

## Phase 3: Provider, Secret, And Route Validation Refinements

Selected items covered:

- Minor 5: Provider Config Validator
- Minor 6: Secrets Reference Audit
- Supports Major 8 and Major 10

### Goal

Make provider and secret readiness inspectable before traffic automation or
deployment. This phase should reduce late failures and prevent agents from
handling raw credentials.

### Provider Config Validator

Primary files:

- `src/ophelia/portability.py`
- new `src/ophelia/provider_config.py`
- new `src/ophelia/commands/providers.py`
- existing traffic-related command modules

Commands:

```bash
ship providers validate --config ./traffic-providers.json --json
ship providers explain --config ./traffic-providers.json --json
```

Validation rules:

- Accept provider config path as a typed arg.
- Validate file exists and is JSON.
- Validate known provider types:
  - `file`
  - `cloudflare`
  - future `caddy`
- Check required fields by provider type.
- Check mutation flags separately from credentials.
- For Cloudflare, require `api_token_env`, never a token value.
- Reject provider config that contains likely raw token keys such as `api_token`,
  `token`, `secret`, `password`, or `private_key` unless the field is explicitly
  a reference name and documented.
- Validate Caddy provider `sites_dir` is inside the configured runtime root for
  mutating reloads.
- Validate DNS TTL ranges exactly once in this module and reuse from traffic
  planning.

Example JSON:

```json
{
  "schema_version": 1,
  "kind": "ophelia.provider_config.validation",
  "status": "blocked",
  "config_path": "traffic-providers.json",
  "providers": [
    {
      "name": "cloudflare-production",
      "type": "cloudflare",
      "status": "blocked",
      "blockers": [
        {
          "code": "provider_token_must_be_env_ref",
          "path": "providers[0].api_token",
          "message": "Use api_token_env instead of storing a token value."
        }
      ],
      "warnings": []
    }
  ]
}
```

Tests:

- Valid file provider passes.
- Valid Cloudflare env-ref config passes.
- Raw token-looking fields are blocked.
- Invalid TTL is blocked.
- Missing config path is a clear error.

### Secrets Reference Audit

Primary files:

- `src/ophelia/manifest.py`
- `src/ophelia/portability.py`
- new `src/ophelia/secrets_audit.py`
- new `src/ophelia/commands/secrets.py` or extend the existing one

Command:

```bash
ship secrets audit <manifest-or-app> --environment production --json
```

Implementation guidance:

- Reuse manifest parsing and env-shape logic.
- Compare required env keys, rendered `env.example`, runtime env key names,
  secret refs, provider config env refs, and app pack hook/env declarations.
- Report only key names and ref names.
- Never read or print raw env values by default.
- If checking existence in the current process environment, report only
  `present: true|false`.
- Add `--include-process-env` only for local checks, and still do not print
  values.

Example JSON:

```json
{
  "schema_version": 1,
  "kind": "ophelia.secrets_audit",
  "app": "dragon-writer",
  "environment": "production",
  "status": "warn",
  "keys": [
    {
      "name": "DATABASE_URL",
      "required": true,
      "source": "manifest.env",
      "runtime_present": true,
      "example_present": true,
      "value_redacted": true
    }
  ],
  "blockers": [],
  "warnings": []
}
```

Tests:

- Secret-looking values in fixtures are redacted.
- Missing required keys produce blockers.
- Extra runtime keys produce warnings, not raw values.
- Audit can be called by readiness without duplicating logic.

### Route And Domain Refinement

The existing conflict scanner should be audited in this phase.

Requirements:

- Route conflicts include owners, manifest paths, runtime bundle paths, and
  route source.
- Domain conflicts are blockers when two active apps claim the same host and
  path without an explicit shared route policy.
- JSON output must include `blockers` and `warnings`.
- Lumen should be able to show conflict ownership without parsing Caddy text.

Add tests under `tests/test_conflicts.py`.

## Phase 4: Readiness Remediation And Pack Quality Details

Selected items covered:

- Minor 2: Readiness Remediation Hints
- Minor 7: Pack Quality Score Details
- Builds on provider, secret, route, backup, and release checks

### Goal

Turn readiness from a pass/fail report into an action-oriented checklist. Agents
and Lumen should be able to see what is wrong, why it matters, and which exact
command or manifest change to do next.

### Implementation

Primary files:

- `src/ophelia/portability.py`
- new `src/ophelia/remediation.py`
- `src/ophelia/commands/app.py`
- `src/ophelia/commands/pack.py`

Add a common finding shape:

```python
@dataclass(frozen=True)
class Finding:
    code: str
    severity: Literal["blocker", "warning", "info"]
    area: str
    message: str
    path: str | None = None
    remediation: Remediation | None = None
```

Add remediation shape:

```python
@dataclass(frozen=True)
class Remediation:
    summary: str
    commands: list[str]
    docs: list[str]
    manifest_patch_hint: dict[str, object] | None = None
    requires_human_approval: bool = False
```

Do not generate shell snippets for mutating behavior. Commands in remediation
must be typed Ophelia commands, usually `plan` forms.

Readiness command:

```bash
ship app readiness dragon-writer --environment production --json
```

Add:

- `remediation` on each blocker/warning where feasible
- `next_actions` sorted by priority
- `score_details`
- `source_reports` pointing to subreports consumed by the aggregator

Example:

```json
{
  "schema_version": 1,
  "kind": "ophelia.app_readiness",
  "app": "dragon-writer",
  "environment": "production",
  "status": "blocked",
  "score": 72,
  "score_details": {
    "pack_contract": {"points": 18, "max_points": 25},
    "data": {"points": 12, "max_points": 25},
    "backups": {"points": 10, "max_points": 20},
    "routes": {"points": 15, "max_points": 15},
    "release": {"points": 7, "max_points": 10},
    "restore_drill": {"points": 10, "max_points": 20}
  },
  "blockers": [
    {
      "code": "restore_drill_missing",
      "area": "restore",
      "message": "Critical data has no successful restore drill receipt.",
      "remediation": {
        "summary": "Plan a restore drill from the latest export bundle.",
        "commands": [
          "ship app restore-drill plan dragon-writer --environment production --json"
        ],
        "docs": ["docs/portable-app-pack-spec.md"]
      }
    }
  ],
  "next_actions": []
}
```

Scoring guidance:

- Use weighted categories, not magic constants scattered across functions.
- Keep weights in one constant such as `READINESS_SCORE_WEIGHTS`.
- Score should never hide blockers. A high score can still be blocked.
- Include `max_points`, `points`, and `reason` for every category.
- Preserve backwards compatibility for existing readiness output by adding
  fields rather than removing keys.

Tests:

- Critical app without restore drill is blocked and has remediation.
- Static app can score high without database checks.
- Inferred data contracts lower score but do not break legacy manifests.
- Score details sum to the top-level score.

Docs:

- Update portable app pack spec.
- Add examples for Dragon Writer.
- Add change record.

## Phase 5: Receipt Timeline And Dry-Run Diff Attachments

Selected items covered:

- Minor 4: Receipt Timeline
- Minor 10: Dry-Run Diff Attachments
- Foundation for Major 7, Major 8, and Major 10

### Goal

Receipts should be browsable as an operational history, and plans should include
machine-readable and human-readable diff artifacts whenever a command proposes a
state change.

### Receipt Timeline

Primary files:

- `src/ophelia/commands/receipts.py`
- `src/ophelia/portability.py`
- new `src/ophelia/receipt_index.py`

Commands:

```bash
ship receipts list --json
ship receipts show <receipt-id> --json
ship receipts timeline --app dragon-writer --environment production --json
ship receipts timeline --operation app.traffic.apply --json
```

Implementation guidance:

- Read receipts from existing app receipt directories first.
- Do not require a database yet. Phase 7 will index receipts into SQLite.
- Sort by timestamp descending by default.
- Include filtering by app, environment, operation, status, and date range.
- Include artifact paths and rollback availability.
- Validate receipts defensively. Malformed receipts should appear as warnings,
  not crash the timeline.

Example JSON:

```json
{
  "schema_version": 1,
  "kind": "ophelia.receipt_timeline",
  "filters": {
    "app": "dragon-writer",
    "environment": "production"
  },
  "receipts": [
    {
      "operation_id": "app.traffic.apply.dragon-writer.production.20260621T120000Z",
      "operation": "app.traffic.apply",
      "status": "succeeded",
      "app": "dragon-writer",
      "environment": "production",
      "started_at": "2026-06-21T12:00:00Z",
      "completed_at": "2026-06-21T12:00:10Z",
      "path": "receipts/app.traffic.apply.dragon-writer.production.json",
      "rollback_available": true
    }
  ],
  "warnings": []
}
```

Tests:

- Timeline sorts deterministically.
- Malformed receipts produce warnings.
- Filters work independently and together.
- Existing receipt browser commands keep working.

### Dry-Run Diff Attachments

Primary files:

- `src/ophelia/planning.py`
- `src/ophelia/operation_schema.py`
- command modules that produce plans

For plan commands that propose file, provider, route, DNS, Caddy, Compose, env
shape, policy, or release changes:

- Add `changes` entries with structured before/after summaries.
- Add artifact attachments when details are too large.
- Attach diff artifacts under the runtime root, a temp plan directory, or an
  explicit `--artifacts-dir`.
- Do not include secret values in diffs.

Artifact shape:

```json
{
  "name": "compose-diff",
  "kind": "ophelia.artifact.diff",
  "media_type": "text/x-diff",
  "path": "plans/app.deploy.plan.../compose.diff",
  "redacted": true,
  "description": "Rendered Compose diff with env values redacted."
}
```

Tests:

- Deploy plan includes a Compose diff artifact when rendered output changes.
- Traffic plan includes provider diff artifacts.
- Env diffs include key names only.
- Diff artifacts do not contain known fixture secret values.

## Phase 6: Runtime State Database

Selected item covered:

- Major 7: Runtime State Database

### Goal

Create a local SQLite read model that indexes manifests, runtime bundles,
releases, backups, receipts, routes, checks, and provider plans. The database is
not the source of truth at first. It is an agent-friendly query and cache layer
rebuilt from files.

### Design

Primary files:

- new `src/ophelia/state_db.py`
- new `src/ophelia/commands/state.py`
- `src/ophelia/api.py`
- `src/ophelia/receipt_index.py`

Default path:

```text
<runtime_root>/state/ophelia-state.sqlite3
```

Rules:

- Initial commands are read-only rebuild/index commands that scan files.
- No production runtime files should be mutated except writing the state DB
  itself under the Ophelia runtime root.
- Use migrations with a small `schema_migrations` table.
- Keep schema version in code and expose it via `ship state status --json`.
- Rebuild must be safe to rerun.
- If SQLite is unavailable for any reason, existing file-based commands must
  keep working.

Suggested tables:

- `apps`
- `environments`
- `manifests`
- `routes`
- `releases`
- `receipts`
- `artifacts`
- `checks`
- `backups`
- `restore_drills`
- `provider_configs`
- `operation_plans`
- `policy_results`

Commands:

```bash
ship state status --json
ship state rebuild --runtime-root ~/ophelia-runtime --json
ship state query receipts --app dragon-writer --json
```

`state rebuild` writes a local index. It should still be treated as an Ophelia
state mutation, but not a VPS service mutation. It does not require the same
production confirmation token as traffic or deploy. It should be clearly
documented as local index creation.

### Schema Tips

- Store canonical JSON payloads in `payload_json` columns for forward
  compatibility.
- Store common query fields as typed columns.
- Use `operation_id` and `receipt_id` uniqueness.
- Store paths relative to runtime root when possible.
- Store `redacted` flags on payloads and artifacts.
- Do not store raw env values.

### API

Expose read-only endpoints:

- `GET /state/status`
- `GET /state/apps`
- `GET /state/receipts`
- `GET /state/routes`
- `GET /state/backups`

Lumen should use these instead of crawling files once available.

### Tests

- Rebuild indexes fixture runtime root.
- Rebuild is idempotent.
- Corrupt receipt is indexed as warning or skipped with diagnostic.
- Queries match file-based receipt timeline output.
- No secret fixture values appear in the DB.

## Phase 7: Policy Engine

Selected item covered:

- Major 6: Policy Engine

### Goal

Centralize safety gates and organizational rules so deploys, exports, imports,
restore drills, traffic changes, and generated app repos all follow consistent
policy.

### Implementation

Primary files:

- new `src/ophelia/policy.py`
- new `src/ophelia/commands/policy.py`
- `src/ophelia/operation_schema.py`
- `src/ophelia/actions.py`

Policy file locations:

- repo default: `config/ophelia-policy.yml`
- runtime override: `<runtime_root>/policy/ophelia-policy.yml`
- command override: `--policy path/to/policy.yml`

Policy model:

```yaml
version: 1
defaults:
  production_requires_plan: true
  production_requires_confirmation: true
  require_json_receipts: true
rules:
  - id: production-traffic-health-check
    operation: app.traffic.apply
    environment: production
    require:
      target_health_check: true
      rollback_available: true
    severity: blocker
```

Commands:

```bash
ship policy validate --policy config/ophelia-policy.yml --json
ship policy explain --json
ship policy evaluate --operation app.traffic.plan --app dragon-writer --environment production --json
```

Implementation guidance:

- Keep the first policy engine simple and deterministic.
- Avoid embedding a broad expression language in the first pass.
- Use typed conditions and operations instead of arbitrary Python or shell.
- Fail closed for unknown required policy fields.
- Fail open only for unknown advisory fields, with warnings.
- Include policy results in all relevant plans under `checks`.

Policy result shape:

```json
{
  "schema_version": 1,
  "kind": "ophelia.policy_result",
  "operation": "app.traffic.plan",
  "app": "dragon-writer",
  "environment": "production",
  "status": "blocked",
  "rules": [
    {
      "id": "production-traffic-health-check",
      "status": "blocked",
      "severity": "blocker",
      "message": "Production traffic apply requires a target health check."
    }
  ],
  "blockers": [],
  "warnings": []
}
```

Tests:

- Valid policy parses.
- Unknown operation is rejected or warned consistently.
- Production traffic policy blocks missing health check.
- Policy results appear in traffic/deploy/export/import plans.

## Phase 8: Agent-Native Operation Graph

Selected item covered:

- Major 3: Agent-Native Operation Graph

### Goal

Represent multi-step operations as explicit graphs that agents and Lumen can
inspect, execute step by step, resume, cancel, or explain. This makes complex
workflows less dependent on prompt memory.

### Implementation

Primary files:

- new `src/ophelia/workflows.py`
- `src/ophelia/operations.py`
- `src/ophelia/actions.py`
- `src/ophelia/api.py`
- new `src/ophelia/commands/workflows.py`

Command:

```bash
ship workflow plan move-app --app dragon-writer --from spaceship --to ovh --environment production --json
ship workflow show <workflow-id> --json
```

The first graph should plan, not execute:

```text
validate manifest
  -> validate provider config
  -> audit secrets
  -> check route conflicts
  -> check backups
  -> readiness report
  -> export plan
  -> import plan
  -> restore drill plan
  -> traffic plan
```

Graph node shape:

```json
{
  "id": "readiness",
  "operation": "app.readiness",
  "command": [
    "ship",
    "app",
    "readiness",
    "dragon-writer",
    "--environment",
    "production",
    "--json"
  ],
  "depends_on": ["backup-status", "route-conflicts"],
  "mutates_state": false,
  "status": "planned",
  "blockers": [],
  "artifacts": []
}
```

Implementation guidance:

- Commands in graph nodes should be arrays of typed args, not shell strings.
- Graph planning should never run mutating operations.
- Graph execution, when added, should run one node at a time through the same
  command/action registry used by the API.
- Include resumability fields from the beginning:
  - `workflow_id`
  - `node_id`
  - `status`
  - `started_at`
  - `completed_at`
  - `receipt_id`
- Store graph plans as artifacts and optionally index in the state DB.

Tests:

- Move-app graph includes the expected nodes in dependency order.
- No mutating node is runnable without a prior plan and confirmation.
- JSON command arrays contain no shell metacharacter assumptions.
- Lumen action descriptors can link to workflow graph nodes.

## Phase 9: Lumen Ops Adapter

Selected item covered:

- Major 10: Lumen Ops Adapter

### Goal

Make Ophelia easy for Lumen to consume without importing Ophelia internals or
parsing human output. Lumen should use stable command descriptors, operation
graphs, state queries, receipts, and policy results.

### Relationship To CLI, Agents, And MCP

This should work hand in hand with all three:

- CLI remains the canonical operator and automation surface.
- Lumen calls `ship api serve`, reads JSON receipts, and can shell out to
  installed `ship` when appropriate.
- Agents use the same stable JSON contracts and command catalog.
- A future MCP server should wrap the command/action registry. It should not
  duplicate business logic.

The adapter should be a thin translation layer, not a second implementation of
Ophelia operations.

### Implementation

Primary files:

- new `src/ophelia/lumen_adapter.py`
- `src/ophelia/api.py`
- `src/ophelia/actions.py`
- `src/ophelia/operation_schema.py`

CLI commands:

```bash
ship lumen capabilities --json
ship lumen dashboard-data --json
ship lumen action-descriptors --json
```

API endpoints:

- `GET /lumen/capabilities`
- `GET /lumen/dashboard-data`
- `GET /lumen/action-descriptors`
- `GET /lumen/apps`
- `GET /lumen/apps/<app>/readiness`
- `GET /lumen/apps/<app>/timeline`

Dashboard data should include:

- apps
- environments
- readiness status
- portability score
- recent receipts
- open blockers
- route conflicts
- backup freshness
- traffic status
- observability summary after Phase 11

Do not include:

- raw env values
- provider tokens
- private keys
- database URLs
- full logs with secrets

Tests:

- Adapter output parses and uses stable keys.
- It does not import command modules in a way that creates circular imports.
- It includes action descriptors from the shared registry.
- It omits raw secret values from fixtures.

Docs:

- Update [Job / Action API Notes](job-action-api.md).
- Add Lumen consumption examples to the handoff docs.

## Phase 10: App Factory And Default GitHub Release/Deploy Model

Selected items covered:

- Major 5: App Factory And Repo Scaffolder
- Major 2: Default GitHub Release And Deploy Model

### Goal

Make new app creation boring and consistent. An app should be scaffolded with an
Ophelia pack, GitHub branch policy, release automation, GHCR publishing, and
environment manifests without bespoke CI glue.

### Product Boundary

This feature should work with Lumen, agents, and CLI/MCP:

- CLI: deterministic `ship app create plan` and `ship app create apply`.
- Lumen: form-based wizard that calls the same plan/apply API and shows
  generated artifacts.
- Agents: command catalog, schema export, and operation graph describe the steps.
- MCP: future wrapper around the same action descriptors.

Do not require Lumen to exist for the CLI flow to work.

### App Factory Commands

Primary files:

- new `src/ophelia/app_factory.py`
- new `src/ophelia/github_release.py`
- new `src/ophelia/commands/app_factory.py`
- `src/ophelia/commands/app.py`
- `src/ophelia/actions.py`

Commands:

```bash
ship app create plan --app example --template fastapi-postgres --owner personal --json
ship app create apply --plan <plan-id-or-path> --confirm <token> --json
ship app templates list --json
ship app templates explain fastapi-postgres --json
```

Plan output should include:

- files to create
- manifest preview
- GitHub repo settings to apply
- branch policy
- GitHub Actions workflows
- GHCR package naming
- required secrets by name only
- release label policy
- deployment environments
- rollback metadata
- confirmation token

Initial templates:

- static site
- Docker web service
- web service with Postgres
- web service with Redis
- worker service
- Dragon Writer-style critical data app example

### GitHub Release/Deploy Model

Policy:

- `next` is staging.
- Pushes to `next` auto-build and deploy staging.
- `master` is production.
- Production only receives changes through PR merge.
- PRs into `master` require one release label:
  - `release:patch`
  - `release:minor`
  - `release:major`
- Merge to `master` creates a semantic version tag.
- The tag builds immutable images.
- Production deploy uses the production manifest and immutable image digest.
- Rollback metadata is stored as Ophelia receipts and release JSON.

Generated files:

- `.ophelia.yml`
- `ophelia/agent.md`
- `ophelia/runbook.md`
- `ophelia/checks/smoke.sh`
- `.github/workflows/ophelia-staging.yml`
- `.github/workflows/ophelia-release.yml`
- `.github/dependabot.yml` if appropriate
- `.github/pull_request_template.md`
- `.github/labeler.yml` if label automation is used

GitHub configuration plan should include:

- branch protection for `master`
- optional branch protection for `next`
- required PR review count
- required status checks
- required release label validation
- environments:
  - `staging`
  - `production`
- required secrets by name only:
  - `OPHELIA_DEPLOY_HOST`
  - `OPHELIA_DEPLOY_PORT`
  - `OPHELIA_DEPLOY_USER`
  - `OPHELIA_DEPLOY_KEY`
  - `GHCR_TOKEN` or GitHub-provided token usage, without values

Use official GitHub APIs or `gh` commands only behind explicit plan/apply. Do
not perform GitHub mutation during `plan`.

### Release Automation Contract

Add a reusable workflow template that outputs:

```json
{
  "schema_version": 1,
  "kind": "ophelia.release_metadata",
  "app": "example",
  "environment": "production",
  "version": "1.4.0",
  "git_sha": "abc1234",
  "image": "ghcr.io/org/example",
  "image_digest": "sha256:...",
  "manifest_path": ".ophelia.production.yml",
  "release_notes": [],
  "rollback": {
    "previous_version": "1.3.2"
  }
}
```

Tests:

- Scaffold preview writes nothing.
- Apply writes only inside target repo/workdir.
- Generated YAML validates.
- Generated Ophelia manifest validates.
- Missing release label blocks production release plan.
- Branch policy descriptors are stable JSON.
- No generated docs include secret values.

Docs:

- Add an app factory guide.
- Add a release/deploy model guide.
- Add change records.

## Phase 11: Built-In Observability And Telemetry Layer

Selected item covered:

- Major 1: Built-In Observability And Telemetry Layer

### Goal

Every app running inside Ophelia should get a baseline monitoring and telemetry
contract for free, and Lumen should be able to show health, status, resource,
release, backup, and traffic data without bespoke per-app setup.

### First Version

Keep the first version lightweight:

- Health check status
- Release status
- Container status from local Docker inspection when explicitly run
- Route status
- Backup freshness
- Restore drill status
- Traffic checkpoint status
- Receipt failure counts
- Optional app-defined metrics endpoint

Do not introduce a heavy metrics stack until the data model and Lumen adapter are
stable.

### Implementation

Primary files:

- new `src/ophelia/observability.py`
- new `src/ophelia/commands/observability.py`
- `src/ophelia/lumen_adapter.py`
- `src/ophelia/state_db.py`

Manifest additions:

```yaml
observability:
  health:
    url: https://example.com/health
    expect_status: 200
  metrics:
    url: http://service:9000/metrics
    format: prometheus
    auth: none
  logs:
    containers: true
    retain_days: 14
```

Commands:

```bash
ship observability plan --app dragon-writer --environment production --json
ship observability status --app dragon-writer --environment production --json
ship observability export --app dragon-writer --environment production --json
```

Rules:

- `status` is read-only.
- Docker and HTTP probes must have timeouts.
- HTTP health URLs must reject credentials, query strings, and fragments unless
  there is a separately approved reason.
- Metrics scraping is read-only and bounded.
- Store summaries, not unbounded logs, in receipts/state DB.

Lumen dashboard should consume:

- app status
- health summary
- last deploy
- last backup
- last restore drill
- last traffic change
- current blockers
- recent failed receipts

Future extension:

- Prometheus/node exporter integration
- OpenTelemetry traces
- uptime probes
- alert routing
- cost/resource trend summaries

Tests:

- Health URL validation rejects credentialed URLs.
- Status command times out cleanly.
- Missing optional metrics endpoint is a warning.
- Lumen dashboard data includes observability summary when present.

## Phase 12: Backup And Restore Verification Platform

Selected item covered:

- Major 9: Backup And Restore Verification Platform

### Goal

Move from "backup exists" to "backup can be restored and verified." This
extends the existing restore drill foundation into scheduled, receipt-backed
verification.

### Implementation

Primary files:

- `src/ophelia/backup.py`
- `src/ophelia/portability.py`
- new `src/ophelia/restore_verification.py`
- new `src/ophelia/commands/restore.py` or extend app restore-drill commands
- `src/ophelia/state_db.py`

Commands:

```bash
ship backup verify plan dragon-writer --environment production --json
ship backup verify apply dragon-writer --environment production --confirm <token> --json
ship restore-drills list --app dragon-writer --json
ship restore-drills show <drill-id> --json
```

Plan should include:

- selected backup artifact
- age and freshness
- expected data contracts
- rehearsal target
- resources required
- verification commands
- cleanup policy as a separate future cleanup plan, not implicit deletion
- confirmation token

Apply should:

- only run in an isolated rehearsal target
- never overwrite production data
- write receipt and artifacts
- preserve rollback notes
- not delete rehearsal artifacts unless a separate approved cleanup command is
  designed later

Verification checks:

- backup file exists
- checksum matches if available
- archive listing succeeds
- metadata matches app/environment
- restore command can run in rehearsal
- app-defined verify hook passes
- receipt is written

Tests:

- Plan does not mutate files.
- Apply requires confirmation.
- Apply refuses production target paths.
- Missing checksum is warning or blocker based on data criticality.
- Critical app readiness consumes latest successful verification receipt.

## Phase 13: Production Traffic Controller Hardening

Selected item covered:

- Major 8: Production Traffic Controller

### Goal

Make traffic movement production-ready in implementation and contracts before
polish. The current traffic plan/apply foundation should become a robust,
policy-gated controller with provider validation, health checks, rollback
receipts, dry-run diffs, and Lumen visibility.

### Implementation

Primary files:

- traffic planning/apply code currently in `src/ophelia/portability.py` and
  `src/ophelia/commands/app.py`
- `src/ophelia/provider_config.py`
- `src/ophelia/policy.py`
- `src/ophelia/receipt_index.py`
- `src/ophelia/state_db.py`
- `src/ophelia/lumen_adapter.py`

Commands:

```bash
ship app traffic plan <app> --from <source> --to <target> --target-origin <origin> --environment production --json
ship app traffic apply <app> --from <source> --to <target> --target-origin <origin> --environment production --confirm <token> --json
ship app traffic rollback plan <app> --receipt <receipt-id> --environment production --json
ship app traffic rollback apply <app> --receipt <receipt-id> --environment production --confirm <token> --json
ship traffic status --app <app> --environment production --json
```

Hardening requirements:

- Provider config must validate before traffic plan can produce an apply token.
- Target health check should be policy-required for production.
- Plan token input hash must include provider config digest, target origin,
  health URL, expected status, DNS provider, Caddy provider, and mutation flags.
- Apply must reject tokens from stale or mismatched plans.
- Apply must record pre-change provider state sufficient for rollback.
- Rollback must refuse operations that require deleting previously absent DNS
  records unless a separately approved delete workflow exists.
- All provider mutations must be typed operations, not shell snippets.
- All provider outputs must redact credentials.
- Caddy live reload must remain opt-in through provider config and policy.

Traffic status should combine:

- latest traffic apply receipt
- latest rollback receipt
- current provider plan if available
- route ownership
- target health status
- active blockers

Tests:

- Plan is read-only.
- Apply requires matching token.
- Mismatched target origin invalidates token.
- Cloudflare config never stores token values.
- Rollback plan reads prior provider state from receipt.
- Rollback apply blocks unsafe delete cases.
- Traffic status works without provider credentials.

## Cross-Phase Engineering Requirements

### Test Strategy

Use focused tests as each feature lands:

- Unit tests for pure validators and schema builders.
- CLI tests for JSON parseability and error shape.
- Fixture runtime roots for receipts, releases, backups, and route conflicts.
- Golden-ish schema tests where stable contracts matter.
- Redaction tests with known fake secret values.
- API tests for Lumen endpoints.

Do not weaken existing tests to make new features pass.

Standard verification after meaningful changes:

```bash
PYTHON=python3 make docs-check
PYTHON=python3 make compile
PYTHON=python3 make test
PYTHON=python3 make validate-examples
git diff --check
```

When staged changes exist from another agent, also run:

```bash
git diff --cached --check
```

### Redaction Rules

Centralize redaction. Add or reuse a helper such as:

```python
SENSITIVE_KEY_PATTERNS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "API_KEY",
)
```

Rules:

- Redact by key name and by value shape where possible.
- Preserve useful metadata: key present, source, required, ref name.
- Never include raw values in receipts, plans, state DB, Lumen payloads, or
  artifacts.
- Add tests that fail if fixture secret strings appear in JSON output.

### Confirmation Tokens

For every mutating flow:

- Plan generates token.
- Token is derived from canonical plan input.
- Apply recomputes canonical input and validates token.
- Apply writes receipt.
- Apply consumes or invalidates token when appropriate.

Token inputs should include all behavior-changing fields.

### Agent Ergonomics

Every agent-facing command should:

- accept typed args
- support `--json`
- include `schema_version`
- include `kind`
- include `operation` where applicable
- include `blockers`
- include `warnings`
- include `next_actions` when useful
- point to artifact files instead of printing large content
- include docs references for remediation

### Lumen Ergonomics

Lumen should never need to parse human CLI text. For every Lumen surface:

- prefer HTTP API if `ship api serve` is available
- fall back to installed `ship ... --json`
- consume command descriptors rather than hardcoding all commands
- use receipt timeline and state DB for history
- use operation graphs for multi-step workflows
- show policy blockers before approval controls

## Definition Of Done For This Plan

This plan is complete when:

- All selected minor and major items above have command or API surfaces.
- Every mutating operation has a read-only plan form and confirmation token.
- Every agent-consumed command has stable `--json` output.
- Lumen can discover capabilities and actions without local source imports.
- Ophelia can scaffold a repo and GitHub release/deploy contract in preview
  mode and apply only with confirmation.
- Readiness reports include remediation and detailed scoring.
- Receipts are browseable as timelines.
- Runtime state can be rebuilt into a local query database.
- Policy evaluation gates risky operations.
- Operation graphs can plan an app move.
- Observability summaries are available to Lumen.
- Backup verification proves restore readiness through receipts.
- Traffic automation is provider-validated, policy-gated, health-checked, and
  rollback-aware.
- Each logical change has a change record.
- Verification commands pass.

## Suggested First Agent Batch

For the next implementation agent, the safest high-ROI first batch is:

1. Add and enforce the change-record template.
2. Normalize command descriptors and command catalog JSON.
3. Add manifest JSON schema export.
4. Add install health check.
5. Add provider config validator.
6. Add secrets reference audit.
7. Wire remediation hints into readiness.

This batch creates the foundation the larger systems need and does not require
production state mutation.
