# Ophelia 2.0 Technical Handoff For Lumen VPS Deployment

Date: 2026-06-20

Audience: the agent responsible for deploying Lumen and officially setting up the VPS platform.

Scope: Ophelia remains the VPS deploy and runtime substrate. This handoff describes the Ophelia 2.0 portability, readiness, receipt, and production traffic automation foundation that is now implemented.

## Non-Negotiable Safety Rules

- Do not rename Ophelia.
- Do not mutate production VPS state without an explicit Ophelia plan, reviewed JSON output, and the matching confirmation token.
- Do not SSH into a VPS for mutation unless the operator explicitly authorizes that separate operational step.
- Do not delete containers, images, volumes, backups, apps, DNS records, env files, or runtime roots.
- Do not print raw env values, secrets, database URLs, private keys, provider tokens, or credentials.
- Use JSON output for agent consumption.
- Treat every mutating command as dry-run-first.
- Prefer receipts as the durable integration surface for Lumen Ops.

## Implementation Summary

Ophelia 2.0 now has a portability foundation for moving apps between hosts, rehearsing imports and restore drills, checking readiness, producing runbooks, and planning production traffic movement.

The implementation adds:

- Portable app pack manifest sections.
- Backwards-compatible inferred data contracts from legacy `addons.postgres` and `addons.redis`.
- Pack validation, explanation, and scaffold preview commands.
- Redacted env shape diffing.
- Backup freshness reporting.
- Route and domain conflict scanning with owner metadata.
- App movement readiness aggregation.
- Portability scoring.
- Generated app runbook payloads.
- JSON receipt storage and browsing.
- Export planning and confirmed export bundle creation.
- Import planning and confirmed rehearsal import previews.
- Restore drill planning and confirmed isolated drill receipts.
- Cutover planning and confirmed checkpoint receipts.
- Per-app isolation planning and opt-in Compose network rendering.
- Production traffic planning, checkpoint apply, provider execution, and provider rollback.
- Local Job/Action API descriptors for Lumen and other agent callers.

No production VPS state was mutated during implementation or verification.

## Main Files Added Or Changed

Core:

- `src/ophelia/manifest.py`
- `src/ophelia/portability.py`
- `src/ophelia/operation_schema.py`
- `src/ophelia/actions.py`
- `src/ophelia/api.py`
- `src/ophelia/conflicts.py`
- `src/ophelia/templates.py`
- `templates/compose/app.compose.tpl`

CLI:

- `src/ophelia/commands/app.py`
- `src/ophelia/commands/pack.py`
- `src/ophelia/commands/env.py`
- `src/ophelia/commands/backup.py`
- `src/ophelia/commands/receipts.py`
- `src/ophelia/commands/inspect.py`
- `src/ophelia/commands/__init__.py`

Docs and examples:

- `README.md`
- `docs/manifest-spec.md`
- `docs/portable-app-pack-spec.md`
- `docs/ophelia-next-architecture.md`
- `docs/dragon-writer-migration-runbook.md`
- `docs/selected-portability-feature-roadmap.md`
- `docs/preflight-and-safety.md`
- `docs/job-action-api.md`
- `docs/releases-and-rollback.md`
- `docs/platform-handbook.md`
- `examples/dragonwriter.ophelia.yml`

Tests:

- `tests/test_portability.py`
- `tests/test_manifest.py`
- `tests/test_actions.py`
- `tests/test_conflicts.py`

## Manifest Contract

Existing manifests remain compatible. The new sections are optional.

New optional sections:

```yaml
pack:
  portability: critical
  owner: personal
  description: Dragon Writer production app
  deploy_binding_file: ophelia/deploy.json

host_requirements:
  arch: amd64
  min_memory: 1g
  min_disk_free: 20g
  requires_edge: true
  requires_docker: true

networking:
  edge: shared
  internal: per-app

data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true

hooks:
  pre_export: ophelia/hooks/pre-export.sh
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh
```

### Backwards Compatibility

If an older manifest has:

```yaml
addons:
  postgres: true
  redis: true
```

and omits `data`, Ophelia infers:

```yaml
data:
  postgres:
    mode: shared-postgres-database
    inferred_from_addon: true
  redis:
    mode: redis-logical-db
    inferred_from_addon: true
```

This preserves old deploy behavior while making movement readiness explicit. Critical apps with inferred contracts will warn until export, import, backup, and verification behavior is explicitly declared.

### Networking

Default behavior remains shared networking:

```yaml
networking:
  edge: shared
  internal: shared
```

Opt-in per-app internal networking:

```yaml
networking:
  internal: per-app
```

When enabled, Compose uses a private internal network named:

```text
<app>-<environment>-internal
```

This is rendered only when the manifest opts in. Existing apps stay on the shared `ophelia-internal` network unless changed intentionally.

## Standard JSON Envelopes

### Plan Envelope

Every plan uses the stable shape:

```json
{
  "schema_version": 1,
  "kind": "ophelia.plan",
  "operation": "app.traffic.plan",
  "operation_id": "app-traffic-plan.dragon-writer.production.20260620T191747Z",
  "app": "dragon-writer",
  "environment": "production",
  "risk": "critical",
  "dry_run": true,
  "summary": "Production traffic automation plan for dragon-writer from spaceship to ovh.",
  "blockers": [],
  "warnings": [],
  "checks": [],
  "changes": [],
  "artifacts": [],
  "confirmation_required": true,
  "confirmation_token": "example-token",
  "exact_apply_input": {
    "command": "ship app traffic apply ..."
  }
}
```

### Report Envelope

Read-only reports use:

```json
{
  "schema_version": 1,
  "kind": "ophelia.report",
  "operation": "app.readiness",
  "operation_id": "app-readiness.dragon-writer.production.20260620T191747Z",
  "status": "blocked",
  "app": "dragon-writer",
  "environment": "production",
  "summary": "Readiness for dragon-writer production: blocked.",
  "blockers": [],
  "warnings": [],
  "checks": [],
  "artifacts": [],
  "inputs_redacted": true
}
```

### Receipt Envelope

Receipts use:

```json
{
  "schema_version": 1,
  "kind": "ophelia.receipt",
  "operation": "app.export.create",
  "operation_id": "app-export-create.dragon-writer.production.20260620T191747Z",
  "plan_operation_id": "app-export-plan.dragon-writer.production.20260620T191700Z",
  "status": "succeeded",
  "app": "dragon-writer",
  "environment": "production",
  "started_at": "2026-06-20T19:17:00Z",
  "completed_at": "2026-06-20T19:17:47Z",
  "actor": "operator",
  "inputs_redacted": true,
  "artifacts": [],
  "checks": [],
  "rollback": {
    "available": false,
    "note": "Export create only wrote local export artifacts and did not mutate source runtime."
  }
}
```

## Commands For Lumen And VPS Deployment Agents

Use `--json` for machine-readable output.

### Pack

Validate a pack:

```bash
./cli/ship pack validate examples/dragonwriter.ophelia.yml --json
```

Explain a pack:

```bash
./cli/ship pack explain examples/dragonwriter.ophelia.yml --json
```

Preview scaffolding:

```bash
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
```

Write scaffolding only when explicitly requested:

```bash
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --write --json
```

`pack init` refuses to overwrite existing files unless `--force` is passed.

### Env Shape Diff

```bash
./cli/ship env diff dragon-writer --environment production --json
```

This reports required keys, missing keys, placeholder keys, and source hints. It never prints env values.

### Backup Freshness

```bash
./cli/ship backup status dragon-writer --environment production --json
```

This reads existing backup directories and metadata. It does not restore data and does not delete anything.

### Readiness

```bash
./cli/ship app readiness dragon-writer --environment production --json
```

Readiness aggregates:

- Pack validation.
- Env shape.
- Backup freshness.
- Route/domain conflicts.
- Release metadata.
- Restore drill receipts.
- Portability score.
- Network compatibility.

Readiness can return `ready`, `warning`, or `blocked`.

### Runbook

```bash
./cli/ship app runbook dragon-writer --environment production --json
```

The runbook is generated from the same data model as readiness. It is useful for handoff to a human operator or a deployment agent.

### Export

Plan export:

```bash
./cli/ship app export plan dragon-writer --environment production --json
```

Create export bundle after reviewing the plan:

```bash
./cli/ship app export create dragon-writer --environment production --confirm <token> --json
```

By default, export create writes:

- Export metadata.
- Redacted runtime files.
- Static/local data archives where declared.
- Checksums.
- `receipts/export-plan.json`.
- `receipts/export-create.json`.
- A deterministic `.tar` archive.
- Optional `.tar.zst` when local `zstd` is available.

Postgres export is opt-in:

```bash
./cli/ship app export plan dragon-writer --environment production --include-postgres --json
./cli/ship app export create dragon-writer --environment production --include-postgres --confirm <token> --json
```

The Postgres path uses allowlisted `pg_dump` behavior and redacts `DATABASE_URL` and password values from receipts.

### Import

Plan import from a bundle or metadata file:

```bash
./cli/ship app import plan ./exports/dragon-writer.production.export/manifest.json --json
```

Apply rehearsal import preview:

```bash
./cli/ship app import apply ./exports/dragon-writer.production.export/manifest.json --confirm <token> --json
```

Import apply writes an isolated rehearsal preview. It does not replace active production runtime state.

### Restore Drill

Plan restore drill:

```bash
./cli/ship app restore-drill plan dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --json
```

Apply restore drill:

```bash
./cli/ship app restore-drill apply dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --confirm <token> --json
```

Restore drill apply validates artifact readability and writes receipts. It does not destructively restore over production state.

### Cutover

Plan cutover:

```bash
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh --environment production --json
```

Apply cutover checkpoint:

```bash
./cli/ship app cutover apply dragon-writer --from spaceship --to ovh --environment production --confirm <token> --json
```

Cutover apply writes a checkpoint receipt. It does not mutate Caddy or DNS.

### Production Traffic Automation

Plan traffic movement:

```bash
./cli/ship app traffic plan dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --json
```

Apply traffic checkpoint:

```bash
./cli/ship app traffic apply dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --confirm <token> \
  --json
```

Default `traffic apply` is checkpoint-only. It records intent and writes receipts. It does not mutate DNS or Caddy unless provider execution is explicitly requested.

### Target Health Gate

Plan with read-only target health:

```bash
./cli/ship app traffic plan dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --target-health-url https://dragonwriter-target.example.net/health \
  --run-target-health \
  --json
```

Apply with the same target health inputs:

```bash
./cli/ship app traffic apply dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --target-health-url https://dragonwriter-target.example.net/health \
  --run-target-health \
  --confirm <token> \
  --json
```

Target health URLs must:

- Use `http` or `https`.
- Have no credentials.
- Have no query string.
- Have no fragment.

This prevents secrets from leaking into plans, logs, receipts, or job records.

### File Provider Execution

Plan file-backed DNS and Caddy execution:

```bash
./cli/ship app traffic plan dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --dns-provider file \
  --caddy-provider file \
  --provider-config ./traffic-providers.json \
  --execute-provider-mutation \
  --json
```

Apply file-backed DNS and Caddy execution:

```bash
./cli/ship app traffic apply dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --dns-provider file \
  --caddy-provider file \
  --provider-config ./traffic-providers.json \
  --execute-provider-mutation \
  --confirm <token> \
  --json
```

Minimal provider config:

```json
{
  "schema_version": 1,
  "dns": {
    "provider": "file",
    "record_file": "/var/lib/ophelia/traffic/dns-records.json",
    "allow_mutation": true
  },
  "caddy": {
    "provider": "file",
    "sites_dir": "/var/lib/ophelia/traffic/caddy-sites",
    "allow_mutation": true
  }
}
```

File DNS writes JSON records and captures previous/next values in the traffic receipt.

File Caddy writes:

```text
<app>.<environment>.traffic.caddy
```

and snapshots any previous file into the traffic receipt directory.

### Live Caddy Validate And Reload

Caddy validate/reload is gated separately:

```json
{
  "caddy": {
    "provider": "file",
    "sites_dir": "/home/kyle/ophelia-runtime/caddy/sites.d",
    "runtime_root": "/home/kyle/ophelia-runtime",
    "ophelia_root": "/home/kyle/Developer/platforms/ophelia",
    "allow_mutation": true,
    "validate": true,
    "reload": true,
    "allow_reload": true,
    "timeout": 30
  }
}
```

Rules:

- `allow_mutation: true` is required.
- `reload: true` requires `allow_reload: true`.
- If validate or reload is requested, `sites_dir` must match `<runtime_root>/caddy/sites.d`.
- Timeout must be a positive integer.
- Reload uses Ophelia's typed Caddy manager, not arbitrary shell snippets.

### Cloudflare DNS Provider

Plan Cloudflare DNS execution:

```bash
./cli/ship app traffic plan dragon-writer \
  --from spaceship \
  --to ovh \
  --target-origin dragonwriter-target.example.net \
  --environment production \
  --dns-provider cloudflare \
  --provider-config ./traffic-providers.json \
  --execute-provider-mutation \
  --json
```

Provider config:

```json
{
  "dns": {
    "provider": "cloudflare",
    "zone_id": "replace-with-zone-id",
    "api_token_env": "CLOUDFLARE_API_TOKEN",
    "allow_mutation": true,
    "allow_create": false,
    "proxied": false
  }
}
```

Rules:

- Token values are never stored in plans or receipts.
- `api_token_env` names the environment variable that contains the token.
- Apply lists matching DNS records.
- Apply patches exactly one existing record by default.
- Apply never deletes DNS records.
- Creating a missing record requires `allow_create: true`.
- TTL must be `1` for Cloudflare automatic TTL or between `30` and `86400` seconds.
- Rollback uses `PATCH` to restore previous records captured in the apply receipt.
- Rollback blocks deletion-only cases.

Official Cloudflare DNS API references:

- List DNS records: https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/
- Create DNS records: https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/
- Update DNS records: https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/update/

### Traffic Rollback

Plan rollback from a traffic apply receipt:

```bash
./cli/ship app traffic rollback plan dragon-writer \
  --receipt <traffic-receipt-id> \
  --environment production \
  --json
```

Apply rollback:

```bash
./cli/ship app traffic rollback apply dragon-writer \
  --receipt <traffic-receipt-id> \
  --environment production \
  --confirm <token> \
  --json
```

Rollback restores only previous provider state captured in the receipt.

Rollback refuses:

- DNS deletion-only rollback.
- Caddy deletion-only rollback.
- Unsupported provider state.
- Missing previous snapshots.

### Isolation Planning

```bash
./cli/ship app isolation plan dragon-writer --environment production --json
```

This reports current compatibility mode and target per-app network shape. There is no standalone isolation apply command. Per-app networking is applied through the normal manifest deploy flow after opt-in.

### Receipt Browser

List receipts:

```bash
./cli/ship receipts list --app dragon-writer --json
```

Show a receipt:

```bash
./cli/ship receipts show <receipt-id> --json
```

Receipts are stored under the runtime root, generally in app-specific receipt directories.

## Job And Action API

The local Job/Action API is exposed by:

```bash
./cli/ship api serve
```

It binds only to `127.0.0.1` or `localhost` by default.

Endpoints:

```text
GET  /health
GET  /actions
POST /jobs
GET  /jobs/<job_id>
GET  /jobs/<job_id>/events
POST /jobs/<job_id>/cancel
GET  /host/inventory
GET  /registry/manifests
GET  /registry/releases
GET  /operations
POST /operations/run
```

There is no arbitrary shell endpoint.

Jobs use:

- Typed schemas.
- Unknown-field rejection.
- Idempotency keys.
- Per-app/environment locks for mutating actions.
- Confirmation records.
- Audit records.
- Optional artifact link metadata.
- Local-only binding.

### Action IDs

Portability and production traffic action IDs:

```text
pack.validate
pack.explain
pack.init.preview
env.diff
backup.status
app.readiness
app.runbook
app.export.plan
app.export.create
app.import.plan
app.import.apply
app.restore-drill.plan
app.restore-drill.apply
app.cutover.plan
app.cutover.apply
app.traffic.plan
app.traffic.apply
app.traffic.rollback.plan
app.traffic.rollback.apply
app.isolation.plan
receipts.list
receipts.show
```

Mutating action IDs are dry-run-first:

```text
app.export.create
app.import.apply
app.restore-drill.apply
app.cutover.apply
app.traffic.apply
app.traffic.rollback.apply
```

### Job Dry-Run Flow

Create a dry-run job:

```json
{
  "action_id": "app.traffic.apply",
  "inputs": {
    "app": "dragon-writer",
    "environment": "production",
    "source_host": "spaceship",
    "target_host": "ovh",
    "target_origin": "dragonwriter-target.example.net",
    "dry_run": true
  },
  "requested_by": "lumen",
  "source": "lumen-vps-deploy-agent",
  "idempotency_key": "dragon-writer-traffic-apply-preview"
}
```

A mutating dry-run returns `waiting_for_confirmation` and a payload containing:

```json
{
  "required_confirmation_token": "example-token",
  "confirmation_expires_at": "2026-06-20T19:30:00Z",
  "exact_apply_input": {
    "dry_run": false,
    "confirm_token": "example-token"
  }
}
```

The apply job must use the exact same inputs, except:

- `dry_run` becomes `false`.
- `confirm_token` is set to the issued token.

If any meaningful input changes, the confirmation input hash changes and apply fails.

## Dragon Writer Current Pack Shape

`examples/dragonwriter.ophelia.yml` now declares Dragon Writer as a critical portable app:

- `pack.portability: critical`
- `owner: personal`
- `host_requirements` for edge, Docker, memory, disk, and arch.
- `networking.internal: per-app`
- explicit Postgres data contract.
- critical uploads volume contract.
- required backups, restore drills, and offsite backup signal.
- allowlisted hook paths.
- explicit HTTP verification checks.

Pack validation currently warns that:

- Dragon Writer uses shared Postgres and needs rehearsed dump, restore, and verify receipts before cutover.
- The image is not digest-pinned.

Those are warnings, not parser errors.

## Production Readiness Interpretation

For a real Lumen VPS deployment, use this rough order:

```text
pack validate
env diff
backup status
app readiness
app export plan
app export create
copy bundle to target
app import plan on target
app import apply rehearsal
restore-drill plan
restore-drill apply
cutover plan
cutover apply checkpoint
traffic plan with target health
traffic apply checkpoint or provider execution
traffic rollback plan available from receipt
```

Do not proceed to traffic mutation until readiness blockers are clean or explicitly accepted by the operator.

Expected readiness blockers before a target runtime exists may include:

- Missing runtime env file.
- Missing required env keys.
- Missing active release metadata.
- Missing fresh backup metadata.
- Missing restore drill receipt.

Those are correct blockers. They should be resolved by the deployment workflow, not bypassed.

## Final Audit Fixes

The final audit pass fixed these concrete issues:

- `exact_apply_input.command` now shell-quotes all arguments.
- Direct Python API calls with malformed TTL, timeout, or expected HTTP status return structured JSON blockers instead of raising type errors.
- `caddy_provider=cloudflare` is rejected. Cloudflare is a DNS provider only.
- Cloudflare TTL validation blocks invalid TTLs before apply.
- Caddy validate/reload blocks if configured `sites_dir` does not match `<runtime_root>/caddy/sites.d`.
- Invalid Caddy timeout config is blocked.
- Target health URLs with credentials, query strings, or fragments are blocked.
- Cloudflare invalid JSON responses are converted into structured failed provider results.
- Readiness warnings now aggregate into stable `code`, `message`, and `path` fields instead of Python dict repr strings.
- File DNS receipts no longer put target host data into a misleading `app` field.

## Verification Completed

The final worktree passed:

```bash
PYTHON=python3 make docs-check
PYTHON=python3 make compile
PYTHON=python3 make test
PYTHON=python3 make validate-examples
git diff --check
```

Full test result:

```text
Ran 105 tests
OK
```

Additional smoke checks were run:

```bash
./cli/ship pack validate examples/dragonwriter.ophelia.yml --json
./cli/ship app readiness dragon-writer --environment production --manifest examples/dragonwriter.ophelia.yml --runtime-root /tmp/ophelia-audit-runtime --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --manifest examples/dragonwriter.ophelia.yml --runtime-root /tmp/ophelia-audit-runtime --json
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
```

The readiness and traffic smoke checks correctly returned blockers against the temporary runtime because no real Dragon Writer runtime, env, backups, release metadata, or restore drill receipts existed under `/tmp/ophelia-audit-runtime`.

## Remaining Risks

No production VPS state was exercised. Cloudflare and Caddy provider paths are covered by unit tests and mocks/local providers, but the deployment agent still needs real production provider config, real Cloudflare token env wiring, and operator-reviewed traffic plans before any live apply.

1. Issue: Live production Cloudflare and Caddy execution has not been run against the VPS.
   Proposed direction: run `traffic plan` first with production provider config, inspect blockers, checks, artifacts, and exact apply input, then perform a confirmed apply only after backup, restore drill, health, and rollback gates are clean.
   Why it matters: this is the first point where real DNS or Caddy can change.
   Impact: avoids accidental traffic movement or broken rollback.
   Risks/tradeoffs: requires correct provider config and token/env setup.
   Complexity: moderate operational rehearsal, low code complexity.

