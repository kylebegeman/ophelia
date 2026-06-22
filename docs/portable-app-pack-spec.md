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
- The same pack can target Spaceship, Hostinger, OVH, or a future host when host
  capabilities match.

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
app: dragon-writer
environment: production
kind: service

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
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh

verify:
  - name: health
    url: https://dragonwriter.begam.in/health
  - name: home
    url: https://dragonwriter.begam.in/
    expect_status: 200
```

`data.volumes[].mount` is both a portability contract and a runtime contract.
For single-service manifests, Ophelia mounts the volume into that service. For
multi-service manifests, set `data.volumes[].service` so the runtime target is
unambiguous. When `source` is omitted, Ophelia renders an app/environment-scoped
Docker named volume; when `source` is present, it is rendered as the host or
Compose-relative source path.

## Field Reference

### `pack`

Pack metadata used by inventory, Lumen Ops, and migration plans.

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
    dragon-writer/
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
    dragon-writer/
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
dragon-writer.production.export.<timestamp>.tar.zst
  manifest.json
  checksums.sha256
  source-host.json
  runtime/
    manifest.lock.json
    release.json
    active_release.json
    compose.yml
    caddy/
  data/
    postgres/
      dragon_writer.dump
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
default should be secret refs and redacted env shape.

## Commands

### Pack Commands

```bash
./cli/ship pack validate path/to/.ophelia.yml
./cli/ship pack explain path/to/.ophelia.yml
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
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
./cli/ship env diff dragon-writer --environment production --json
./cli/ship backup status dragon-writer --environment production --json
./cli/ship inspect conflicts --include-runtime --json
./cli/ship app readiness dragon-writer --environment production --json
./cli/ship app runbook dragon-writer --environment production
./cli/ship receipts list --app dragon-writer --json
```

These commands are read-only. They report redacted env shape, backup freshness,
route/domain ownership, portability score, generated runbook text, and existing
operation receipts.

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

##### Dragon Writer example (abbreviated)

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
          "ship app restore-drill plan dragon-writer --environment production --json",
          "ship app export plan dragon-writer --environment production --json"
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
      "command": "ship app restore-drill plan dragon-writer --environment production --json",
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
./cli/ship app export plan dragon-writer --environment production
./cli/ship app export create dragon-writer --environment production --confirm <token>
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
./cli/ship app import plan ./exports/dragon-writer.production.export.tar.zst
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
./cli/ship app restore-drill plan dragon-writer --environment production
./cli/ship app restore-drill apply dragon-writer --environment production --confirm <token>
```

A restore drill proves the exported artifacts are usable without damaging the
live app. The current apply form validates bundle/artifact readability and writes
a restore-drill receipt. It does not restore over production or start services.

### Cutover Commands

```bash
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh
./cli/ship app cutover apply dragon-writer --from spaceship --to ovh --confirm <token>
```

Cutover apply should only run after successful import and verification on the
target host. The current apply form writes a confirmed cutover checkpoint
receipt and rollback note. It does not mutate Caddy, DNS, or source retention
state.

### Traffic Automation Commands

```bash
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider file --caddy-provider file --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider cloudflare --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic rollback plan dragon-writer --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply dragon-writer --receipt <traffic-receipt-id> --environment production --confirm <token> --json
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
./cli/ship app isolation plan dragon-writer --environment production --json
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
- Whether Lumen Ops Secret Vault should become the only secret-ref provider.
- Whether app-owned Postgres should become the default for `portability:
  critical`.
