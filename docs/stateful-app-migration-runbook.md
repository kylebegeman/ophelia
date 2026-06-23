# Stateful App Migration Runbook

Status: public template

This runbook describes how to migrate a stateful app through Ophelia without
using private host notes as public examples. It is intentionally conservative:
plan first, collect redacted evidence, rehearse restore/import paths, then cut
over only after explicit approval.

## Objectives

- Preserve production data, uploads, runtime env shape, release metadata, and
  rollback options.
- Prove backup and restore ability before final cutover.
- Keep the source host available during a defined retention window.
- Make the migration repeatable through Ophelia commands and receipts.

## Non-Objectives

- Do not delete source containers, images, volumes, backups, apps, DNS records,
  or env files during planning.
- Do not rewrite the app as part of the migration.
- Do not change public traffic until target verification is complete and
  approved.

## Required Source Inventory

Capture these as redacted receipts or evidence files:

- source host id, OS, Docker version, Compose version, and disk free
- active containers and image references
- active routes and Caddy snippets
- current env key names with values redacted
- Postgres database name, role, schema version, dump size, and latest backup time
- Redis usage, if any
- upload/static paths and total size
- current release id and active release metadata
- latest local backup and latest offsite backup marker
- public health endpoint status

## Required App Pack Shape

```yaml
pack:
  portability: critical
  owner: platform
  description: Demo service production app

networking:
  internal: per-app

data:
  postgres:
    mode: shared-postgres-database
    database: demo_service
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
    offsite:
      provider: restic
      target: s3://ophelia-fixture-backups/demo-service
      retention_days: 30

verify:
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

hooks:
  pre_export: ophelia/hooks/pre-export.sh
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh
```

## Pre-Migration Gates

Do not proceed to final cutover until all gates pass:

- source inventory receipt exists
- current source backup is valid
- offsite backup marker is current enough for the risk window
- source database dump passes listing validation
- upload/static archive can be listed without errors
- target host has enough free disk and memory
- target host has Caddy edge ready or a staging route available
- target import rehearsal has completed
- target health checks pass
- target data verification passes
- rollback plan is written
- DNS or route cutover plan is reviewed

## Read-Only Planning Commands

```bash
./cli/ship pack validate examples/service-app.ophelia.yml --json
./cli/ship pack explain examples/service-app.ophelia.yml --json
./cli/ship env diff demo-service --environment production --json
./cli/ship backup status demo-service --environment production --json
./cli/ship inspect conflicts --include-runtime --json
./cli/ship app readiness demo-service --environment production --json
./cli/ship app runbook demo-service --environment production
./cli/ship app export plan demo-service --environment production --json
./cli/ship backup rehearse plan ./exports/demo-service.production.export.tar --manifest .ophelia.yml --json
./cli/ship app restore-drill plan demo-service --environment production --json
./cli/ship app cutover plan demo-service --from source-host --to target-host --environment production --json
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --json
./cli/ship receipts list --app demo-service --json
```

These commands must not mutate production state.

## Confirmed Follow-Up Commands

Confirmed commands require tokens from matching plans:

```bash
./cli/ship app export create demo-service --environment production --confirm <token> --json
./cli/ship backup rehearse apply ./exports/demo-service.production.export.tar --manifest .ophelia.yml --confirm <token> --json
./cli/ship app import apply ./exports/demo-service.production.export.tar --confirm <token> --json
./cli/ship app restore-drill apply demo-service --environment production --source ./exports/demo-service.production.export.tar --confirm <token> --json
./cli/ship app cutover apply demo-service --from source-host --to target-host --environment production --confirm <token> --json
./cli/ship app traffic apply demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic rollback plan demo-service --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply demo-service --receipt <traffic-receipt-id> --environment production --confirm <token> --json
```

## Rehearsal Flow

```text
source inventory
  -> export plan
  -> export create
  -> copy export bundle to target
  -> import plan on target
  -> import apply into rehearsal namespace
  -> restore drill apply
  -> target verification
  -> cutover plan
  -> traffic plan
  -> approved traffic apply
  -> monitor
  -> source retention window
```

## Rollback Notes

Keep source runtime untouched until the retention window ends. Traffic rollback
should restore captured provider state from the traffic receipt when provider
execution was used. If provider execution was manual, the receipt should still
contain the prior and intended states for operator rollback.
