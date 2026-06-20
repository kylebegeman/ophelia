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
```

`pack init` is preview-only unless `--write` is passed. It refuses to overwrite
existing scaffold files unless `--force` is also passed.

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
```

Traffic plan is the first production traffic automation contract. It records the
domains to move, DNS record intent, Caddy route intent, readiness gates,
provider labels, TTL, rollback notes, and exact apply input. The current apply
form writes a confirmed traffic checkpoint receipt only. It does not mutate DNS
or Caddy until an explicit provider backend is implemented and tested.

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
