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

## Phase 1 Principles

- Prefer immutable images over source builds on the VPS.
- Make manifests explicit instead of relying on auto-detection.
- Keep shared services lean: Caddy, Postgres, Redis.
- Treat Caddy config as generated output, not hand-edited source.
- Let each app repo declare its own runtime contract through
  `.ophelia.yml`.

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
