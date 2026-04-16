# Ophelia

Ophelia is the deployment control plane for your VPS platform. It owns shared
infrastructure, deployment conventions, and the `ship` CLI. Application source
code stays in each app's own repository.

## Current Scope

This first pass establishes:

- a dedicated platform repo
- a manifest format for app repos
- render and validation flows for runtime bundles
- shared Caddy/Postgres/Redis platform definitions
- a local runtime layout that mirrors the eventual VPS shape
- branch conventions where `next` is the working integration branch and `master`
  is the production-sync branch

The VPS should hold runtime state only: generated config, secrets, volumes,
logs, and pulled images.

## Core Docs

- [Platform Handbook](docs/platform-handbook.md)
- [Architecture](docs/architecture.md)
- [AspectAvy Host Layout](docs/aspectavy-host-layout.md)
- [Manifest Spec](docs/manifest-spec.md)
- [Migration Plan](docs/migration-plan.md)

## Repository Layout

```text
ophelia/
  cli/                    # Local entrypoints
  docs/                   # Architecture and migration notes
  examples/               # Example app manifests
  manifests/              # Platform-owned operational manifests
  platform/               # Shared infrastructure and host scripts
  src/ophelia/            # Python control plane
  templates/              # Render templates for compose and Caddy
```

## Quick Start

```bash
cd /Users/kyle/Developer/projects/web/ophelia
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install PyYAML

./cli/ship validate examples/dragonwriter.ophelia.yml
./cli/ship render examples/dragonwriter.ophelia.yml --output-dir ./build/dragonwriter
./cli/ship deploy examples/dragonwriter.ophelia.yml
./cli/ship verify examples/dragonwriter.ophelia.yml
./cli/ship bootstrap-host kyle@209.74.71.165 --ssh-port 22022
./cli/ship list
```

By default `ship deploy` writes generated runtime state into
`~/ophelia-runtime/apps/<app>/`.
Shared Caddy snippets are staged into `~/ophelia-runtime/caddy/sites.d/`.

## Phase 1 Principles

- Prefer immutable images over source builds on the VPS.
- Make manifests explicit instead of relying on auto-detection.
- Keep shared services lean: Caddy, Postgres, Redis.
- Treat Caddy config as generated output, not hand-edited source.
- Let each app repo declare its own runtime contract through
  `.ophelia.yml`.

## Phased Migration

You do not need to cut public ingress over immediately.

Recommended order:

1. bootstrap Ophelia runtime and networks
2. bring up shared foundation services first: Postgres and Redis
3. stage and test app runtime bundles
4. bridge migrated apps back into the legacy `~/edge` Caddy with localhost
   `host_port` mappings when needed
5. move remaining public hosts into platform-owned manifests
6. validate generated Caddy config through `platform/scripts/validate-caddy.sh`
7. cut over public ingress through `platform/scripts/cutover-public-edge.sh`

## Branch Strategy

- `next` is the default working branch.
- `master` is the production branch for platform sync to the VPS.
- pushes to `master` run the platform deployment workflow in `.github/workflows`
  once the repository is connected to GitHub secrets.

## Next Milestones

1. Clean up the remaining legacy/manual VPS deployments and stale cron drift.
2. Add release history, rollback, and health-check verification.
3. Keep moving app repos onto repo-owned Ophelia manifests and GHCR-backed CI deploys.
4. Layer in Authelia once the basic runtime path is stable.

## Deploy Flows

### Operator-first flow

Use this for initial setup, testing, or one-off deploys from your machine:

```bash
./cli/ship deploy path/to/.ophelia.yml --host kyle@209.74.71.165 --ssh-port 22022 --apply
```

For static apps, sync built assets first or pass a static build directory once that
workflow is added to the app repo.

### App-repo CI flow

Use this after an app repo is set up with GitHub Actions:

1. CI builds the app artifact or image.
2. CI syncs the artifact to the VPS or pushes the image to GHCR.
3. CI SSHes into the VPS and runs `~/ophelia/cli/ship deploy ... --apply`.

If the app has public health checks or operator surfaces that should be part of the release contract, run `~/ophelia/cli/ship deploy ... --apply --verify` instead.

For static sites this means:

1. build `dist/`
2. rsync `dist/` into `~/ophelia-runtime/static/<app>/`
3. run `~/ophelia/cli/ship deploy /path/to/.ophelia.yml --runtime-root ~/ophelia-runtime --apply`

## Shared Services

The shared compose is intentionally split into:

- foundation: Postgres and Redis
- edge: Caddy

That lets you migrate the platform in-place without fighting the current
public `~/edge` Caddy container on ports `80/443`.

For apps that still need to sit behind the legacy public edge during
migration, set `services.<name>.host_port` in the manifest. Ophelia will bind
that service to `127.0.0.1:<host_port>` while still attaching it to the shared
Docker networks, so `~/edge` can proxy to it before full ingress cutover.

For host-based ingress that still points at legacy localhost-bound apps,
use tunnel manifests plus route rewrites. The shared Caddy service now exposes
`host.docker.internal` through Docker's host-gateway mapping so Ophelia-managed
ingress can proxy to existing host services without hand-maintained Caddy rules.

## Prism-first Deployments

Ophelia can already host Prism as a normal service, but it now also has a Prism-first manifest profile so Prism-backed products do not need to keep re-expressing the same runtime contract.

Use `profile: prism` when a manifest is primarily hosting:

- a Prism runtime
- a Prism-backed product surface such as Quark
- a Prism service that needs a dedicated admin host and a baked Console bundle

The Prism profile adds:

- a `prism:` block for dedicated admin-host and surface metadata
- manifest-relative `env_files` copied into the runtime bundle
- service `mounts` when a product genuinely needs extra runtime files or directories
- optional inferred verification checks for `/health` and `/console`
- automatic routing for `prism.admin_domain` when that host should proxy to the primary Prism service

Use [examples/quark-ops.ophelia.yml](examples/quark-ops.ophelia.yml) as the current reference shape for a baked-image Quark surface on `ops.begam.in`, and [manifests/quark-ops-staging.ophelia.yml](manifests/quark-ops-staging.ophelia.yml) for the staging host on `ops-staging.begam.in`.

For the dedicated Quark hosts:

1. build and push the Prism image from the `prism` repo
2. sync the Ophelia control plane onto the VPS without deleting remote-only state
3. deploy the staging or production manifest with `platform/scripts/deploy-quark-ops.sh`

Example:

```bash
cd /Users/kyle/Developer/projects/web/prism/platform
./scripts/release/build-image.sh ghcr.io/mrbagels/prism:quark-next

cd /Users/kyle/Developer/projects/web/ophelia
./platform/scripts/deploy-quark-ops.sh --environment staging --verify
```

`ops.begam.in` is intended to be a root-hosted Prism admin domain for the Quark surface. `/console` remains available as a compatibility fallback on that same host.
