# Job / Action API Notes

The local HTTP Job/Action API foundation is implemented with `ship api serve`.
It binds to `127.0.0.1` by default and exposes JSON endpoints for actions,
jobs, job events, host inventory, and registries.

Quark can call these read-only commands today:

- `ship self-test --json` (first smoke command; confirms the install is healthy)
- `ship schema manifest --json` (manifest JSON schema, draft 2020-12)
- `ship validate <manifest> --json`
- `ship explain <manifest> --json`
- `ship diff <manifest> --json`
- `ship deploy <manifest> --plan --json`
- `ship status --json`
- `ship doctor --json`
- `ship inspect conflicts --json`
- `ship drift <manifest> --json`
- `ship releases <app> --json`
- `ship pack validate <manifest> --json`
- `ship pack explain <manifest> --json`
- `ship pack init --app <app> --json` for preview-only scaffolding
- `ship env diff <app> --environment <env> --json`
- `ship backup status <app> --environment <env> --json`
- `ship app readiness <app> --environment <env> --json`
- `ship app runbook <app> --environment <env> --json`
- `ship app export plan <app> --environment <env> --json`
- `ship app import plan <bundle-or-metadata> --json`
- `ship app restore-drill plan <app> --environment <env> --json`
- `ship app cutover plan <app> --from <source> --to <target> --json`
- `ship app traffic plan <app> --from <source> --to <target> --target-origin <origin> --json`
- `ship app traffic rollback plan <app> --receipt <traffic-receipt-id> --json`
- `ship app isolation plan <app> --environment <env> --json`
- `ship receipts list --json`
- `ship receipts show <receipt-id> --json`
- `ship receipts show latest:<app> --json`
- `ship workflow run <workflow-id-or-alias> --preview --json`
- `ship state query receipts --ref latest:<operation> --json`

Mutating commands require confirmation tokens from their matching plans:

- `ship deploy --apply --confirm <token>` for production manifests
- `ship rollback apply ... --confirm <token>`
- `ship backup create ... --confirm <token>`
- `ship restore apply ... --confirm <token>`
- `ship app export create ... --confirm <token>` for metadata/runtime export bundles
- `ship app import apply ... --confirm <token>` for isolated rehearsal import previews
- `ship app restore-drill apply ... --confirm <token>` for artifact/listability drill receipts
- `ship app cutover apply ... --confirm <token>` for cutover checkpoint receipts
- `ship app traffic apply ... --confirm <token>` for production traffic checkpoint receipts
- `ship app traffic rollback apply ... --confirm <token>` for file-provider traffic rollback receipts

Isolation apply is not exposed as a standalone mutating action descriptor.
Per-app internal networks are enabled by manifest opt-in
`networking.internal: per-app`, then rendered through the normal deploy
plan/apply flow. Export create, import apply, restore drill apply, and cutover
apply are exposed as dry-run-first action descriptors, but they only write local
artifacts and receipts. Cutover apply does not mutate Caddy or DNS.
Traffic apply records exact DNS/Caddy provider intent and writes a checkpoint
receipt by default. File-backed provider execution is available through typed
fields: `dns_provider`, `caddy_provider`, `provider_config`, and
`execute_provider_mutation`. It still requires the prior dry-run confirmation
record, a matching token, a provider config file, and provider config
`allow_mutation: true`. The file backend writes local DNS/Caddy artifacts and
receipts. The Cloudflare DNS backend uses `api_token_env`, lists matching DNS
records, patches exactly one existing record by default, and never stores token
values in plans or receipts. Creating a missing record requires
`allow_create: true`. Cloudflare TTL must be automatic `1` or between `30` and
`86400` seconds. Live Caddy reload is available only when the Caddy provider
config also sets `reload: true` and `allow_reload: true`, with `sites_dir`
matching `<runtime_root>/caddy/sites.d`.
Traffic plan/apply also accept target health fields:
`target_health_url`, `run_target_health`, `target_health_timeout`, and
`target_health_expect_status`. Health checks are read-only and included in the
confirmation-token input hash. Health URLs must be http(s) URLs without
credentials, query strings, or fragments.
Traffic rollback is a separate action pair, `app.traffic.rollback.plan` and
`app.traffic.rollback.apply`. Rollback apply restores prior provider state
captured by a traffic apply receipt and blocks cases that would require deleting
previously absent records or files.

API endpoints:

- `GET /health`
- `GET /actions`
- `GET /commands`
- `GET /schema/manifest`
- `POST /jobs`
- `GET /jobs/<job_id>`
- `GET /jobs/<job_id>/events`
- `POST /jobs/<job_id>/cancel`
- `GET /host/inventory`
- `GET /registry/manifests`
- `GET /registry/releases`
- `GET /operations`
- `POST /operations/run`

There is no arbitrary shell endpoint. Jobs use typed schemas, idempotency keys,
job state, audit records, local-only binding, and optional artifact link fields.
Operation templates return explicit step lists and result artifacts; they do
not execute autonomous deploy workflows.

Mutating API jobs are stricter than direct staging CLI applies: `deploy.apply`,
`deploy.rollback.apply`, `backup.create`, and `restore.apply` must first run
with `dry_run: true`. The dry-run stores a confirmation record under the runtime
root, returns `required_confirmation_token`, `confirmation_expires_at`, and
`exact_apply_input`, and the apply job must submit the matching token before it
expires. Successful apply consumes the token.

## Operation Digest Blocks

Canonical plan, report, and receipt envelopes include a compact `digest` block.
The digest is redundant with the full payload, but bounded for operator cards,
Lumen previews, and downstream agents that need a quick summary.

Digest fields include:

- `operation`, `app`, `environment`, `risk`, and `status`.
- `counts` for blockers, warnings, checks, artifacts, and planned changes.
- `blocker_codes` and `warning_codes` for fast filtering.
- `confirmation.required`, `confirmation.token_present`, and a redacted
  `confirmation.apply_command` when the operation exposes one.
- `mutation.dry_run`, `mutation.changes_planned`, and
  `mutation.artifacts_planned`.
- `rollback.available` and `rollback.note`.

The digest is produced through the same redaction path as other operator JSON.
Command strings are token-scrubbed before they are added to the digest.

Completion callbacks are disabled by default. When enabled in
`config/ophelia-actions.json`, Ophelia posts the completed job JSON to
`completion_callback_url` and signs the payload with `X-Ophelia-Signature:
sha256=<hmac>` when `OPHELIA_CALLBACK_SECRET` is set.

## Command Catalog Discovery

`ship commands catalog --json` and `GET /commands` expose a single, sorted
catalog of every agent-facing command. The catalog is derived from the same
action registry that backs `GET /actions`, plus a small set of CLI-only
commands (`commands catalog`, `validate`, `render`, `receipts list`,
`receipts show`), so agents can discover risk and mutation metadata without
parsing help text.

CLI output is exactly:

```json
{"schema_version": 1, "kind": "ophelia.command_catalog", "commands": [ ... ]}
```

`GET /commands` returns `{"commands": [ ... ]}` with the same descriptor list.
Use `ship commands catalog --human` for an aligned operator table; the default
output is JSON.

Each `CommandDescriptor` has these fields:

- `command`: the CLI invocation, for example `ship app traffic plan`.
- `operation`: the stable operation id, matching an action id when applicable
  (for example `app.traffic.plan`).
- `summary`: a one-line description.
- `risk`: coarse risk tier (`low`, `medium`, `high`, `critical`). Read-only
  commands are `low`; production-capable deploy/traffic mutations are
  `critical`.
- `mutates_state`: true for mutating actions.
- `requires_confirmation`: true when a confirmation token is required.
- `plan_command` / `apply_command`: the paired plan and apply invocations for
  dry-run-first mutating flows; null when there is no pair.
- `json_kind`: the envelope kind the command emits (`ophelia.plan`,
  `ophelia.receipt`, `ophelia.report`, or `ophelia.command_catalog`).
- `args_schema`: a JSON-schema-ish input shape. It only describes input shapes
  and never carries secret values or raw-secret defaults.
- `output_schema_ref`: a reference to the output envelope version.
- `artifacts`: artifact kinds the command may produce.
- `safety_notes`: human-readable safety notes plus policy gates.
- `examples`: copyable example invocations. These are surfaced by
  `ship commands catalog --json`, `GET /commands`, and Lumen action
  descriptors.

## Doctor Diagnostics

`ship doctor --json` emits
`{"schema_version": 1, "kind": "ophelia.doctor", ...}` and returns non-zero
only when a non-warning blocker is present. Fresh checkout gaps, such as missing
Docker, no provider config, or no state DB yet, are warnings so the command can
be used during setup.

The expanded report checks Python and PyYAML availability, runtime-root
writability and layout, manifest parsing/rendering, manifest registry health,
state DB freshness, command catalog completeness, changelog consistency, Docker
and `gh` status, provider config validity, and a redaction smoke test.

## Operation Reference Aliases

Receipt and workflow continuation commands accept exact IDs as before, plus
aliases resolved through the shared operation-reference resolver:

- `latest`: newest matching receipt or workflow in the current command scope.
- `latest:<app>`: newest receipt or workflow for an app.
- `latest:<operation>`: newest receipt for an operation, for example
  `latest:app.traffic.apply`.
- Short ID prefixes, when they match exactly one candidate.
- Receipt/workflow JSON paths.

Ambiguous prefixes return an explicit blocker with candidate IDs. Commands do
not guess. Traffic rollback resolves aliases before token generation and apply,
so confirmation tokens are bound to the resolved receipt path/id, not to a
moving alias.

Alias-enabled surfaces include `receipts show`, `restore-drills show`,
`state query receipts --ref`, `workflow show`, `workflow run`, and
`app traffic rollback plan|apply --receipt`.

## Workflow Run Preview

`ship workflow run <workflow-ref> --preview --json` emits
`{"schema_version": 1, "kind": "ophelia.workflow_preview", ...}` and never
executes a node or writes a receipt. It resolves the workflow reference,
substitutions, dependency outcomes, unresolved placeholders, local executable
paths, blockers, skipped nodes, and ready nodes.

The non-preview `ship workflow run` path still executes only stored nodes marked
non-mutating, passes commands as argv arrays, and writes a receipt. Both preview
and run receipts include the shared `digest` block.

## Provider And Secret Validation (Phase 3)

These are read-only validators. They never print secret values and never
require a confirmation token.

`ship providers validate --config <path> --json` checks a traffic provider
config file (the same shape consumed by `app traffic plan/apply`). It emits
`{"schema_version": 1, "kind": "ophelia.provider_config.validation", ...}`.
It blocks raw inline token keys (a token value embedded in the config) and
requires `api_token_env` for the Cloudflare DNS provider, so tokens are always
resolved from a named environment variable rather than stored in the config.

`ship providers explain --config <path> --json` describes the same config in
structured form. It emits
`{"schema_version": 1, "kind": "ophelia.provider_config.explanation", ...}`.
It reports the resolved provider kinds, mutation/reload gates, and the
`api_token_env` name only, never the token value.

`ship secrets audit <manifest-or-app> --environment <env> --json` reports the
presence of required secrets/env keys. It emits
`{"schema_version": 1, "kind": "ophelia.secrets_audit", ...}`. The audit lists
key names plus `present` booleans only. It never reads, echoes, or stores any
secret value.

## Receipt Timeline And Dry-Run Diffs (Phase 5)

`ship receipts timeline --json` returns a chronological view of stored receipts
with optional filters `--app`, `--environment`, `--operation`, `--status`,
`--since`, and `--until`. It emits
`{"schema_version": 1, "kind": "ophelia.receipt_timeline", ...}`. It is
read-only and reads the same receipt store as `receipts list` / `receipts show`.

Dry-run plans for diff-producing operations attach a redacted diff artifact:

```json
{
  "kind": "ophelia.artifact.diff",
  "redacted": true,
  "path": "receipts/.../diff.json"
}
```

Diff artifacts are `redacted: true` and referenced by path only inside plans.
The plan carries the artifact reference, not inline diff content, so secret or
bulky payloads never land in the plan envelope.

## Runtime State Read-Model (Phase 6)

`ship state status|rebuild|query receipts --json` exposes a local SQLite
read-model of runtime state:

- `ship state status --json` emits
  `{"schema_version": 1, "kind": "ophelia.state_status", ...}`.
- `ship state rebuild --json` emits
  `{"schema_version": 1, "kind": "ophelia.state_rebuild", ...}`.
- `ship state query receipts --json` emits
  `{"schema_version": 1, "kind": "ophelia.state_query", ...}`.

`state rebuild` writes ONLY the local SQLite index under the runtime root. It is
local index creation, not a VPS mutation, so it does not require a production
confirmation token.

Read-only API endpoints back the same read-model:

```text
GET /state/status
GET /state/apps
GET /state/receipts
GET /state/routes
GET /state/backups
```

When the index is absent, these endpoints degrade gracefully: they return
`available: false` / `needs_rebuild: true` rather than failing. They never
return `500`.

## Policy Engine (Phase 7)

`ship policy validate|explain|evaluate --json` runs the policy engine:

- `ship policy evaluate --json` emits
  `{"schema_version": 1, "kind": "ophelia.policy_result", ...}`.
- `ship policy validate --json` emits
  `{"schema_version": 1, "kind": "ophelia.policy_validation", ...}`.
- `ship policy explain --json` emits
  `{"schema_version": 1, "kind": "ophelia.policy_explanation", ...}`.

Evaluation is fail-closed on unknown required conditions (an unrecognized
required condition becomes a blocker) and fail-open with warnings on unknown
advisory keys (an unrecognized advisory key is surfaced as a warning, not a
blocker).

Policy resolution order:

```text
1. explicit --policy <path>
2. <runtime_root>/policy/ophelia-policy.yml
3. repo config/ophelia-policy.yml
```

Policy results ride under plan `checks`, so agents see policy blockers and
warnings in the same `checks` array used for every other plan gate.
