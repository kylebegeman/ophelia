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

The public example registry lives at `config/app-registry.example.json`.
Operators can point Ophelia at a private registry with `OPHELIA_APP_REGISTRY`
or `--registry`.

## Host Inventory

`ship host inventory --json` emits the read-only host inventory contract
(`kind: "ophelia.host_inventory"`). The local host is collected from live status
checks, and optional JSON/YAML records can add provider, region, capacity,
capability, network, backup, and foundation service metadata for other hosts.

`ship host readiness [host-id] --json` evaluates capability, capacity, network,
and backup readiness without mutating the host.

See [Host Inventory And Placement](host-inventory-and-placement.md) for the
record shape and scoring behavior.

## Safety Rules

- Protected entries are inventory only unless an operator explicitly starts an
  adoption, migration, or deployment phase.
- Caddy site files must be backed up before editing or removing them.
- Caddy config must validate before reload.
- `shared-caddy-1`, `shared-postgres-1`, `shared-redis-1`, and `ophelia-edge`
  are shared runtime foundations and must not be deleted as part of app
  cleanup.

## Operator Commands

Use `ophelia` or `ship`; both invoke the same CLI.

```bash
./cli/ophelia apps
./cli/ophelia status
./cli/ophelia caddy validate
./cli/ophelia caddy reload --json
./cli/ophelia host inventory --json
./cli/ophelia host readiness local --json
./cli/ophelia app health demo-service
./cli/ophelia app logs demo-service
./cli/ophelia app placement demo-service --environment staging --json
```
