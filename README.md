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
- branch conventions where `dev` is the working branch and `master` is the
  production-sync branch

The VPS should hold runtime state only: generated config, secrets, volumes,
logs, and pulled images.

## Repository Layout

```text
ophelia/
  cli/                    # Local entrypoints
  docs/                   # Architecture and migration notes
  examples/               # Example app manifests
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
4. optionally run Ophelia Caddy on alternate ports for validation
5. switch public ingress only after the platform path is proven

## Branch Strategy

- `dev` is the default working branch.
- `master` is the production branch for platform sync to the VPS.
- pushes to `master` run the platform deployment workflow in `.github/workflows`
  once the repository is connected to GitHub secrets.

## Next Milestones

1. Add remote execution and rollout logic to `ship deploy`.
2. Add release history, rollback, and health-check verification.
3. Add app adoption for existing manual VPS deployments.
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
