# Ophelia Next Architecture

Status: planning direction

Ophelia remains the deployment control plane for Kyle's VPS platform. The next
architecture should make Ophelia easier to use, safer for important data, and
portable across any current or future VPS without turning it into a second UI
product that competes with Lumen Ops.

The working goal is simple:

```text
buy or rebuild a VPS
  -> bootstrap Ophelia
  -> register host capabilities
  -> import or deploy portable app units
  -> verify health, data, routes, and backups
  -> cut traffic over with receipts and rollback notes
```

## Positioning

Ophelia is the deterministic host-side deploy/runtime substrate.

Lumen Ops is the eventual cockpit over that substrate. Lumen should read
inventory, show plans, collect approvals, and retain receipts. Ophelia should
continue to own the host contracts, app manifests, rendered runtime files,
export/import mechanics, Caddy validation, Docker orchestration, and migration
safety primitives.

This boundary keeps Ophelia useful now while allowing Lumen Ops to absorb the
operator experience later.

## Goals

- Keep app source code in app repositories.
- Keep generated runtime state, env files, secrets, volumes, logs, and backups on
  the VPS.
- Make every deployable app describable as a portable app unit.
- Make app movement between VPSs a planned export/import/cutover flow, not a
  bespoke SSH session.
- Treat data as first-class. Database dumps, uploads, object data, volume data,
  and restore checks are part of the app contract.
- Allow one host to run many unrelated apps without accidental shared state.
- Preserve one shared public edge per host while isolating app internals.
- Produce receipts for risky operations that Lumen Ops can ingest later.
- Prefer dry-run plans, confirmation tokens, and explicit cutover commands over
  implicit mutation.

## Non-Goals

- Do not replace Lumen Ops with an Ophelia UI.
- Do not make Ophelia a generic Kubernetes replacement.
- Do not require every app to be rewritten for a new framework.
- Do not make Lakebed a runtime dependency.
- Do not clean up old services, duplicate apps, or unused containers as part of
  this planning work.
- Do not silently move production data or DNS.

## Borrowed Ideas

Lakebed is useful as inspiration, not as the target runtime. The ideas worth
borrowing are:

- a capsule-shaped contract where the deployable unit is the whole app boundary
- CLI-first creation, deploy, inspect, logs, and database dump flows
- private-by-default hosted inspection
- a tiny committed deploy binding that identifies the same hosted app from any
  checkout
- agent-oriented instructions inside generated projects
- explicit current limits instead of magic behavior
- state and log inspection commands that are available before guessing

Ophelia should adapt those ideas to arbitrary Dockerized apps, VPS hosts,
Postgres, Redis, Caddy, uploads, static assets, and Lumen Ops receipts.

## Core Concepts

### Host

A host is a VPS or trusted machine that can run Ophelia-managed services. A host
has:

- stable host id
- provider metadata
- SSH identity and fingerprint
- OS, CPU, memory, disk, and network capabilities
- Docker and Compose readiness
- Caddy edge readiness
- app inventory
- backup inventory
- host-level receipt history

Host records should be readable without mutating the host.

### Foundation

The foundation is the small set of services shared by a host:

- public edge proxy
- TLS storage
- host inventory files
- backup/export staging area
- optional shared Postgres or Redis pools

The public edge is shared because ports `80` and `443` are host-level resources.
Everything behind the edge should be app-scoped unless it is explicitly declared
as shared infrastructure.

### Portable App Unit

A portable app unit is the complete deployable contract for an app environment.
It includes:

- manifest and rendered runtime bundle
- service images and image digests
- routes and domains
- env shape and secret refs
- volume declarations
- data ownership declarations
- backup, export, import, and restore commands
- health and smoke checks
- migration hooks
- release and rollback metadata
- target host requirements

See [Portable App Pack Spec](portable-app-pack-spec.md).

### Runtime Bundle

A runtime bundle is generated output under the host runtime root. It should be
reproducible from the app pack plus host profile, except for env files, secrets,
live volumes, logs, and backups.

Current Ophelia already writes generated Compose, Caddy, env templates, release
metadata, and release bundles. The next architecture should extend that model to
also track data contracts, export/import receipts, restore drill status, and
host movement readiness.

### Receipt

A receipt is a durable artifact produced by an Ophelia operation. Receipts should
be JSON first and human-readable when useful. They should avoid raw secret values.

Examples:

- deploy plan
- deploy apply
- backup create
- export create
- import preview
- import apply
- restore drill
- cutover plan
- cutover apply
- rollback apply
- Caddy validation
- DNS checklist or handoff

Lumen Ops can later read these receipts directly.

## Isolation Model

The default should be:

```text
shared host edge
  -> shared edge network
    -> per-app reverse proxy target
      -> per-app internal network
        -> per-app services
        -> per-app data stores or declared shared data pools
```

Use one shared edge network for Caddy to reach public services. Use a separate
internal network per app/environment so unrelated apps cannot casually talk to
each other or share aliases.

Current Ophelia keeps shared `ophelia-internal` compatibility for omitted
manifest networking config. Apps can opt into `networking.internal: per-app`,
which renders a private app/environment internal network while keeping
`ophelia-edge` for Caddy ingress. The target architecture is:

- `ophelia-edge` or `ophelia-edge-<host>` as the shared public edge network
- `<app>-<environment>-internal` as the private app network
- explicit shared foundation networks only for declared shared pools

## Data Ownership Modes

Data portability is the main gap to close.

Each app data dependency should declare one ownership mode:

| Mode | Meaning | Recommended Use |
| --- | --- | --- |
| `app-postgres` | App owns its own Postgres container and volume. | Critical apps that must move quickly. |
| `shared-postgres-database` | App owns a database and role inside shared Postgres. | Smaller apps, staging, or low-memory hosts. |
| `external-postgres` | App points at an external managed database. | Apps already using managed infrastructure. |
| `redis-logical-db` | App uses one logical DB in shared Redis. | Cache or non-critical state only. |
| `app-redis` | App owns its own Redis container and volume. | Critical Redis-backed queues or durable state. |
| `host-volume` | App owns a named Docker volume or host bind path. | Uploads, static state, generated artifacts. |
| `object-store` | App owns an object bucket or prefix. | Upload-heavy apps or future S3/B2-compatible storage. |

For critical personal data apps such as Dragon Writer, prefer `app-postgres` or
an equally strong `shared-postgres-database` export/import contract. If a shared
database pool is used, Ophelia must still know exactly how to dump, restore,
verify, and rehearse that one app database.

## App Movement Lifecycle

App movement should be explicit and reversible.

```text
inventory source
  -> export plan
  -> export create
  -> import plan on target
  -> import apply on target
  -> target health verification
  -> source freeze or read-only mode
  -> final delta export
  -> final import
  -> cutover plan
  -> cutover apply
  -> monitor
  -> source retention window
  -> source cleanup only after approval
```

Commands should be dry-run-first for every mutating step.

## Relationship With Existing Commands

Existing commands should keep working:

- `ship validate`
- `ship render`
- `ship deploy --plan`
- `ship deploy --apply`
- `ship verify`
- `ship releases`
- `ship rollback`
- `ship backup`
- `ship restore`
- `ship status`
- `ship doctor`
- `ship drift`

New command families should build on the same plan/apply pattern:

- `ship host inventory`
- `ship host bootstrap plan|apply`
- `ship pack validate`
- `ship pack explain`
- `ship app export plan|create`
- `ship app import plan|apply`
- `ship app restore-drill plan|apply`
- `ship app cutover plan|apply`
- `ship app move plan`

## Lumen Ops Integration

Ophelia should expose machine-readable outputs before Lumen Ops writes exist.

Minimum integration surface:

- JSON inventory for hosts, apps, services, routes, data dependencies, backups,
  and releases
- JSON operation plans with risk level, confirmation token, expected changes,
  affected services, and rollback notes
- JSON receipts for applied operations
- bounded command list for read-only and mutating operations
- secret refs instead of raw secret values

Lumen Ops should not call arbitrary shell commands on a host when an Ophelia
operation exists.

## Implementation Phases

### Phase 1: Contracts and Inventory

- Add portable app pack docs and schema.
- Add read-only host/app inventory outputs.
- Add data dependency declarations to manifests without breaking existing apps.
- Add pack validation and explain commands.
- Add tests for backwards-compatible manifest parsing.

Implemented foundation:

- manifests load optional `pack`, `host_requirements`, `data`, and `hooks`
  sections
- old `addons.postgres` and `addons.redis` declarations infer compatibility
  data contracts
- `ship pack validate` and `ship pack explain` report pack readiness
- `ship app export plan` emits read-only JSON export planning receipts
- `ship app export create` writes confirmed redacted metadata/runtime bundles,
  declared local data archives, optional allowlisted Postgres dumps, checksums,
  and receipts
- `ship app import plan` emits read-only JSON import planning receipts from a
  bundle directory, metadata JSON, or readable tar bundle
- `ship app import apply` writes isolated rehearsal preview artifacts and
  receipts without changing active runtime
- `ship app restore-drill plan|apply` records export artifact/listability drill
  plans and receipts
- `ship app cutover plan|apply` records cutover readiness and checkpoint
  receipts without mutating Caddy or DNS
- `networking.internal: per-app` renders app/environment private internals when
  explicitly set

### Phase 2: Export and Restore Drills

- Expand Postgres restore validation beyond artifact/listability checks.
- Add optional service start verification for restore drills in explicitly
  isolated targets.
- Keep destructive restore disabled until preview and verification are reliable.

### Phase 3: Import and Target Bootstrap

- Add target host capability checks.
- Add target health verification and data verification hook execution in
  bounded rehearsal targets.

### Phase 4: Cutover

- Add freeze/read-only hooks.
- Add final export/import flow.
- Add Caddy route switch plans.
- Add DNS checklist output for providers that are not yet native.
- Add source retention receipts and rollback notes.

### Phase 5: Lumen Ops Adapter

- Let Lumen Ops consume Ophelia inventory, plans, receipts, and operation
  descriptors.
- Keep Ophelia as the host-side deterministic executor.
- Keep approvals and human-facing orchestration in Lumen Ops.

## Safety Rules

- No command deletes source data during migration.
- No command mutates production without a fresh plan token.
- No command prints raw env values or secret material in reports.
- Every import has a preview.
- Every cutover has a rollback or recovery note.
- Every database-backed app has a restore drill before production movement.
- Cleanup of duplicate or old apps is a separate approved operation.

## Open Decisions

- Whether production critical apps should default to per-app Postgres containers
  despite higher memory use.
- Whether runtime roots should remain `apps/<app>` or move to
  `apps/<app>/<environment>` with a compatibility alias.
- Whether Ophelia should ship a tiny host agent/API server or stay SSH/CLI-first
  until Lumen Ops needs a persistent endpoint.
- Which secret-ref store should become canonical before Lumen Ops Secret Vault is
  ready.
- Whether object storage should be implemented first as local archives, B2/S3
  sync, or both.
