# Fixture App Suite

The fixture app suite is a committed set of synthetic micro apps, manifests,
runtime snapshots, host inventory, and provider observations. It exists to make
readiness and live-value work testable without touching production state.

Fixture root:

```text
fixtures/app-suite/
```

## Included Apps

| App | Kind | Purpose |
| --- | --- | --- |
| `fixture-static-site` | `static` | Static app with no Docker requirement. |
| `fixture-http-service` | `service` | Simple HTTP service with required env. |
| `fixture-postgres-api` | `service` | Explicit Postgres data contract with backup, restore-drill, and verification evidence. |
| `fixture-redis-worker` | `service` | Redis-backed worker-style service. |
| `fixture-multi-service` | `multi-service` | API plus web routes for multi-service routing coverage. |
| `fixture-tunnel-app` | `tunnel` | Tunnel app with inherited upstream target. |
| `fixture-stateful-volume` | `service` | Bind-mounted volume with critical backup/restore evidence. |
| `fixture-incomplete-app` | `service` | Deliberately blocked app for missing env, release, backup, and restore-drill coverage. |

## Runtime Snapshot

The committed runtime snapshot under `fixtures/app-suite/runtime/` includes:

- runtime env files with fake values only
- release metadata for every non-broken fixture
- backup manifests for stateful fixtures
- restore-drill and backup-verification receipts
- GitHub repo observation examples
- GitHub secret observation examples
- SOPS-shaped key-name files with fake encrypted placeholders

Do not copy this runtime tree to a real host. It is a deterministic test input,
not deployable runtime state.

## Commands

Validate all fixture manifests:

```bash
make validate-fixtures
```

Validate fixture plugin metadata:

```bash
make validate-fixture-plugins
```

Run the live-readiness aggregate against the fixture suite:

```bash
make live-readiness-fixtures
```

Run the Lumen console payload against the fixture suite:

```bash
make lumen-console-fixtures
```

Equivalent direct command:

```bash
./cli/ship live-readiness run \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --allow-blocked \
  --json
```

`--allow-blocked` is intentional here because `fixture-incomplete-app` should
make the aggregate report blocked while still allowing CI and local smoke runs
to complete.

## Expected Readiness Shape

A healthy fixture run should produce:

- `kind: "ophelia.live_readiness_report"`
- `read_only: true`
- `mutates_state: false`
- eight apps in `totals.app_count`
- one blocked app: `fixture-incomplete-app`
- multiple drift findings, because fixture release metadata is a stored snapshot
  and not a freshly rendered runtime bundle
- no raw env, database, token, or provider values in output

The fixture Lumen console run should also show eight app rows, one fixture
plugin, a non-empty approval queue, and the same one-blocked/seven-warning
readiness mix.

## Maintenance Rules

- Keep fixtures synthetic. Do not use real domains, repositories, tokens,
  database URLs, private keys, or production data.
- Prefer `.invalid` domains for route and health URLs.
- Update the fixture test expectations when adding or removing fixture apps.
- Keep the suite fast and read-only. It should be safe to run in local unit
  tests and CI.
- Add focused fixture cases when new readiness, placement, provider, backup,
  restore, drift, or workflow behavior needs realistic multi-app coverage.

## Plugin Metadata

The suite includes a metadata-only plugin manifest:

```text
fixtures/app-suite/plugins/fixture-app-suite/ophelia-plugin.yml
```

The plugin manifest describes fixture apps, provider observations, secret
observations, host inventory, and a Lumen surface. It does not execute code or
register new commands.
