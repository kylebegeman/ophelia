# Architecture

## Goals

- Put platform logic in one dedicated repo.
- Keep application code in application repos.
- Make runtime state reproducible from manifests and templates.
- Avoid VPS drift caused by hand-edited edge config.
- Make ingress host layout explicit enough to rehearse public and private
  routes with fixture manifests before any production cutover.

## Boundaries

### stack ownership

Ophelia is the source of truth for VPS app/runtime contracts. It owns manifests,
rendered Caddy/Compose/runtime files, host operations, backup and restore
contracts, rollback, runtime inspection, receipts, safety gates, and the JSON
surfaces that downstream tools consume.

Private or internal operator UIs can become cockpits over those contracts, but
they should not replace the host-side source of truth. Existing products and old
host services are legacy inventory until a separately approved adoption,
migration, or decommission phase moves them onto the current Ophelia contract.

### `ophelia` owns

- `ship` CLI
- shared infrastructure definitions
- platform-owned ingress manifests
- render templates
- migration and backup scripts
- operational docs

### app repos own

- application code
- Dockerfiles
- CI pipelines
- `.ophelia.yml` manifests

### the VPS owns

- generated per-app runtime bundles
- secrets and env files
- pulled container images
- Docker volumes
- logs and backups

## Checkout Locations

The canonical workstation checkout is:

```text
/path/to/ophelia
```

During migration, an older checkout path may still exist at:

```text
/path/to/old-ophelia
```

That older path is compatibility-only. Commands should be written repo-relative
and should not assume either absolute workstation path. The VPS source checkout
continues to live at `~/ophelia`, while generated runtime state continues to
live under `~/ophelia-runtime`.

## Runtime Shape

```text
~/ophelia-runtime/
  apps/
    demo-service/
      compose.yml
      env
      env.example
      manifest.lock.json
      release.json
      releases/
      caddy/
        demo-service.caddy
  caddy/
    env
    global.d/
    sites.d/
  static/
  backups/
  cache/
```

Platform-owned manifests live in `manifests/`. In the public repository they
are synthetic demo manifests. Private operators can maintain separate host
manifests for apps that do not belong to a single app repo yet, such as:

- static and redirect hosts
- production and staging tunnel ingress
- temporary ingress bridges during migration

## Networking

Manifest v2 uses three Docker network classes:

- `ophelia-edge`: Caddy to public app traffic
- `ophelia-<app>-<environment>-app`: revision-isolated communication inside one app
- `ophelia-data`: explicitly declared communication with separately deployed data services

Workloads join only their declared networks. A separately deployed stateful
service can expose a stable cross-app address on `ophelia-data` with
`network_aliases.data`. Stable aliases require `update.strategy: recreate`, so
the old revision is stopped before the new revision claims the same address:

```yaml
workloads:
  postgres:
    kind: internal
    artifact: postgres
    port: 5432
    networks: [data]
    network_aliases:
      data: [demo-service-postgres]
update:
  strategy: recreate
```

The shared Caddy service also maps `host.docker.internal` to Docker's
host-gateway address so tunnel-style manifests can proxy to host-published
services on Linux. For legacy app containers that only bind `127.0.0.1` inside
their own Compose project, attach the target container to `ophelia-edge` and
use a stable network alias instead of `host.docker.internal`.

## Deployment Model

Preferred flow:

1. app repo builds an image in CI
2. image is pushed to GHCR
3. `ship deploy --plan` explains generated files, image references, env
   requirements, route changes, and verification checks
4. confirmed `ship deploy --apply` writes release metadata and rendered bundle
   snapshots, pulls images, and updates the app on the VPS
5. Caddy reloads only after generated snippets are staged and validation is
   possible

For platform-owned ingress, Ophelia now also includes:

- `platform/scripts/sync-platform-static.sh`
- `platform/scripts/apply-manifests.sh`
- `platform/scripts/validate-caddy.sh`
- `platform/scripts/cutover-public-edge.sh`

Those scripts let the VPS reconcile platform manifests without hand-editing
public edge config.

Every deploy writes a latest `release.json` pointer plus immutable release
records under `apps/<app>/releases/`. Successful applies also write
`active_release.json`, which is the release Ophelia treats as active for
rollback, status, and generated support-file cleanup. New releases snapshot
rendered bundles under `apps/<app>/release-bundles/` so rollback can restore
generated Compose, Caddy, env fragment, and artifact files without deleting
operator-managed runtime state.

## Two Deployment Modes

### 1. Manual `ship` from an operator machine

This is the bootstrap path:

- use local Ophelia
- stage or apply a manifest over SSH
- use it for first deploys, migrations, and recovery

### 2. Automated deploys from app repositories

This is the steady-state path:

- the app repo owns its build pipeline
- CI publishes an artifact or image
- CI invokes `ship` on the VPS to apply the runtime config

That split keeps Ophelia as the platform control plane while each app repo owns
its own release lifecycle.

## Safety And Inspection

Ophelia does not expose arbitrary shell execution. Operational commands are
allowlisted and deterministic. Mutating flows are dry-run-first where risk is
material:

- production deploy apply requires a confirmation token from the matching plan
- rollback apply requires the rollback plan token
- backup create and restore preview apply require plan tokens
- restore apply creates a preview/report and does not overwrite active env,
  runtime files, or volumes

Read-only inspection is available through `ship status`, `ship doctor`,
`ship diff`, `ship drift`, `ship inspect conflicts`, and `ship explain`.

## Example Domain Layout

A public-safe rehearsal layout can use:

- `app.example.com`
- `api.example.com`
- `docs.example.com`
- `admin.example.com`
- `demo-static.example.com`
- `demo-service.example.com`

Docs and admin hosts are implemented generically through route-level exact-path
passthroughs plus host-level `rewrite_prefix` behavior, not through
product-specific Caddy hacks.
