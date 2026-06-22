# Host Inventory And Placement

Ophelia models hosts as read-only placement targets. Host inventory describes
capacity, capabilities, provider metadata, foundation service readiness, network
state, and backup staging without mutating host state.

## Inventory Sources

`ship host inventory` always includes the local Ophelia host from live,
read-only status checks. Optional host records can be declared in JSON or YAML:

- `<runtime_root>/hosts/hosts.json`
- `<runtime_root>/hosts/hosts.yml`
- `<runtime_root>/inventory/hosts.json`
- `<runtime_root>/inventory/hosts.yml`
- `config/ophelia-hosts.yml`
- an explicit `--config <path>`

The default repo config only annotates the local host with stable metadata.
Live local observations win for Docker, Caddy, network, disk, memory, and
foundation service availability.

Example host record:

```yaml
version: 1
hosts:
  - id: target-host
    name: Example target runtime
    provider: example-provider
    region: gra
    arch: amd64
    roles: [runtime]
    labels: [production]
    capacity:
      memory: 8gb
      disk_free: 100gb
    capabilities:
      docker: true
      docker_compose: true
      caddy: true
      edge: true
      network: true
      backups: true
      postgres: true
      redis: true
```

## CLI Surfaces

```bash
ship host inventory --json
ship host readiness target-host --json
ship app placement demo-service --environment production --from local --to target-host --json
```

`ship app placement` resolves manifests the same way as readiness and export
planning. It derives requirements from:

- manifest kind, services, routes, and resources
- `addons.postgres` and `addons.redis`
- explicit `data` contracts
- `host_requirements.arch`, `min_memory`, `min_disk_free`,
  `requires_edge`, and `requires_docker`
- source and target host hints for locality and comparison

## Scoring

Placement scores are read-only recommendations. The planner reports:

- hard blockers for missing required capabilities or insufficient known
  capacity
- warnings for unknown provider, capacity, backup, or edge metadata
- capacity fit
- service capability fit
- edge and network fit
- backup and restore readiness
- provider metadata presence
- data locality against the source host or known runtime history

The planner returns `recommended_host` plus the top `recommended_hosts`, but it
does not create, reserve, mutate, or reconcile hosts.

## Output Contracts

- `ship host inventory --json` emits `kind: "ophelia.host_inventory"`.
- `ship host readiness --json` emits `kind: "ophelia.host_readiness"`.
- `ship app placement --json` emits `kind: "ophelia.app_placement_plan"`.

The existing `/host/inventory` and `operator_reports.host_inventory` surface
remain backwards-compatible with the legacy top-level keys while also carrying
the richer `hosts` list.

## Workflow And Lumen Integration

The `move-app` workflow now includes an `app.placement.plan` node after
readiness and before export planning. This keeps placement recommendations in
the agent-executable graph before the first mutating node pauses for
confirmation.

The Lumen adapter exposes `host_inventory` and `placement` surfaces through the
same redacted report path used by readiness, receipts, state, and workflows.

## Safety

Host inventory is read-only by default. Placement plans are dry-run reports and
never mutate hosts, DNS, runtime files, backup state, GitHub, or provider APIs.
All emitted payloads pass through the centralized deep redaction sweep.
