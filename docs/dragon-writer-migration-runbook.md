# Dragon Writer Migration Runbook

Status: planning runbook

Dragon Writer is the highest-priority app currently associated with the
Spaceship VPS. The goal is to migrate it safely, not redeploy it from scratch.
The migration should preserve application data, uploaded files, runtime env
shape, release metadata, and rollback options.

This runbook is intentionally conservative. It is a plan for later execution,
not approval to mutate any host.

## Objectives

- Move Dragon Writer off Spaceship before the Spaceship VPS is retired.
- Preserve all production data.
- Prove restore ability before final cutover.
- Keep the source host available as a rollback/reference during a retention
  window.
- Avoid coupling Dragon Writer data to Lumen data.
- Make the migration repeatable through Ophelia commands.

## Non-Objectives

- Do not delete Pingstation, old apps, duplicate containers, or stale images.
- Do not clean up Spaceship during the migration planning phase.
- Do not rewrite Dragon Writer.
- Do not depend on Lumen Ops being fully built before Dragon Writer can move.
- Do not change DNS until target verification is complete and approved.

## Target Placement

Dragon Writer is personal/family infrastructure, not a business SaaS product.
The expected target is the personal cloud/Lumen host, currently the OVH VPS,
unless a later host decision changes that.

Dragon Writer should remain isolated from Lumen:

- separate Compose project
- separate internal Docker network
- separate Postgres ownership or explicit database contract
- separate upload/static volumes
- separate env and secret refs
- shared Caddy edge only

## Required Source Inventory

Before migration, capture:

- source host id, OS, Docker version, Compose version, and disk free
- active Dragon Writer containers
- active image references and image digests where available
- active routes and Caddy snippets
- current env keys with values redacted
- Postgres database name, role, schema version, dump size, and latest backup time
- Redis usage, if any
- upload/static paths and total size
- current release id and active release metadata
- latest local backup and latest offsite backup marker
- public health endpoint status
- staging health status, if staging is relevant

The inventory output should be a receipt, not loose notes.

## Required App Pack Work

Dragon Writer needs an explicit portable app pack before migration:

```yaml
pack:
  portability: critical
  owner: personal
  description: Dragon Writer production app

data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
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

If Dragon Writer moves to app-owned Postgres later, change the mode to
`app-postgres` and add an explicit Postgres volume declaration. The export,
import, and verification contract must remain explicit either way.

## Pre-Migration Gates

Do not proceed to final cutover until all gates pass:

- source inventory receipt exists
- current source backup is valid
- offsite backup marker is current enough for the risk window
- source database dump passes `pg_restore -l` or equivalent listing validation
- upload/static archive can be listed without errors
- target host has enough free disk and memory
- target host has Caddy edge ready or a staging route available
- target import rehearsal has completed
- target health checks pass
- target data verification passes
- rollback plan is written
- DNS or route cutover plan is reviewed

Current read-only Ophelia checks:

```bash
./cli/ship pack validate examples/dragonwriter.ophelia.yml --json
./cli/ship pack explain examples/dragonwriter.ophelia.yml --json
./cli/ship env diff dragon-writer --environment production --json
./cli/ship backup status dragon-writer --environment production --json
./cli/ship inspect conflicts --include-runtime --json
./cli/ship app readiness dragon-writer --environment production --json
./cli/ship app runbook dragon-writer --environment production
./cli/ship app export plan dragon-writer --environment production --json
./cli/ship app restore-drill plan dragon-writer --environment production --json
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh --environment production --json
./cli/ship receipts list --app dragon-writer --json
```

These commands must not mutate production state. Export create, import apply,
restore drill apply, cutover apply, and DNS changes remain separate confirmed
future steps.

## Rehearsal Flow

```text
source inventory
  -> export plan
  -> export create
  -> copy export bundle to target
  -> import plan on target
  -> import apply into rehearsal namespace
  -> start target app on staging route
  -> run health checks
  -> run data verification
  -> write rehearsal receipt
```

The rehearsal must not alter production DNS.

## Final Cutover Flow

```text
lower DNS TTL if needed
  -> confirm source and target receipts
  -> freeze source or put it in read-only mode
  -> create final database dump
  -> create final upload/static archive or delta
  -> transfer final export bundle
  -> import final bundle on target
  -> start target production stack
  -> run target health checks
  -> run target data verification
  -> switch Caddy route or DNS
  -> verify public endpoint
  -> keep source frozen/read-only during retention window
  -> unfreeze source only if rollback is required
```

If Dragon Writer has no application-level read-only mode, the fallback is a short
maintenance window where writes are stopped by stopping the source app after the
final export begins.

## Rollback Strategy

Before cutover:

- rollback is canceling the target import and leaving source production in place

After cutover:

- if target health fails immediately, switch the route back to source and
  unfreeze source
- if target data verification fails, switch the route back to source and preserve
  failed target import artifacts for inspection
- if target passes but later problems appear, use the source retention window as
  recovery while deciding whether to re-run final export or repair target

Do not delete source data during the retention window.

## Post-Cutover

After the retention window:

- verify backups are running on the new host
- verify offsite backups are current
- verify source host no longer receives writes
- record final migration receipt
- only then plan Spaceship cleanup as a separate operation

## Ophelia Features Needed Before Execution

- explicit data contracts in manifests
- source inventory receipt
- Postgres dump create and validation
- volume/archive create and validation
- export bundle with checksums
- import preview
- import apply into rehearsal namespace
- restore drill receipt
- cutover plan and apply receipts
- route/DNS checklist

## Manual Fallback

If Ophelia support is not complete in time, the same lifecycle still applies.
The fallback should be a scripted and logged manual run with:

- explicit source freeze
- `pg_dump` or equivalent database export
- archive of uploads/static data
- checksum files
- target restore
- target health verification
- target data verification
- route/DNS cutover
- rollback notes

The fallback should still produce artifacts that Ophelia can ingest later.
