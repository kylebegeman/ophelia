# Architecture

## Goals

- Put platform logic in one dedicated repo.
- Keep application code in application repos.
- Make runtime state reproducible from manifests and templates.
- Avoid VPS drift caused by hand-edited edge config.
- Make ingress host layout explicit enough to rehearse `bagels.top` before mirroring it onto `aspectavy.com`.

## Boundaries

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

## Runtime Shape

```text
~/ophelia-runtime/
  apps/
    dragon-writer/
      compose.yml
      env
      env.example
      manifest.lock.json
      release.json
      caddy/
        dragon-writer.caddy
  caddy/
    sites.d/
  static/
  backups/
  cache/
```

Platform-owned manifests live in `manifests/` and are intended for hosts that
do not belong to a single app repo yet, such as:

- the `bagels.top` static and redirect hosts
- AspectAvy production and staging tunnel ingress
- the static `dev.bagels.top` preview host
- temporary legacy-ingress bridges like `dev.pokedex.begam.in`

## Networking

Use two Docker networks:

- `ophelia-edge`: Caddy to public app traffic
- `ophelia-internal`: app to Postgres/Redis traffic

Shared services live in `platform/shared/compose.yml`. Generated app bundles
join both networks with stable aliases like `dragon-writer-web`.

The shared Caddy service also maps `host.docker.internal` to Docker's
host-gateway address so tunnel-style manifests can proxy to localhost-bound host
services on Linux.

## Deployment Model

Preferred flow:

1. app repo builds an image in CI
2. image is pushed to GHCR
3. `ship deploy` renders runtime config
4. `ship deploy` pulls the image and updates the app on the VPS
5. Caddy reloads against generated snippets

The current codebase only implements manifest parsing and local runtime bundle
generation plus remote bundle staging. Service startup and Caddy activation are
available through `ship deploy --host ... --apply`.

For platform-owned ingress, Ophelia now also includes:

- `platform/scripts/sync-platform-static.sh`
- `platform/scripts/apply-manifests.sh`
- `platform/scripts/validate-caddy.sh`
- `platform/scripts/cutover-public-edge.sh`

Those scripts let the VPS reconcile platform manifests without hand-editing
public edge config.

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

## Incremental Cutover

The current VPS still uses `~/edge` for public ingress. Ophelia should migrate
in this order:

1. start shared foundation services
2. migrate app runtime state and deployment flow
3. use `host_port` bridge bindings for apps that still need to sit behind the
   legacy `~/edge` Caddy
4. move remaining public edge hosts into Ophelia manifests
5. validate generated Ophelia Caddy config
6. move public ingress to Ophelia Caddy through the cutover script

## AspectAvy Domain Layout

The bagels.top rehearsal layout is:

- `app.bagels.top`
- `api.bagels.top`
- `docs.bagels.top`
- `admin.bagels.top`
- `dev.bagels.top`

The future mirror is:

- `app.aspectavy.com`
- `api.aspectavy.com`
- `docs.aspectavy.com`
- `admin.aspectavy.com`
- `dev.aspectavy.com`

Staging follows:

- `staging-app.bagels.top`
- `staging-api.bagels.top`
- `staging-docs.bagels.top`
- `staging-admin.bagels.top`

Docs and admin hosts are implemented generically through route-level exact-path
passthroughs plus host-level `rewrite_prefix` behavior, not through
AspectAvy-specific Caddy hacks.
