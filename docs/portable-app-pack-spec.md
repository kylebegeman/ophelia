# Portable App Pack Spec

Status: draft, with Phase 1 manifest support, pack validation/explain,
readiness reports, receipt browsing, export plan, and import plan implemented.

A portable app pack is the Ophelia contract that makes one app environment
deployable, inspectable, exportable, importable, and movable between hosts.

The pack is not the app source code. It is the deployment and data contract that
lets Ophelia safely operate the app.

## Design Principles

- The app repository owns the app pack.
- The VPS owns runtime state, secrets, volumes, logs, backups, and pulled images.
- Generated runtime files are reproducible.
- Live data is never implied. It must be declared.
- Every important app can prove restore readiness before migration.
- Host-specific values are either rendered from a host profile or materialized
  from secret refs.
- The same pack can target a source host, a target host, or a future host when
  host capabilities match.

## Repository Shape

Preferred app repository shape:

```text
app-repo/
  .ophelia.yml
  ophelia/
    runbook.md
    agent.md
    checks/
      smoke.sh
      data-verify.sh
    hooks/
      freeze.sh
      unfreeze.sh
      post-import.sh
```

`.ophelia.yml` remains the primary manifest. The optional `ophelia/` directory
contains app-specific operator notes, agent instructions, checks, and hooks.

For platform-owned apps that do not yet have their own repo manifest, Ophelia can
continue to hold manifests under `manifests/`. The long-term goal is to promote
each real app into an app-repo-owned pack.

## Manifest Additions

The existing manifest should stay backwards compatible. Add these sections
incrementally.

```yaml
version: 1
app: demo-service
environment: production
kind: service

pack:
  portability: critical
  owner: personal
  description: Demo Service production app
  deploy_binding_file: ophelia/deploy.json

host_requirements:
  arch: amd64
  min_memory: 1g
  min_disk_free: 20g
  requires_edge: true
  requires_docker: true

networking:
  internal: per-app

data:
  postgres:
    mode: shared-postgres-database
    database: demo_service
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
    offsite:
      provider: restic
      target: s3://ophelia-fixture-backups/demo-service
      retention_days: 30
      encryption_required: true
      restore_rehearsal_cadence_days: 30
      last_rehearsal_ref: restore-drills/latest.json

hooks:
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh

verify:
  - name: health
    url: https://demo-service.example.com/health
  - name: home
    url: https://demo-service.example.com/
    expect_status: 200
  - name: internal-runtime-health
    type: command
    service: web
    command:
      - npm
      - run
      - ophelia:health
      - --
      - --json
    expect_json:
      status: ok
```

`data.volumes[].mount` is both a portability contract and a runtime contract.
For single-service manifests, Ophelia mounts the volume into that service. For
multi-service manifests, set `data.volumes[].service` so the runtime target is
unambiguous. When `source` is omitted, Ophelia renders an app/environment-scoped
Docker named volume; when `source` is present, it is rendered as the host or
Compose-relative source path. Bare relative sources such as `uploads` render as
`./uploads` so Compose treats them as bind paths, not named volumes. Export
create archives declared host sources directly. For app-owned Docker named volumes, export create uses a local helper
image to mount the volume read-only and write the archive into the export
bundle without printing data.

Top-level `verify` supports public HTTP checks, internal service checks, and
internal command checks. Use `service: app` plus `path: /ophelia/health` for the
standard app-owned health endpoint, or `type: command` for app-owned npm
scripts that should run with `docker compose exec -T <service> ...`. HTTP,
internal, and command checks can declare `expect_json` or `json_assertions` for
stable app-owned JSON fields.

When `data.backups.offsite_required: true`, declare the concrete offsite target:
`data.backups.offsite.provider`, `target`, `retention_days`,
`encryption_required`, `restore_rehearsal_cadence_days`, and
`last_rehearsal_ref`. Pack validation warns with
`offsite_backup_target_missing` and lists missing fields until all are set.

## Field Reference

### `pack`

Pack metadata used by inventory, operator-console views, and migration plans.

| Field | Required | Meaning |
| --- | --- | --- |
| `portability` | no | `critical`, `standard`, or `static`. |
| `owner` | no | `personal`, `business`, `platform`, or a custom owner label. |
| `description` | no | Human summary for inventory. |
| `deploy_binding_file` | no | Small committed app binding file, if used. |

### `host_requirements`

Target host requirements checked before import or deploy.

| Field | Meaning |
| --- | --- |
| `arch` | CPU architecture requirement, such as `amd64` or `arm64`. |
| `min_memory` | Minimum memory for the app and declared data services. |
| `min_disk_free` | Minimum free disk before import. |
| `requires_edge` | Whether the host must have Ophelia edge ready. |
| `requires_docker` | Whether Docker and Compose are required. |

### `data`

All state that must survive app movement.

Supported sections:

- `postgres`
- `redis`
- `volumes`
- `object_storage`
- `static_assets`
- `external_services`
- `backups`

Every `critical` data item must define export, import, and verify behavior before
Ophelia should allow production cutover.

### `hooks`

Optional app-specific scripts or commands. Hooks must be allowlisted by path,
run with bounded environment variables, and record stdout/stderr as artifacts.

| Hook | Purpose |
| --- | --- |
| `pre_export` | Check app readiness before export. |
| `freeze` | Put the app in maintenance or read-only mode. |
| `unfreeze` | Return the source app to normal service if cutover is canceled. |
| `post_import` | Run target-side migrations or cache warmup. |
| `post_cutover` | Run final checks after traffic moves. |

## Runtime Root Shape

Current shape:

```text
~/ophelia-runtime/
  apps/
    demo-service/
      compose.yml
      env
      manifest.lock.json
      release.json
      active_release.json
      releases/
      release-bundles/
      caddy/
```

Target-compatible shape:

```text
~/ophelia-runtime/
  apps/
    demo-service/
      production/
        compose.yml
        env
        manifest.lock.json
        data-contract.json
        release.json
        active_release.json
        releases/
        release-bundles/
        export-bundles/
        import-previews/
        restore-drills/
        receipts/
        caddy/
```

If Ophelia moves to the target shape, production apps can keep a compatibility
alias at `apps/<app>` until older commands are migrated.

## Export Bundle Shape

An export bundle is an immutable artifact that can be copied to another host.

```text
demo-service.production.export.<timestamp>.tar.zst
  manifest.json
  checksums.sha256
  source-host.json
  runtime/
    manifest.lock.json
    release.json
    active_release.json
    compose.yml
    caddy/
    ophelia/
  data/
    postgres/
      demo_service.dump
      metadata.json
    volumes/
      uploads.tar.zst
      metadata.json
  receipts/
    export-plan.json
    export-create.json
```

Export bundles should not include raw secret values unless the command is
explicitly configured to create a sealed operator-only recovery artifact. The
default should be secret refs and redacted env shape. `ship backup status`
treats successful, complete export bundles as fresh backup evidence; incomplete
data exports do not satisfy the backup freshness gate.

## Commands

### Pack Commands

```bash
./cli/ship pack validate path/to/.ophelia.yml
./cli/ship pack explain path/to/.ophelia.yml
./cli/ship pack init --app demo-service --environment production --critical --postgres --uploads --json
./cli/ship pack init --app demo-service --environment staging --directory ../demo-service --include-manifest --kind service --domain demo-service.example.com --image ghcr.io/example/demo-service:latest --json
./cli/ship pack init --app demo-static --environment staging --directory ../demo-static --include-manifest --kind static --domain demo-static.example.com --json
```

`pack init` is preview-only unless `--write` is passed. It refuses to overwrite
existing scaffold files unless `--force` is also passed. Generated hook and
check scripts are written executable.

With `--include-manifest`, `pack init` also plans `.ophelia.yml`. Service
manifests require `--domain` and `--image`; static manifests require
`--domain` and write a minimal static asset root (`public/` by default). The
same preview, `--write`, and `--force` overwrite policy applies to the manifest
and static placeholder files.

`pack validate --json` and `pack explain --json` redact data-contract command
metadata before output. Secret-shaped flags, key/value arguments, and
credential URLs are masked while preserving enough command shape for review.

### Readiness Commands

```bash
./cli/ship env diff demo-service --environment production --json
./cli/ship backup status demo-service --environment production --json
./cli/ship inspect conflicts --include-runtime --json
./cli/ship app readiness demo-service --environment production --json
./cli/ship app runbook demo-service --environment production
./cli/ship receipts list --app demo-service --json
```

These commands are read-only. They report redacted env shape, backup freshness,
route/domain ownership, portability score, generated runbook text, and existing
operation receipts.

### Backup Rehearsal Commands

```bash
./cli/ship backup rehearse plan ./exports/demo-service.production.export.tar --manifest .ophelia.yml --json
./cli/ship backup rehearse apply ./exports/demo-service.production.export.tar --manifest .ophelia.yml --confirm <token> --json
./cli/ship backup rehearse ./exports/demo-service.production.export.tar --manifest .ophelia.staging.yml --environment staging --json
```

`backup rehearse` is the app-manifest-facing rehearsal flow for a concrete
export bundle directory, `.tar`, or `.tar.zst`. Plan is read-only. Apply is
confirmation-gated. The direct one-shot form plans and applies with the current
plan token internally. Both apply paths write only under the app restore-drills
directory, safely extract archives into isolated paths, and run declared
`data.volumes[].verify.command` hooks against extracted volume data.

Verifier commands should be read-only and deterministic. They can use `{path}`,
`{data_path}`, or `{volume_path}` to receive the extracted volume path; if no
placeholder is present, Ophelia appends the path as the final argument.

### Fresh Install For Non-live Apps

```bash
./cli/ship app fresh-install plan demo-service --environment staging --manifest .ophelia.staging.yml --json
./cli/ship app fresh-install apply demo-service --environment staging --manifest .ophelia.staging.yml --confirm <token> --json
```

Fresh install is destructive and only applies to declared `data.volumes`
targets. It is allowed when the manifest has `lifecycle.live: false` and
`lifecycle.data_can_be_reset: true`, or when the operator passes both
`--non-live` and `--fresh-start-allowed`. Apply creates a pre-reset export by
default, unless `--skip-pre-reset-export` is explicit, then resets declared
host-path or Docker named volumes and runs post-reset verification.

#### Readiness Report Fields

`./cli/ship app readiness <app> --json` returns the report envelope with the
existing keys (`readiness_level`, `portability_score`, `blockers`, `warnings`,
`checks`, `env_shape`, `backup_status`, `route_conflicts`, `release`,
`restore_drill_receipts`, ...) plus these additive fields:

- `blockers[]` / `warnings[]` entries are enriched with an optional
  `remediation` object when the finding `code` is known. The remediation keeps
  the original `code`, `message`, and `path`, and adds `summary`, typed
  `commands` (always Ophelia `ship ...` commands, never raw shell),
  `docs`, `requires_human_approval`, and an optional `manifest_patch_hint`.
- `next_actions[]`: a priority-sorted list (blockers before warnings, then
  aggregation order) of `{code, area, command, summary}` derived from the first
  command of each finding's remediation. Findings without a remediation are
  skipped.
- `score_details`: a per-category roll-up of the portability score keyed by
  factor category (`runtime`, `data`, `secrets`, `backup`, `restore`). Each
  entry is `{points, max_points, ok_factors, total_factors, reason}`. The sum of
  every category's `points` equals `portability_score.score`, and the sum of
  every `max_points` equals 100.
- `source_reports`: compact, redaction-safe pointers to the sub-reports the
  aggregator consumed: `env_shape`, `backup_status`, `route_conflicts`,
  `secrets_audit` (computed via the shared `secrets_audit` module, names only),
  and `restore_drill`. Each pointer carries status and counts only; values stay
  redacted.

The readiness score never hides blockers: when any blocker exists,
`readiness_level` is `blocked` regardless of how high the score is.

`./cli/ship pack validate <manifest> --json` and `pack explain` also expose a
manifest-only `score_details` roll-up. Because pack validation cannot see
runtime env, backup, or restore state, it scores only the manifest-derived
factors it can assess (pack metadata, explicit data contracts, image digests,
and whether validation passed); it does not invent the runtime factors.

##### Demo Service example (abbreviated)

```json
{
  "readiness_level": "blocked",
  "portability_score": {"score": 65, "level": "blocked", "factors": [/* ... */]},
  "blockers": [
    {
      "code": "restore_drill_missing",
      "message": "No restore drill receipt is recorded.",
      "path": "restore-drills",
      "remediation": {
        "summary": "Plan a restore drill from the latest export bundle, then run it to record a receipt.",
        "commands": [
          "ship app restore-drill plan demo-service --environment production --json",
          "ship app export plan demo-service --environment production --json"
        ],
        "docs": ["docs/portable-app-pack-spec.md"],
        "requires_human_approval": true
      }
    }
  ],
  "next_actions": [
    {
      "code": "restore_drill_missing",
      "area": "blocker",
      "command": "ship app restore-drill plan demo-service --environment production --json",
      "summary": "Plan a restore drill from the latest export bundle, then run it to record a receipt."
    }
  ],
  "score_details": {
    "runtime": {"points": 30, "max_points": 30, "reason": "runtime: 3/3 factor(s) ok, 30/30 point(s) earned."},
    "data": {"points": 20, "max_points": 20, "reason": "data: 1/1 factor(s) ok, 20/20 point(s) earned."},
    "secrets": {"points": 15, "max_points": 15, "reason": "secrets: 1/1 factor(s) ok, 15/15 point(s) earned."},
    "backup": {"points": 0, "max_points": 20, "reason": "backup: 0/1 factor(s) ok, 0/20 point(s) earned."},
    "restore": {"points": 0, "max_points": 15, "reason": "restore: 0/1 factor(s) ok, 0/15 point(s) earned."}
  },
  "source_reports": {
    "env_shape": {"status": "ok", "key_count": 3, "required_missing": 0, "values_redacted": true},
    "backup_status": {"status": "ok", "freshness": "fresh", "backup_count": 1},
    "route_conflicts": {"status": "ok", "conflict_count": 0},
    "secrets_audit": {"status": "ok", "key_count": 3, "missing_required": 0, "values_redacted": true},
    "restore_drill": {"receipt_count": 0, "status": "missing"}
  }
}
```

### Export Commands

```bash
./cli/ship app export plan demo-service --environment production
./cli/ship app export create demo-service --environment production --confirm <token>
```

The export plan is implemented and read-only. It reports:

- source host id
- app release id
- data dependencies
- estimated export size
- blockers
- warnings
- commands to be run
- artifact paths
- restore drill requirement status

Export create writes a bundle directory and deterministic `.tar` archive
containing `manifest.json`, `source-host.json`, sanitized runtime files,
checksums, JSON receipts, and local static/volume archives when the pack declares
local source paths. When the local host has `zstd`, it also writes the planned
`.tar.zst` archive; otherwise the `.tar` archive remains the readable fallback.
`--include-postgres` enables the allowlisted read-only `pg_dump` path. It does
not run Redis exports, mutate Caddy, run Docker operations, SSH, or change DNS.

### Import Commands

```bash
./cli/ship app import plan ./exports/demo-service.production.export.tar.zst
```

The import plan is implemented and read-only. It accepts an export bundle
directory, metadata JSON, `.tar`, or `.tar.zst` when local tar/zstd support can
read metadata from it. It reports:

- target host id
- host capability match
- required Docker networks and volumes
- Caddy route changes
- data restore commands
- env and secret refs needed
- checks to run after import
- whether the import is rehearsal or cutover

Import apply is implemented for rehearsal previews. It requires the confirmation
token from a matching import plan and writes `import-previews/<id>/` with the
plan, source artifact inventory, required env shape, restore command plan, and
receipts. It does not restore data into production, write active env files,
start containers, or change Caddy/DNS.

### Restore Drill Commands

```bash
./cli/ship app restore-drill plan demo-service --environment production
./cli/ship app restore-drill apply demo-service --environment production --confirm <token>
```

A restore drill proves the exported artifacts are usable without damaging the
live app. Apply validates bundle/artifact readability, safely extracts artifact
contents into an isolated drill directory, runs declared app-owned volume
verifier commands, and writes a restore-drill receipt. It does not restore over
production or start services.

### Cutover Commands

```bash
./cli/ship app cutover plan demo-service --from source-host --to target-host
./cli/ship app cutover apply demo-service --from source-host --to target-host --confirm <token>
```

Cutover apply should only run after successful import and verification on the
target host. The current apply form writes a confirmed cutover checkpoint
receipt and rollback note. It does not mutate Caddy, DNS, or source retention
state.

### Traffic Automation Commands

```bash
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --json
./cli/ship app traffic apply demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --target-health-url https://demo-service-target.example.net/health --run-target-health --json
./cli/ship app traffic apply demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --target-health-url https://demo-service-target.example.net/health --run-target-health --confirm <token> --json
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --dns-provider file --caddy-provider file --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --dns-provider cloudflare --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic rollback plan demo-service --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply demo-service --receipt <traffic-receipt-id> --environment production --confirm <token> --json
```

Traffic plan is the first production traffic automation contract. It records the
domains to move, DNS record intent, Caddy route intent, readiness gates,
provider labels, TTL, rollback notes, and exact apply input. Apply is
checkpoint-only unless provider execution was planned with
`--execute-provider-mutation`.

Target health checks are optional but first-class. Provide
`--target-health-url` and `--run-target-health` on the plan; apply must use the
same inputs, and the confirmation token covers them. Health URLs must be
http(s), cannot contain credentials, query strings, or fragments, and can set
timeout/expected status with `--target-health-timeout` and
`--target-health-expect-status`.

The implemented provider backend is `file`, intended for production rehearsal,
local control planes, and testable handoff to external automation. It does not
call external DNS APIs, and Caddy reload is disabled unless the provider config
sets the explicit reload gate. Provider execution requires:

- the confirmation token from the matching traffic plan
- `--execute-provider-mutation` on both plan and apply
- `--provider-config <path>`
- at least one non-manual provider
- `allow_mutation: true` in the selected provider config section

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

File DNS writes a JSON record set with previous and next values in the receipt.
File Caddy writes a staged `<app>.<environment>.traffic.caddy` site file and
snapshots the previous file into the traffic receipt directory when one exists.
File Caddy can also validate or reload the shared Caddy process through
Ophelia's typed Caddy manager, but only when the provider config explicitly
requests it. Validation or reload requires `sites_dir` to match
`<runtime_root>/caddy/sites.d` so Ophelia validates and reloads the same site
directory it writes. Reload requires both `reload: true` and
`allow_reload: true`:

```json
{
  "caddy": {
    "provider": "file",
    "sites_dir": "/opt/ophelia-runtime/caddy/sites.d",
    "runtime_root": "/opt/ophelia-runtime",
    "ophelia_root": "/path/to/ophelia",
    "allow_mutation": true,
    "validate": true,
    "reload": true,
    "allow_reload": true,
    "timeout": 30
  }
}
```

Cloudflare DNS provider config uses a token environment variable reference, not
a token value. Apply lists the matching DNS record, patches exactly one existing
record, and records previous/next metadata in the receipt. It never deletes DNS
records. If no matching record exists, apply fails unless `allow_create: true`
is set. TTL must be `1` for Cloudflare automatic TTL or between `30` and
`86400` seconds.

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

Traffic rollback is also dry-run-first. `traffic rollback plan` reads a traffic
apply receipt and plans restoration of previous provider state. Rollback apply
refuses cases that would require deleting a DNS record or Caddy file that did
not previously exist; it only restores known previous DNS records and previous
Caddy file snapshots captured by the apply receipt.

### Isolation Commands

```bash
./cli/ship app isolation plan demo-service --environment production --json
```

The isolation plan reports the current shared-network compatibility mode and
the target per-app network shape. The plan is read-only. Compose rendering uses
the private `<app>-<environment>-internal` network only when the manifest sets
`networking.internal: per-app`; omitted manifests keep the shared
`ophelia-internal` compatibility network.

## Data Verification

Every critical app should define data checks that can run on source and target.

Examples:

- count key database tables
- verify latest important records exist
- verify uploads directory has expected file count and size
- run app-level read-only health endpoint
- run a smoke path with an authenticated test account where appropriate

Verification should avoid printing private user data.

## Backwards Compatibility

Existing manifests with `addons.postgres: true` and `addons.redis: true` should
continue to work. Ophelia can infer a default data contract:

```yaml
data:
  postgres:
    mode: shared-postgres-database
    inferred_from_addon: true
  redis:
    mode: redis-logical-db
    inferred_from_addon: true
```

Inferred contracts are enough for deploy, status, and backup warnings. They are
not enough for production movement of critical apps until export, import, and
verify details are explicit.

## Validation Rules

Pack validation should fail when:

- a critical data item lacks export or import behavior
- a production app has no health verification
- a production app declares a writable host bind mount without backup behavior
- a route uses a domain already owned by another active manifest
- a service has no image after manifest defaults are resolved
- an app-postgres data item has no volume declaration
- a destructive hook path is outside the app pack allowlist

Pack validation should warn when:

- a shared Postgres database is used by a critical app
- Redis is declared as durable state but uses a shared logical DB
- no restore drill has succeeded recently
- no offsite backup is recorded
- production uses an image tag without a digest

## Open Questions

- Whether `pack` metadata should live in `.ophelia.yml` or a separate
  `ophelia/pack.yml`.
- Whether export bundles should support encrypted env payloads in addition to
  secret refs.
- Whether a private operator UI secret vault should become the only secret-ref provider.
- Whether app-owned Postgres should become the default for `portability:
  critical`.
