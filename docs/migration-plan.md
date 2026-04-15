# Migration Plan

## Current VPS Reality

- public ingress now runs through Ophelia-managed Caddy on `80/443`
- app deployments are spread across independent repos
- several apps still own their own Postgres containers
- `~/begam.in` includes stale deployment and backup logic
- AspectAvy staging and production now run through repo-owned Ophelia service manifests and the shared runtime
- legacy `~/edge` is retained only as a rollback artifact until cleanup
- the full AspectAvy rehearsal DNS set now resolves to the VPS

## Migration Order

1. Back up the current VPS state exactly as-is.
2. Create `ophelia-runtime` and shared Docker networks.
3. Stand up shared Postgres and Redis alongside the current apps.
4. Move public edge hosts into platform-owned manifests in `manifests/`.
5. Reconcile platform manifests through `platform/scripts/apply-manifests.sh`.
6. Validate generated Caddy config through `platform/scripts/validate-caddy.sh`.
7. Cut public ingress over with `platform/scripts/cutover-public-edge.sh`.
8. Move app repos onto repo-owned Ophelia manifests and shared services.
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

## Current Follow-up After AspectAvy Migration

The major AspectAvy migration steps are now done:

- repo-owned service manifests exist in the AspectAvy repo
- public ingress serves the full `app/api/docs/admin/dev` bagels rehearsal layout
- staging and production run against shared Postgres instead of bundled DB containers

The remaining follow-up is narrower:

- keep the app-repo GHCR publish/deploy workflows healthy
- retire the stopped legacy AspectAvy DB containers when rollback comfort is high
- move the mirrored `aspectavy.com` hostnames onto this VPS when the final
  domain cutover is ready

## Deferred Until Phase 2

- Authelia
- Uptime Kuma integration
- GlitchTip
- one-command rollback with traffic switching
