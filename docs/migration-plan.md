# Migration Plan

## Current VPS Reality

- `~/edge` is the real Caddy source of truth
- app deployments are spread across independent repos
- several apps still own their own Postgres containers
- `~/begam.in` includes stale deployment and backup logic
- AspectAvy staging and production still run as localhost-bound legacy stacks
- As of April 14, 2026, `docs.bagels.top`, `admin.bagels.top`, `dev.bagels.top`,
  `staging-docs.bagels.top`, and `staging-admin.bagels.top` do not yet resolve
  to the VPS

## Migration Order

1. Back up the current VPS state exactly as-is.
2. Create `ophelia-runtime` and shared Docker networks.
3. Stand up shared Postgres and Redis alongside the current apps.
4. Move public edge hosts into platform-owned manifests in `manifests/`.
5. Reconcile platform manifests through `platform/scripts/apply-manifests.sh`.
6. Validate generated Caddy config through `platform/scripts/validate-caddy.sh`.
7. Add the missing DNS records for the new AspectAvy rehearsal hosts.
8. Cut public ingress over with `platform/scripts/cutover-public-edge.sh`.
9. Remove stale cron jobs and old ingress config after cutover.

## First Apps To Migrate

1. one static site
2. `dragon-writer`
3. `pokedex`
4. `AspectAvy` staging and production
5. `dev.pokedex` if it still needs to exist during edge cutover

## AspectAvy Rehearsal Layout

Production on `bagels.top`:

- `app.bagels.top` proxies the customer-facing app and public pages
- `api.bagels.top` proxies the raw backend/API host
- `docs.bagels.top` rewrites host-root requests onto upstream `/docs` while
  preserving the machine-readable docs endpoints
- `admin.bagels.top` rewrites host-root requests onto upstream `/admin`
- `dev.bagels.top` serves static prototypes and previews

Staging on `bagels.top`:

- `staging-app.bagels.top`
- `staging-api.bagels.top`
- `staging-docs.bagels.top`
- `staging-admin.bagels.top`

Future mirror on `aspectavy.com`:

- `app.aspectavy.com`
- `api.aspectavy.com`
- `docs.aspectavy.com`
- `admin.aspectavy.com`
- `dev.aspectavy.com`

## Backend Follow-up After Ophelia

Ophelia can carry the host layout through tunnel manifests immediately, but the
AspectAvy backend repo should still follow up with:

- a repo-owned `.ophelia.yml` once the backend deploy lifecycle moves out of
  tunnel mode
- CI/GHCR deploy wiring so Ophelia can stop proxying to the legacy localhost
  stacks
- host-aware canonical URLs if the backend should differentiate `app`, `api`,
  `docs`, and `admin` behavior more explicitly
- any app-layer cleanup needed if absolute `/docs` or `/admin` links still leak
  across the new host split

## Deferred Until Phase 2

- Authelia
- Uptime Kuma integration
- GlitchTip
- one-command rollback with traffic switching
