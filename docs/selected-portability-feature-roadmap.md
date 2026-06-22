# Selected Portability Feature Roadmap

Status: selected planning scope

This document records the Ophelia portability and ergonomics features selected
for implementation planning. Listing a feature here does not mean it must ship in
one batch. It means the feature belongs in the desired Ophelia direction and
should be designed as part of the same cohesive system.

This document supersedes the earlier broad implementation prompt in
`docs/prompts/portable-runtime-build-agent.md` for future build-agent work. The
older prompt remains useful historical context, but new implementation work
should use the selected scope here and the prompt in
`docs/prompts/selected-portability-build-agent.md`.

The selected scope includes all eight minor improvements from the product idea
exercise, plus major systems 1, 2, 3, 4, 5, and 8:

- Portable App Pack System
- Data Export And Import Pipeline
- Restore Drill System
- Cutover Orchestrator
- Per-App Isolation Refactor
- Lumen Ops Adapter Surface

Host bootstrap/reconcile remains useful future work. Initial read-only
multi-host placement planning has since landed in Phase 6 of the strategic
roadmap.

## Product Direction

Ophelia should become the agent-friendly VPS runtime substrate for portable apps.
It should make app state visible, movement safe, and operation plans explicit.
The operator experience can later live in Lumen Ops, but Ophelia must expose the
stable commands, JSON contracts, receipts, and safety gates that Lumen and LLM
agents can rely on.

Quark, Prism, and OpenClaw are not long-term foundations for this roadmap. They
may exist on current hosts as legacy deployments, but Lumen replaces them. Agents
should avoid designing new features around Quark, Prism, or OpenClaw except when
reading legacy inventory or preparing a separately approved decommission plan.

The product target:

```text
inspect an app
  -> understand readiness
  -> validate pack and env shape
  -> verify backup freshness
  -> export data and runtime state
  -> restore into rehearsal
  -> cut over with receipts
  -> preserve rollback and cleanup separately
```

## Agent-Friendly API Requirements

Many Ophelia operations will be proposed, reviewed, and run by LLM agents. Design
every command and API with that reality in mind.

### Command Design

- Every risky command has a read-only `plan` form and a separate confirmed
  `apply` or `create` form.
- Every command supports `--json` with stable keys.
- Every operation returns structured `blockers`, `warnings`, `next_actions`, and
  `artifacts`.
- Every mutating command accepts a confirmation token derived from the matching
  plan.
- Every mutating API should expose an `exact_apply_input` payload that can be
  replayed after human approval.
- Commands should have bounded output. Large logs, dumps, and reports should be
  written to artifact files and referenced by path.
- Commands should be idempotent where possible. When not possible, they should
  expose a clear `operation_id` and recovery note.
- Commands should avoid open-ended shell fragments. Use typed arguments and
  allowlisted hook paths.

### JSON Plan Envelope

Use one common envelope for agent-readable plans:

```json
{
  "schema_version": 1,
  "kind": "ophelia.plan",
  "operation": "app.export.plan",
  "operation_id": "app.export.plan.dragon-writer.production.20260620T120000Z",
  "app": "dragon-writer",
  "environment": "production",
  "source_host": "spaceship",
  "target_host": null,
  "risk": "high",
  "dry_run": true,
  "summary": "Export Dragon Writer production runtime and data.",
  "blockers": [],
  "warnings": [],
  "checks": [],
  "changes": [],
  "artifacts": [],
  "confirmation_required": true,
  "confirmation_token": "redacted-plan-token",
  "exact_apply_input": {
    "command": "ship app export create dragon-writer --environment production --confirm redacted-plan-token"
  }
}
```

### JSON Receipt Envelope

Use one common envelope for operation results:

```json
{
  "schema_version": 1,
  "kind": "ophelia.receipt",
  "operation": "app.export.create",
  "operation_id": "app.export.create.dragon-writer.production.20260620T120500Z",
  "plan_operation_id": "app.export.plan.dragon-writer.production.20260620T120000Z",
  "status": "succeeded",
  "app": "dragon-writer",
  "environment": "production",
  "started_at": "2026-06-20T12:05:00Z",
  "completed_at": "2026-06-20T12:06:30Z",
  "actor": "operator",
  "inputs_redacted": true,
  "artifacts": [],
  "checks": [],
  "rollback": {
    "available": false,
    "note": "Export did not mutate source runtime."
  }
}
```

### Redaction And Secrets

- Never print raw env values by default.
- Treat env files, secret refs, database URLs, access tokens, private keys, and
  backup provider credentials as sensitive.
- Reports should include env key names, secret ref names, and readiness status.
- If an operator-only sealed artifact is added later, it must be opt-in and
  clearly separate from normal receipts.

### Error Shape

LLM agents need predictable failure output:

```json
{
  "schema_version": 1,
  "kind": "ophelia.error",
  "operation": "pack.validate",
  "status": "failed",
  "blockers": [
    {
      "code": "critical_data_missing_export",
      "message": "Critical Postgres data requires an export contract.",
      "path": "data.postgres.export"
    }
  ],
  "warnings": [],
  "next_actions": [
    "Add data.postgres.export to the app pack."
  ]
}
```

## Minor Features And Improvements

### 1. App Move Readiness Checklist

**Summary**

Show whether an app is safe to move between hosts.

**How it works**

Add a read-only command:

```bash
./cli/ship app readiness dragon-writer --environment production --json
```

The command reads the manifest lock, runtime bundle, release metadata, data
contract, backup status, route ownership, env shape, and restore drill receipts.
It does not connect to live databases unless the implementation explicitly adds a
read-only probe with a timeout.

**User experience**

The operator or agent gets a clear result:

```text
Dragon Writer production is not ready to move.
Blockers:
- critical Postgres data has no restore drill receipt
- uploads volume has no export rule
Warnings:
- image uses a tag without digest
```

**Why it improves the product**

It turns migration uncertainty into a concrete checklist. Agents can use the
output to decide the next safe action instead of guessing.

**Complexity**

Medium

**Impact**

High

**Dependencies or future opportunities unlocked**

Depends on portable app pack metadata and backup status. Unlocks cutover gates,
Lumen Ops migration UI, and host cleanup confidence.

**Technical insight and implementation tips**

- Implement as a pure aggregator first.
- Keep readiness checks in a dedicated module, such as `ophelia.portability`.
- Return normalized `blockers`, `warnings`, `passed_checks`, and
  `unknown_checks`.
- Start with static/runtime files. Add live service probes later.
- Include a `readiness_level`: `ready`, `blocked`, `warning`, or `unknown`.

### 2. Redacted Env Shape Diff

**Summary**

Compare env requirements without exposing secret values.

**How it works**

Add:

```bash
./cli/ship env diff dragon-writer --environment production --json
```

or include the diff in `pack explain`, readiness, import preview, and deploy
plans. Compare:

- manifest `env`
- generated `env.example`
- runtime `env` keys
- data contract generated keys such as `DATABASE_URL`
- target host secret refs, when available

**User experience**

The report says which keys are present, missing, stale, generated, or unknown.
It never prints values.

**Why it improves the product**

Most failed deploys and imports are caused by invisible env drift. This makes the
problem observable and fixable.

**Complexity**

Small

**Impact**

High

**Dependencies or future opportunities unlocked**

Unlocks import previews, target bootstrap checks, Lumen Ops secret readiness, and
safer agent-run deploys.

**Technical insight and implementation tips**

- Model env entries as `{ "key": "...", "source": "...", "status": "present" }`.
- Treat values as sensitive even when they look harmless.
- Include `required_by` fields so agents know whether a key came from a service,
  addon, data contract, route, or manifest.
- Keep comparison deterministic and sorted by key.

### 3. Backup Freshness Receipt

**Summary**

Make backup coverage, recency, and validation status visible.

**How it works**

Add:

```bash
./cli/ship backup status dragon-writer --environment production --json
```

The command reports latest runtime backup, latest database dump, latest upload
archive, offsite marker, validation status, backup age, and threshold result.

**User experience**

The operator sees whether the app is backed up enough to touch.

**Why it improves the product**

Backups should not be trusted because they exist. They should be trusted because
Ophelia can prove they are fresh and listable.

**Complexity**

Medium

**Impact**

High

**Dependencies or future opportunities unlocked**

Unlocks cutover gates, cleanup gates, restore drills, and Lumen Ops backup
dashboards.

**Technical insight and implementation tips**

- Start by reading existing backup directories and manifests.
- Add optional validation commands later, such as `pg_restore -l` for custom
  Postgres dumps and `tar -tf` for archives.
- Make thresholds configurable in the pack, such as `max_age_hours`.
- Track `fresh`, `stale`, `missing`, `invalid`, and `unknown`.

### 4. Route And Domain Conflict Scanner

**Summary**

Detect route, domain, Caddy, and TLS conflicts before deploy or cutover.

**How it works**

Extend:

```bash
./cli/ship inspect conflicts --json
```

Scan active manifests, rendered Caddy snippets, staged manifests, catch-all
routes, path prefixes, host aliases, and on-demand TLS ask configuration.

**User experience**

Before a deployment, Ophelia warns when a domain is already owned or a route
would shadow another app.

**Why it improves the product**

Public edge breakage is one of the highest-impact mistakes on a multi-app VPS.
This catches it before Caddy reload.

**Complexity**

Medium

**Impact**

High

**Dependencies or future opportunities unlocked**

Unlocks safer consolidation, cutover planning, Lumen Ops route maps, and cleanup
planning.

**Technical insight and implementation tips**

- Normalize domains to lowercase.
- Normalize path matching rules before comparing.
- Treat catch-all `https://` and on-demand TLS as special host-level resources.
- Include owner metadata: `app`, `environment`, `manifest_path`,
  `release_id`.
- Distinguish conflicts from overlaps that are intentionally ordered.

### 5. Portability Score

**Summary**

Give each app an understandable portability rating.

**How it works**

Include a score in readiness output:

```json
{
  "portability_score": {
    "score": 72,
    "level": "needs-work",
    "factors": []
  }
}
```

Factors can include pack completeness, explicit data contracts, backup coverage,
restore drill status, image digest use, env readiness, and route clarity.

**User experience**

The operator can quickly compare apps and decide which should be hardened first.

**Why it improves the product**

It gives a productized way to prioritize portability work without reading every
manifest manually.

**Complexity**

Small

**Impact**

Medium

**Dependencies or future opportunities unlocked**

Depends on readiness, data contracts, and backup status. Unlocks Lumen Ops
portfolio views and cleanup planning.

**Technical insight and implementation tips**

- Keep score explainable. Every point should map to a factor.
- Do not let score override blockers. A critical blocker should still block.
- Include categories, for example `data`, `runtime`, `edge`, `backup`,
  `restore`, and `secrets`.
- Start with a simple deterministic rubric, then tune later.

### 6. Generated App Runbook

**Summary**

Generate a current per-app operator runbook from app and runtime metadata.

**How it works**

Add:

```bash
./cli/ship app runbook dragon-writer --environment production
./cli/ship app runbook dragon-writer --environment production --output docs/generated/dragon-writer.md
```

The runbook includes routes, services, data dependencies, backups, deploy
commands, export/import commands, restore drill status, rollback notes, and known
risks.

**User experience**

An operator or LLM agent gets a single current handoff document for the app.

**Why it improves the product**

It reduces stale tribal knowledge and makes app operations easier to delegate.

**Complexity**

Small

**Impact**

Medium

**Dependencies or future opportunities unlocked**

Unlocks Lumen Ops docs panes, incident handoff, and agent task prompts.

**Technical insight and implementation tips**

- Generate Markdown and JSON from the same internal model.
- Include timestamps and source file paths.
- Mark unknown data explicitly.
- Do not include secret values.
- Keep generated docs out of source control unless explicitly requested.

### 7. Receipt Browser Commands

**Summary**

Make Ophelia operation history easy to inspect.

**How it works**

Add:

```bash
./cli/ship receipts list --app dragon-writer --json
./cli/ship receipts show <receipt-id> --json
```

Receipts are stored under the runtime root, likely
`apps/<app>/<environment>/receipts/` or a compatibility path for current runtime
shape.

**User experience**

The operator can answer what changed, who ran it, when it ran, whether it passed,
and where the artifacts are.

**Why it improves the product**

Auditability makes automation safer. Agents can inspect prior operation context
before proposing another change.

**Complexity**

Medium

**Impact**

Medium

**Dependencies or future opportunities unlocked**

Unlocks Lumen run ledger integration, incident review, and rollback discovery.

**Technical insight and implementation tips**

- Use stable receipt ids and operation ids.
- Store receipts as JSON, optionally render human summaries.
- Include `app`, `environment`, `host`, `operation`, `status`, `started_at`,
  `completed_at`, `artifacts`, and `rollback`.
- Keep indexes simple at first: scan JSON files, then optimize later.

### 8. Pack Init Scaffolder

**Summary**

Create the initial Ophelia pack structure for an app.

**How it works**

Add:

```bash
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads
```

The command creates or updates:

```text
ophelia/
  runbook.md
  agent.md
  checks/
  hooks/
```

It can also print manifest snippets for `pack`, `host_requirements`, `data`, and
`hooks`.

**User experience**

Adding portability support feels guided and consistent instead of bespoke.

**Why it improves the product**

Scaffolding reduces setup friction and standardizes how future agents and app
repos interact with Ophelia.

**Complexity**

Medium

**Impact**

Medium

**Dependencies or future opportunities unlocked**

Unlocks faster onboarding for every app and future app-repo CI integration.

**Technical insight and implementation tips**

- Default to preview output unless `--write` is passed.
- Do not overwrite existing files without `--force`.
- Generate comments that explain required follow-up.
- Include app-specific agent instructions that point to `ship pack validate`,
  `ship pack explain`, and readiness commands.

## Major Features, Systems And Refactors

### 1. Portable App Pack System

**Summary**

Make each app environment a first-class, movable unit.

**How it works**

Extend `.ophelia.yml` with optional `pack`, `host_requirements`, `data`, and
`hooks` sections. Parse these into typed manifest models, write them into
`manifest.lock.json`, and validate them with `ship pack validate`.

**User experience**

Every app clearly states what it owns, what it needs, what data must move, and
what checks prove it is healthy.

**Why it improves the product**

This is the foundation for reliable migrations, restore drills, and future Lumen
Ops control.

**Complexity**

Large

**Impact**

Transformational

**Dependencies or future opportunities unlocked**

Unlocks every other selected major feature.

**Technical insight and implementation tips**

- Preserve backwards compatibility. Existing manifests should parse and render
  unchanged.
- Infer data contracts from `addons.postgres` and `addons.redis`, but mark them
  as inferred and insufficient for critical movement.
- Keep validation separate from parsing so old manifests remain accepted.
- Add focused tests for manifest parsing, lockfile output, and validation
  blockers.
- Use enums sparingly and allow future modes through clear validation errors.

### 2. Data Export And Import Pipeline

**Summary**

Treat databases, uploads, volumes, and runtime metadata as portable artifacts.

**How it works**

Add export and import command families:

```bash
./cli/ship app export plan dragon-writer --environment production --json
./cli/ship app export create dragon-writer --environment production --confirm <token>
./cli/ship app import plan ./exports/dragon-writer.production.export.tar.zst --json
./cli/ship app import apply ./exports/dragon-writer.production.export.tar.zst --confirm <token>
```

Export bundles should include runtime metadata, release metadata, checksums,
database dumps, volume archives, and operation receipts. Import should preview
target changes before restoring data.

**User experience**

Moving Dragon Writer becomes a planned artifact flow rather than a manual SSH
procedure.

**Why it improves the product**

It makes data safety and app movement repeatable.

**Complexity**

Large

**Impact**

Transformational

**Dependencies or future opportunities unlocked**

Depends on portable app packs and backup status. Unlocks restore drills,
cutovers, disaster recovery, and host replacement.

**Technical insight and implementation tips**

- Implement `export plan` before `export create`.
- Implement metadata-only export bundles before live database dumps.
- Use checksums for every artifact.
- Prefer Postgres custom format dumps for reliable listing and restore.
- Never include raw secrets in normal export bundles.
- Keep import apply initially limited to rehearsal namespaces until confidence is
  high.

### 3. Restore Drill System

**Summary**

Prove that an app backup/export can restore into an isolated target.

**How it works**

Add:

```bash
./cli/ship app restore-drill plan dragon-writer --environment production --json
./cli/ship app restore-drill apply dragon-writer --environment production --confirm <token>
```

The drill restores into an isolated namespace or target profile, starts only the
services needed for verification, runs health/data checks, writes a receipt, and
cleans up only if the plan explicitly allows cleanup.

**User experience**

The operator sees a durable receipt saying whether restore actually works.

**Why it improves the product**

Backups are not meaningful until restore is tested.

**Complexity**

Large

**Impact**

High

**Dependencies or future opportunities unlocked**

Depends on export/import and data verification hooks. Unlocks production cutover
gates and cleanup confidence.

**Technical insight and implementation tips**

- Run drills against isolated Compose project names.
- Never restore over production services.
- Use separate ports, hostnames, and volumes for rehearsal.
- Preserve failed drill artifacts for inspection.
- Track drill freshness in readiness output.

### 4. Cutover Orchestrator

**Summary**

Coordinate the risky transition from source host to target host.

**How it works**

Add:

```bash
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh --json
./cli/ship app cutover apply dragon-writer --from spaceship --to ovh --confirm <token>
```

The plan checks readiness, backup freshness, target import status, restore drill
status, route conflicts, freeze hooks, DNS checklist, rollback notes, and source
retention policy. Apply sequences only the approved steps.

**User experience**

A production migration becomes a guided, gated workflow with clear rollback
points.

**Why it improves the product**

Cutover is the riskiest part of migration. Ophelia should make it explicit,
audited, and recoverable.

**Complexity**

Massive

**Impact**

Transformational

**Dependencies or future opportunities unlocked**

Depends on packs, export/import, restore drills, readiness, backup status, and
route conflict scanning. Unlocks safe Spaceship exit.

**Technical insight and implementation tips**

- Build as a sequencer over existing primitives, not as one giant custom command.
- Keep source freeze and DNS or Caddy route changes as separate plan steps.
- Require a retention window by default.
- Do not delete source data.
- Write one cutover receipt plus per-step receipts.
- Make rollback status explicit after each step.

### 5. Per-App Isolation Refactor

**Summary**

Move from a shared internal network to app-scoped internals.

**How it works**

Keep a shared edge network for Caddy. Generate a private internal network per
app/environment. Shared Postgres or Redis pools should be explicit dependencies,
not ambient services reachable by every app.

Target shape:

```text
ophelia-edge
dragon-writer-production-internal
lumen-production-internal
stillup-production-internal
```

**User experience**

Dragon Writer, Lumen, Stillup, and SaaS apps can live on the same host without
accidental coupling.

**Why it improves the product**

It improves security, clarity, portability, and future business/personal
separation.

**Complexity**

Large

**Impact**

High

**Dependencies or future opportunities unlocked**

Unlocks safer multi-app hosts, cleaner app movement, better topology views, and
future Lumen Ops service maps.

**Technical insight and implementation tips**

- Make this opt-in or compatibility-gated first.
- Keep current `ophelia-internal` behavior for existing manifests until migrated.
- Add generated aliases that remain stable inside each app network.
- Write migration docs for shared Postgres and Redis access.
- Test rendered Compose carefully.

### 8. Lumen Ops Adapter Surface

**Summary**

Expose Ophelia as a stable agent and product backend for Lumen Ops.

**How it works**

Provide JSON inventory, operation descriptors, plans, receipts, and approval-ready
payloads. Lumen Ops should call Ophelia primitives instead of raw shell commands
for supported workflows.

Initial surfaces:

```bash
./cli/ship actions --json
./cli/ship status --json
./cli/ship app readiness <app> --json
./cli/ship pack explain <manifest> --json
./cli/ship receipts list --json
```

Later, `ship api serve` can expose the same capabilities over a local or
host-bound API.

**User experience**

Lumen can display hosts, apps, health, backups, plans, approvals, and receipts
without asking an agent to improvise shell commands.

**Why it improves the product**

It lets Ophelia remain a reliable engine while Lumen becomes the cockpit.

**Complexity**

Large

**Impact**

Transformational

**Dependencies or future opportunities unlocked**

Depends on stable JSON contracts and receipts. Unlocks Lumen Ops UI, approval
inbox, run ledger, mobile visibility, and safer LLM automation.

**Technical insight and implementation tips**

- Treat CLI JSON as the first API. Do not wait for HTTP.
- Keep schemas stable and versioned.
- Expose action metadata: name, description, risk, dry-run support, required
  args, output schema, and whether approval is required.
- Make API errors structured.
- Include artifact paths that Lumen can render later.

## Suggested Implementation Order

1. Portable app pack parser and lockfile output.
2. Pack validate and explain commands.
3. Redacted env shape diff.
4. Route and domain conflict scanner improvements.
5. Backup freshness status.
6. App readiness checklist and portability score.
7. Receipt storage and browser commands.
8. Generated app runbook.
9. Export plan and metadata-only export bundle.
10. Export create for runtime files and static/volume archives.
11. Postgres dump support.
12. Import plan.
13. Rehearsal import apply.
14. Restore drill plan and apply.
15. Per-app isolation compatibility mode.
16. Cutover plan.
17. Cutover apply.
18. Lumen Ops action descriptors and API hardening.

## Testing Strategy

- Unit-test manifest parsing and backwards compatibility.
- Unit-test pack validation blockers and warnings.
- Snapshot-test representative JSON plans and receipts.
- Test redaction with env values that look like URLs, tokens, private keys, and
  normal strings.
- Test route conflict normalization.
- Test backup status with fixture directories.
- Test export bundle manifests and checksums without requiring Docker.
- Keep live Docker/Postgres integration tests optional and clearly separated.

## Documentation Work Needed

- Update `manifest-spec.md` as fields graduate from draft to implemented.
- Add operator examples to `operator-runbook.md`.
- Add app-specific Dragon Writer examples.
- Add Lumen Ops adapter docs once JSON action descriptors stabilize.
- Keep `dragon-writer-migration-runbook.md` aligned with implemented commands.

## Out Of Scope For This Selected Batch

The following ideas remain useful but are not part of this selected scope:

- host bootstrap and reconcile
- provider-backed host inventory adapters beyond the initial read-only planner
- cost optimization planner
- full web UI
- automatic DNS mutation
- automatic cleanup of duplicate apps or old containers
- automatic Quark, Prism, or OpenClaw decommissioning
- Kubernetes or Nomad support
