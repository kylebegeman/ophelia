# Ophelia Platform Handbook

This is the source-of-truth narrative for the VPS platform work so far:

- what was changed
- why it was changed
- what is live today
- what is still missing
- how to operate it without re-learning the whole system every time

## TL;DR

- `ophelia` is now the platform repo.
- App code stays in app repos.
- The VPS is supposed to hold runtime state, not become the source of truth.
- Public ingress now runs through Ophelia-managed Caddy on `80/443`.
- `kylebegeman.com`, `dragonwriter.begam.in`, and `pokedex.begam.in` are already deploying through Ophelia-managed runtime.
- Shared Postgres and Redis are running and being used by migrated apps.
- The next major platform milestone is moving AspectAvy off tunnel ingress and into repo-owned service/image deploys.
- The full `bagels.top` rehearsal structure for AspectAvy is now expressible in manifests and renderable through generated Caddy config.

## Why This Exists

The old shape of the VPS had drift:

- `~/begam.in` contained deployment and backup logic that no longer matched the real machine
- `~/edge` was the actual public ingress source of truth
- apps were deployed independently with different conventions
- some apps owned their own Postgres containers
- cron still referenced old backups and old container names

That is exactly the kind of setup that works until it does not. The main design correction was to separate:

- desired platform state
- app source code
- runtime state on the VPS

So the platform now aims for this split:

- `ophelia` repo: platform logic and shared infrastructure
- app repos: code, build, release workflow, deploy manifest
- VPS: rendered config, env files, pulled images, volumes, logs, backups

## Current Host Identity

- VPS hostname: `bagel-box`
- SSH user: `kyle`
- SSH port: `22022`
- Direct SSH command:

```bash
ssh -p 22022 kyle@209.74.71.165
```

Important: `ophelia` is not the machine hostname. It is the platform repo/runtime name.

## What We Built

### 1. Created a dedicated platform repo

Local repo:

```text
/Users/kyle/Developer/projects/web/ophelia
```

Purpose:

- hold the `ship` CLI
- define platform architecture and conventions
- store shared Docker/Caddy templates
- own the migration path and operations docs

This is intentionally not a mirror of the VPS filesystem.

### 2. Chose a manifest-first deployment model

Each app repo is expected to declare a small `.ophelia.yml` contract.

Why:

- real apps are not all the same shape
- some are static sites
- some are single-service apps
- some are multi-service apps with path routing
- some are tunnels into a non-Ophelia service

Trying to infer everything from repo structure would get brittle fast.

### 3. Chose Python for the control plane

The `ship` CLI is Python-based instead of a large Bash script.

Why:

- manifest parsing and validation need structure
- release logic grows in complexity quickly
- env rendering and service provisioning are easier to test in Python
- Bash is still fine for small bootstrap/backup scripts, but not as the main control plane

### 4. Created a runtime root on the VPS

Runtime root:

```text
~/ophelia-runtime
```

This is where generated platform state goes.

Current intended shape:

```text
~/ophelia-runtime/
  apps/
  caddy/
    sites.d/
  static/
  backups/
  cache/
```

Why:

- it centralizes generated runtime state
- it avoids spreading platform files across unrelated repos
- it gives the platform a clean ownership boundary

### 5. Created shared Docker networks

Networks:

- `ophelia-edge`
- `ophelia-internal`

Why:

- public app traffic and internal service traffic should not be the same concern
- Caddy and public app routing sit on `ophelia-edge`
- Postgres/Redis and app internals sit on `ophelia-internal`

### 6. Brought up shared platform services

Shared services now defined in Ophelia:

- Caddy
- Postgres
- Redis

Current live shared containers:

- `shared-postgres-1`
- `shared-redis-1`

Public Caddy cutover is done. The legacy `~/edge` stack remains on disk only as a rollback artifact until cleanup.

Why shared Postgres/Redis:

- fewer containers
- less memory overhead
- more consistent app provisioning
- easier backups and future operations

### 7. Added remote apply support to `ship`

`ship` can now:

- parse manifests
- render compose and Caddy output
- write runtime bundles
- provision shared Postgres/Redis data for apps
- stage/apply manifests on the VPS

This is what makes the app-repo CI path possible.

### 8. Established the branch model

For platform and app repos:

- `dev` = working branch
- `master` = production branch

Why:

- clear separation between in-progress work and live deploys
- easy CI trigger model
- predictable mental model across repos

### 9. Wired GitHub Actions for the platform repo

`master` on `ophelia` now syncs the platform repo to the VPS and runs the remote bootstrap script.

That means the VPS copy of `~/ophelia` is kept aligned from GitHub, rather than being hand-maintained.

### 10. Wired GitHub Actions for app repos

We now have the basic steady-state release path:

1. push or merge to `master`
2. CI builds the artifact or image
3. CI syncs manifest/build output as needed
4. CI SSHes into the VPS
5. CI runs `~/ophelia/cli/ship deploy ... --apply`

This means there are now two valid deploy flows:

- operator-driven manual `ship` deploys
- normal app-repo CI deploys

That split is intentional.

### 11. Added platform-owned manifest reconciliation

The platform repo now owns a `manifests/` directory plus scripts to:

- sync platform-owned static assets
- apply every platform manifest to the VPS runtime
- validate generated Caddy config
- cut public ingress over to shared Ophelia Caddy

That is what lets `bagels.top`, AspectAvy ingress rehearsal hosts, and other
platform-level sites move under Ophelia without waiting on every app repo.

## The Deploy Model

### Mode 1: manual/operator deploy

Use this when:

- bootstrapping a new app
- testing a manifest
- recovering from CI problems
- doing first-time migration work

Typical shape:

```bash
cd ~/ophelia
./cli/ship deploy /path/to/.ophelia.yml --runtime-root ~/ophelia-runtime --apply
```

### Mode 2: app-repo CI deploy

Use this for normal releases.

Two variants exist:

- static site flow
- container/image flow

#### Static site flow

Used by `kylebegeman.com`.

Flow:

1. build `dist/` in CI
2. rsync `dist/` to `~/ophelia-runtime/static/<app>/`
3. sync `.ophelia.yml`
4. run `ship deploy ... --apply`

#### Image-based app flow

Used by `dragon-writer` and `pokedex` production.

Flow:

1. build image in CI
2. push image to GHCR
3. sync `.ophelia.yml`
4. log the VPS into GHCR
5. run `ship deploy ... --apply`

Why image-first:

- less work on the VPS
- more reproducible deploys
- cleaner rollback foundation later
- better use of small VPS resources

## Why Public Ingress Was Not Cut Over First

Public ingress now runs through Ophelia-managed Caddy on ports `80/443`.

That was deliberate.

We used a staged migration model:

1. build Ophelia foundations first
2. move apps under Ophelia runtime management
3. bridge those apps back into the old public edge using localhost ports
4. cut over public ingress only after the app path is proven

This lowered risk because:

- it avoided a full traffic cutover early
- it let us migrate app runtime independently of public TLS/edge config
- it made rollback simpler during early migration

## The `host_port` Bridge Strategy

Some manifests include `services.<name>.host_port`.

Example:

- Dragon Writer binds its service on `127.0.0.1:3601`
- Pokedex API binds on `127.0.0.1:3701`
- Pokedex web binds on `127.0.0.1:3702`

That let the legacy `~/edge` Caddy keep proxying to localhost while the app itself was fully managed by Ophelia.

Why this matters:

- migration and public ingress cutover become separate problems
- apps can move first
- edge can move later

## Current Live Topology

As of this document, the VPS is effectively in a hybrid state.

### Public edge

Live:

- shared Ophelia Caddy
- container: `shared-caddy-1`

Legacy rollback artifact:

- `~/edge`
- prior public ingress container: `edge-edge-1`

Ophelia is now the public ingress source of truth.

### Shared Ophelia foundation

Live:

- `shared-postgres-1`
- `shared-redis-1`

These are healthy and being used by migrated apps.

### Ophelia-managed apps

#### `kylebegeman`

- type: static site
- domains:
  - `kylebegeman.com`
  - `www.kylebegeman.com`
- build output syncs into:

```text
~/ophelia-runtime/static/kylebegeman
```

Status:

- live through Ophelia-managed static runtime

#### `dragon-writer`

- type: service
- image: `ghcr.io/mrbagels/dragon-writer:latest`
- bridge port: `3601`
- domain: `dragonwriter.begam.in`
- shared Postgres: yes
- shared Redis: no

Status:

- production migrated
- live through legacy edge -> localhost bridge -> Ophelia-managed container

#### `pokedex`

- type: multi-service
- images:
  - `ghcr.io/mrbagels/pokedex-api:latest`
  - `ghcr.io/mrbagels/pokedex-web:latest`
- bridge ports:
  - API: `3701`
  - web: `3702`
- domain: `pokedex.begam.in`
- path routing:
  - `/api` -> API
  - `/` -> web
- shared Postgres: yes
- shared Redis: no

Status:

- production migrated
- live through legacy edge -> localhost bridges -> Ophelia-managed containers

### Legacy/manual apps still on the box

#### AspectAvy

Still running outside full Ophelia runtime ownership, but its ingress layout is
now defined in platform-owned manifests:

- `manifests/aspectavy-production.ophelia.yml`
- `manifests/aspectavy-staging.ophelia.yml`
- `manifests/aspectavy-dev.ophelia.yml`

Production and staging still terminate at the legacy AspectAvy API containers
today. Ophelia handles them through tunnel manifests that target stable
`ophelia-edge` aliases so ingress can be finished before the backend repo is
migrated.

#### Pokedex dev

Still legacy/manual.

Current state:

- `pokedex-dev-api-1` is restarting
- dev is not yet migrated to the new Ophelia production-style flow

## Repos and Responsibilities

### Platform repo

```text
/Users/kyle/Developer/projects/web/ophelia
```

Owns:

- `ship`
- templates
- shared compose
- bootstrap scripts
- runtime conventions
- docs

### App repos

Each app owns:

- source code
- Dockerfile or static build
- GitHub Actions workflow
- `.ophelia.yml`

Current examples:

- `/Users/kyle/Developer/projects/web/kylebegeman`
- `/Users/kyle/Developer/projects/web/dragon-writer`
- `/Users/kyle/Developer/projects/multiplatform/pokedex/pokedex-platform`

## What The Manifests Mean

Every `.ophelia.yml` is the deployment contract for an app.

It answers:

- what kind of app this is
- what services it has
- which images it runs
- which ports it listens on
- which domains/routes map to which services
- whether it needs shared Postgres or Redis
- which app-wide environment values are required

That is the reason manifests are the center of the model.

## Why The Architecture Looks Like This

### 1. The platform repo is not a server snapshot

Why:

- server snapshots drift immediately
- secrets and volumes do not belong in Git
- a VPS filesystem is runtime state, not design intent

### 2. App repos still own releases

Why:

- apps should control their own build and release lifecycle
- not every app builds the same way
- CI should live next to the source code it releases

### 3. The VPS should mostly run artifacts, not builds

Why:

- VPS compute is usually the most constrained resource
- builds on the server are slower and noisier
- GHCR images are a cleaner contract than source checkouts

### 4. Public edge migration was deferred

Why:

- ingress cutovers are high-risk
- app runtime migration can be proven independently first
- bridge ports make the transition safer

### 5. Shared Postgres/Redis came before auth/monitoring

Why:

- database and cache standardization matter more than optional platform extras
- auth and monitoring are phase 2 concerns
- the first goal was reliable deploy/runtime behavior

## Current Gaps And Known Debt

These are the important unfinished pieces.

### 1. Public Ophelia Caddy is not yet the live edge

Today:

- `~/edge` is still live
- Ophelia-generated Caddy snippets exist under `~/ophelia-runtime/caddy/sites.d`
- full public ingress cutover has not happened yet
- as of April 14, 2026, the new AspectAvy rehearsal DNS names for docs/admin/dev
  are not yet pointed at the VPS

### 2. Old cron jobs still exist

Current crontab still includes `begam.in` jobs and a stale PostgreSQL backup command that references:

```text
docker exec postgres ...
```

That does not match the current shared container naming and needs cleanup.

### 3. `~/begam.in` still exists as old operational baggage

It still contains scripts/logs/backup assumptions that are no longer the platform source of truth.

### 4. AspectAvy backend runtime is not yet fully migrated

Ingress is now modeled in Ophelia, but both staging and production still point
at the legacy localhost-bound AspectAvy stacks through tunnel manifests.

### 5. Pokedex dev is still broken/legacy

`pokedex-dev-api-1` is restarting and the dev environment has not been migrated yet.

### 6. Rollback ergonomics are still early

We do not yet have the full intended release UX such as:

- release history browsing
- one-command rollback by digest
- traffic-aware rollback flow

### 7. Observability is still light

We do not yet have:

- Uptime Kuma
- GlitchTip
- richer health dashboards
- platform-wide alerting

### 8. Auth is deferred

Authelia was intentionally not introduced yet because the platform runtime and deploy path needed to stabilize first.

### 9. Backups need a unified Ophelia-owned story

There are bootstrap/backup scripts in the platform repo, but the live machine still carries legacy backup assumptions that need to be reconciled and cleaned up.

## What Was Already Proven

The important thing is that the platform is no longer theoretical.

These parts are already proven in practice:

- the server is reachable and stable
- the `ophelia` repo is synced to the VPS
- `ship` can apply real manifests on the VPS
- shared Postgres provisioning works
- shared Redis provisioning works
- static-site deploy flow works
- GHCR image deploy flow works
- production app deploys can be driven from app repo CI
- migrated apps can stay behind the old edge during transition
- host-level docs/admin rewrites can be rendered without hardcoded app-specific
  Caddy rules

That means the remaining work is mostly platform completion, not platform invention.

## Recommended Next Steps

This is the order that still makes the most sense.

### Near-term

1. Clean up stale cron and old `begam.in` operational drift.
2. Decide whether to migrate or delete the broken Pokedex dev environment.
3. Move AspectAvy from tunnel manifests into repo-owned service/image deploys
   when the backend repo is ready.
4. Add stronger status/doctor/release inspection commands to `ship`.
5. Add the missing DNS records for the new AspectAvy rehearsal hosts.

### Mid-term

6. Move public ingress from legacy `~/edge` into Ophelia-managed Caddy.
7. Remove now-obsolete edge config and old manual deployment leftovers.

### Later

8. Add rollback UX.
9. Add proper backup/restore workflows owned by Ophelia.
10. Add Authelia if admin/auth protection is still wanted.
11. Add monitoring/observability if the VPS footprint allows it.

## Operational Cheatsheet

### SSH into the VPS

```bash
ssh -p 22022 kyle@209.74.71.165
```

### Platform repo on the VPS

```text
~/ophelia
```

### Runtime root on the VPS

```text
~/ophelia-runtime
```

### Manual deploy from the VPS

```bash
cd ~/ophelia
./cli/ship deploy ~/ophelia-runtime/apps/<app>/source-manifest.yml --runtime-root ~/ophelia-runtime --apply
```

### Manual platform bootstrap on the VPS

```bash
cd ~/ophelia
./platform/scripts/bootstrap-host.sh
```

### Reconcile platform-owned manifests on the VPS

```bash
cd ~/ophelia
./platform/scripts/apply-manifests.sh
./platform/scripts/validate-caddy.sh
```

### Cut public ingress over to Ophelia Caddy

```bash
cd ~/ophelia
./platform/scripts/cutover-public-edge.sh
```

### Current branch conventions

- do work on `dev`
- merge/push to `master` for production deploy behavior

## Summary

The biggest architectural correction was this:

- stop treating the VPS as the source of truth
- stop treating one old repo as a fake image of the server
- make the platform explicit
- make app deploy contracts explicit
- keep runtime state generated and centralized

That is why Ophelia exists, and it is the right foundation for continuing the migration.
