# Ophelia Host Contract

Ophelia is the shared VPS runtime layer. It owns shared ingress, TLS, Docker
network conventions, host inventory, deploy safety checks, health checks, and
backup conventions.

Ophelia does not own app-specific auth, business logic, product policy, Apollo
application behavior, Kepler module behavior, or app data models.

## App Contract

Each app registered on a host should declare:

- app name
- environment
- compose project
- root path
- domains
- Caddy site file
- container names
- health URLs
- public Docker network

The Hostinger registry lives at `config/hostinger-app-registry.json`.

## Safety Rules

- Boop entries are protected inventory only.
- Caddy site files must be backed up before editing or removing them.
- Caddy config must validate before reload.
- Apollo staging must be deployed and healthy before old `quark-staging`
  containers are removed.
- `/opt/quark` should remain as a timestamped backup until Apollo staging has
  survived validation.
- `shared-caddy-1`, `shared-postgres-1`, `shared-redis-1`, and `ophelia-edge`
  are shared runtime foundations and must not be deleted as part of app
  cleanup.

## Operator Commands

Use `ophelia` or `ship`; both invoke the same CLI.

```bash
./cli/ophelia apps
./cli/ophelia status
./cli/ophelia caddy validate
./cli/ophelia caddy reload
./cli/ophelia app health apollo-staging
./cli/ophelia app logs apollo-staging
```
