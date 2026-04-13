# Migration Plan

## Current VPS Reality

- `~/edge` is the real Caddy source of truth
- app deployments are spread across independent repos
- several apps still own their own Postgres containers
- `~/begam.in` includes stale deployment and backup logic

## Migration Order

1. Back up the current VPS state exactly as-is.
2. Create `ophelia-runtime` and shared Docker networks.
3. Stand up shared Caddy, Postgres, and Redis alongside the current apps.
4. Recreate the current routes as generated Ophelia snippets.
5. Migrate one app at a time into shared services.
6. Remove stale cron jobs and old ingress config after cutover.

## First Apps To Migrate

1. one static site
2. `dragon-writer`
3. `pokedex`
4. `AspectAvy` staging and production

## Deferred Until Phase 2

- Authelia
- Uptime Kuma integration
- GlitchTip
- one-command rollback with traffic switching
