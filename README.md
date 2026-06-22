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
- [Releases and Rollback](docs/releases-and-rollback.md)
- [Preflight and Safety](docs/preflight-and-safety.md)
- [Operator Runbook](docs/operator-runbook.md)
- [Job/Action API Notes](docs/job-action-api.md)
- [Host Contract](docs/host-contract.md)
- [Migration Plan](docs/migration-plan.md)
- [Ophelia Next Architecture](docs/ophelia-next-architecture.md)
- [Portable App Pack Spec](docs/portable-app-pack-spec.md)
- [Dragon Writer Migration Runbook](docs/dragon-writer-migration-runbook.md)
- [Selected Portability Feature Roadmap](docs/selected-portability-feature-roadmap.md)
- [Ophelia Improvement Execution Plan](docs/ophelia-improvement-execution-plan.md)
- [Product Improvement Findings](docs/product-improvement-findings.md)
- [Strategic Implementation Roadmap](docs/ophelia-strategic-implementation-roadmap.md)
- [Ophelia Change Records](docs/changelog/README.md)

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

## Canonical Locations

The canonical workstation checkout is:

```text
/Users/kyle/Developer/platforms/ophelia
```

The current checkout may still live at
`/Users/kyle/Developer/projects/web/ophelia` during migration. Commands are
repo-relative and compute template paths from the installed Python package, so
they work from either location as long as they are run from a complete checkout.

The VPS copy remains `~/ophelia`, and runtime state remains
`~/ophelia-runtime`. Runtime state, env files, backups, pulled images, and
generated bundles are not moved into the source checkout.

The local `_worktrees/` directory is a Git worktree holding branch
`codex/quark-image-namespace`. It is intentionally ignored by the main repo and
should not be deleted as part of the canonical path migration unless that
worktree is removed with `git worktree remove`.

## Quick Start

```bash
cd /Users/kyle/Developer/platforms/ophelia
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install PyYAML

./cli/ship self-test                 # first smoke command: confirm the install is healthy
./cli/ship schema manifest --json    # export the manifest JSON schema (draft 2020-12)
./cli/ship validate examples/dragonwriter.ophelia.yml
./cli/ship render examples/dragonwriter.ophelia.yml --output-dir ./build/dragonwriter
./cli/ship deploy examples/dragonwriter.ophelia.yml
./cli/ship deploy examples/dragonwriter.ophelia.yml --plan
./cli/ship diff examples/dragonwriter.ophelia.yml
./cli/ship explain examples/dragonwriter.ophelia.yml
./cli/ship pack validate examples/dragonwriter.ophelia.yml
./cli/ship pack explain examples/dragonwriter.ophelia.yml --json
./cli/ship env diff dragon-writer --environment production --json
./cli/ship backup status dragon-writer --environment production --json
./cli/ship app readiness dragon-writer --environment production --json
./cli/ship app runbook dragon-writer --environment production
./cli/ship app export plan dragon-writer --environment production --json
./cli/ship app export create dragon-writer --environment production --confirm <token> --json
./cli/ship app import plan ./exports/dragon-writer.production.export/manifest.json --json
./cli/ship app import apply ./exports/dragon-writer.production.export/manifest.json --confirm <token> --json
./cli/ship app restore-drill plan dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --json
./cli/ship app restore-drill apply dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --confirm <token> --json
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh --environment production --json
./cli/ship app cutover apply dragon-writer --from spaceship --to ovh --environment production --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider file --caddy-provider file --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider cloudflare --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --confirm <token> --json
./cli/ship app traffic rollback plan dragon-writer --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply dragon-writer --receipt <traffic-receipt-id> --environment production --confirm <token> --json
./cli/ship receipts list --app dragon-writer --json
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
./cli/ship inspect conflicts
./cli/ship status
./cli/ship doctor
./cli/ship verify examples/dragonwriter.ophelia.yml
./cli/ship verify dragon-writer
./cli/ship bootstrap-host kyle@209.74.71.165 --ssh-port 22022
./cli/ship list
```

By default `ship deploy` writes generated runtime state into
`~/ophelia-runtime/apps/<app>/`.
Shared Caddy snippets are staged into `~/ophelia-runtime/caddy/sites.d/`;
global edge snippets and Caddy env placeholders live under
`~/ophelia-runtime/caddy/global.d/` and `~/ophelia-runtime/caddy/env`.

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

Required deployment secrets are `VPS_HOST`, `VPS_PORT`, `VPS_USER`, and
`VPS_SSH_KEY`. The workflow validates that those names are present before
starting SSH and never prints their values.

## Next Milestones

1. Execute Phase 1 of the [Strategic Implementation Roadmap](docs/ophelia-strategic-implementation-roadmap.md): plan digest cards, expanded `ship doctor`, and command catalog examples.
2. Execute Phase 2: workflow run preview plus shared plan/receipt search aliases.
3. Execute Phase 3: durable state service and drift engine foundation.
4. Continue later phases in dependency order: workflow orchestrator, GitHub App and secrets integrations, multi-host placement, plugin contracts, then the Lumen operator console.

## Deploy Flows

### Operator-first flow

Use this for initial setup, testing, or one-off deploys from your machine:

```bash
./cli/ship deploy path/to/.ophelia.yml --plan
./cli/ship deploy path/to/.ophelia.yml --host kyle@209.74.71.165 --ssh-port 22022 --apply
```

Production manifests require a confirmation token from `ship deploy --plan`
before local apply. Mutating operator commands follow the same dry-run-first
shape: plan, inspect the report, then pass the matching `--confirm` token.

### Operator command groups

- `ship validate|render|explain|diff|deploy --plan` for manifest inspection.
- `ship pack validate|explain` for portable app pack contracts, data declarations, host requirements, and movement readiness.
- `ship env diff`, `ship backup status`, and `ship app readiness` for redacted movement readiness checks.
- `ship app runbook` for generated per-app operator runbooks from the readiness model.
- `ship app export plan` and `ship app import plan` for read-only app movement planning receipts.
- `ship app export create --confirm <token>` for confirmed metadata/runtime export bundles with redacted env shape, a `.tar` fallback archive, optional `.tar.zst`, and receipts.
- `ship app import apply --confirm <token>` for isolated rehearsal import previews that do not change active runtime.
- `ship app restore-drill plan|apply` for isolated artifact/listability drill receipts.
- `ship app cutover plan|apply` for confirmed cutover checkpoint receipts; Caddy and DNS are not mutated by the checkpoint.
- `ship app traffic plan|apply` for production traffic automation intent, readiness gates, optional target health checks, checkpoint receipts, explicit file-backed provider execution, and gated Cloudflare DNS updates when `--provider-config` and `--execute-provider-mutation` are both supplied.
- `ship app traffic rollback plan|apply` for receipt-backed rollback of file provider traffic changes when previous DNS/Caddy state was captured.
- `ship app isolation plan` for per-app network compatibility planning; manifests can opt into `networking.internal: per-app`.
- `ship receipts list|show` for local operation receipt browsing.
- `ship pack init` for preview-first app pack scaffolding; pass `--write` before it creates files.
- `ship deploy --apply --confirm <token>` for confirmed production apply.
- `ship releases <app>` and `ship release show <app> <release-id>` for release history.
- `ship rollback plan|apply` for file-level rollback from release bundle snapshots.
- `ship backup plan|create` and `ship restore plan|apply` for backup creation and safe restore previews.
- `ship drift <manifest>` and `ship drift all` for runtime drift detection.
- `ship inspect conflicts` for cross-manifest platform conflict scanning.
- `ship status`, `ship doctor`, and `ship list` for read-only runtime inspection.
- `ship actions`, `ship jobs`, and `ship api serve` for Quark-facing local job integration.

Most read-only commands accept `--json` for Quark/Prism integration.

For static apps, sync built assets first or pass a static build directory once that
workflow is added to the app repo.

### App-repo CI flow

Use this after an app repo is set up with GitHub Actions:

1. CI builds the app artifact or image.
2. CI syncs the artifact to the VPS or pushes the image to GHCR.
3. CI SSHes into the VPS and runs `~/ophelia/cli/ship deploy ... --apply`.

If the app has public health checks or operator surfaces that should be part of the release contract, run `~/ophelia/cli/ship deploy ... --apply --verify` instead.
Verification retries by default for first-deploy TLS races and records apply
status separately from verify status. If apply succeeds but external
verification fails, rerun it with `~/ophelia/cli/ship verify <app>`.

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

`platform/scripts/apply-manifests.sh` intentionally skips `quark-ops*.ophelia.yml` unless `OPHELIA_APPLY_QUARK=1` is set. Broad platform deploys should not depend on the private Prism Quark image, GHCR package access, or `ops.begam.in` verification. Explicit Quark deploys can pull the private Prism image by exporting `GHCR_USERNAME` and `GHCR_TOKEN` before running `deploy-quark-ops.sh`.

Example:

```bash
cd /Users/kyle/Developer/projects/web/prism/platform
./scripts/release/build-image.sh ghcr.io/bagelworks/prism:quark-next

cd /Users/kyle/Developer/platforms/ophelia
./platform/scripts/deploy-quark-ops.sh --environment staging --verify
```

`ops.begam.in` is intended to be a root-hosted Prism admin domain for the Quark surface. `/console` remains available as a compatibility fallback on that same host.
