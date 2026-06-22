# Platform Handbook

Status: public operator reference

This handbook describes Ophelia's public operating model. Private hostnames,
personal paths, retained-product notes, and one-off deployment records belong in
a private operational layer, not in this repository.

## Operating Model

- The source checkout contains Ophelia code, templates, docs, tests, fixtures,
  and example manifests.
- Runtime state lives outside the checkout, usually under `~/ophelia-runtime` or
  an explicitly configured runtime root.
- App source code stays in each app repository.
- App repositories declare their Ophelia contract through `.ophelia.yml`.
- Generated bundles, env files, backups, receipts, release metadata, and
  provider evidence are runtime artifacts, not source files.

## Public Test Model

Use synthetic fixtures first:

```bash
make validate-fixtures
make validate-adoption-fixtures
make live-readiness-fixtures
make live-drills-fixtures
make live-hydration-reviewed-fixture
make production-hardening-fixtures
```

The fixture suite models service, static, stateful, incomplete, multi-service,
worker, provider, backup, restore, readiness, and hydration behavior without
depending on private products or live values.

## Deployment Model

All mutating operations should follow the same pattern:

1. Produce a read-only plan.
2. Review blockers, warnings, changes, artifacts, and exact apply input.
3. Apply only with the matching confirmation token.
4. Write receipts and preserve rollback context.

Representative commands:

```bash
./cli/ship deploy path/to/.ophelia.yml --plan --json
./cli/ship deploy path/to/.ophelia.yml --apply --confirm <token>
./cli/ship app traffic plan demo-app --from source-host --to target-host --target-origin demo-target.example.net --environment staging --json
./cli/ship app traffic apply demo-app --from source-host --to target-host --target-origin demo-target.example.net --environment staging --confirm <token> --json
```

## Runtime Layout

Expected runtime shape:

```text
<runtime-root>/
  apps/<app>/
  backups/
  caddy/
    env
    global.d/
    sites.d/
  receipts/
  state/
```

Static content can live under `<runtime-root>/static` or another configured
path. Caddy validation and Compose use `OPHELIA_STATIC_ROOT` when an additional
static mount is needed.

## Provider Boundaries

Provider integrations should never store raw secret values in source, receipts,
or state:

- Use env-var names, not values.
- Redact command strings and nested payloads.
- Keep provider observations name-only unless a provider-specific contract says
  otherwise.
- Require explicit provider config before provider mutations.

## Public Release Gate

Before publishing:

```bash
./cli/ship open-source audit --json
```

The strict audit must pass. Use `--allow-blocked` only while preparing the repo.
