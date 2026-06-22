# Ophelia Fixture App Suite

This directory contains synthetic micro apps and runtime observations for
testing Ophelia readiness, placement, drift, provider, secret, backup, and
workflow surfaces without touching production state.

The suite is intentionally mixed:

- `fixture-static-site`: static app with no Docker requirement.
- `fixture-http-service`: single HTTP service with a required API token.
- `fixture-postgres-api`: service with explicit Postgres data contracts,
  backup metadata, restore-drill evidence, and backup verification evidence.
- `fixture-redis-worker`: worker-style service with Redis state.
- `fixture-multi-service`: API plus web app with multiple routes.
- `fixture-tunnel-app`: edge tunnel app with an upstream target.
- `fixture-stateful-volume`: service with a bind-mounted data volume and
  backup/restore evidence.
- `fixture-incomplete-app`: deliberately blocked fixture with missing env,
  release, and backup evidence.

All values are fake. Files under `runtime/` are committed snapshots that mimic
Ophelia runtime metadata and provider observations. They are safe for tests and
docs, but they must not be copied into a real runtime root.

## Commands

```bash
make validate-fixtures
make live-readiness-fixtures
```

The live-readiness target uses `--allow-blocked` because
`fixture-incomplete-app` is expected to make the aggregate report blocked.

