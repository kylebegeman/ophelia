# Ophelia Fixture App Suite

This directory contains synthetic micro apps and runtime observations for
testing Ophelia readiness, placement, drift, provider, secret, backup, and
workflow surfaces without touching production state. It also includes reviewed
hydration evidence for one focused app so validation, promotion planning, and
probe-review flows can be rehearsed without live values.

This suite is the source-of-truth test substrate for Ophelia contract work.
Existing products should be treated as migration or deployment targets, not as
the default shape that drives new platform behavior.

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
make live-drills-fixtures
make live-hydration-reviewed-fixture
make production-hardening-fixtures
```

The live-readiness target uses `--allow-blocked` because
`fixture-incomplete-app` is expected to make the aggregate report blocked.
The live-drills target validates the committed expected mixed states in
`live-drills.yml`.

The reviewed hydration target uses
`hydration/fixture-postgres-api/staging/`. That kit stores env shape, observed
secret names, synthetic release metadata, and host capability facts only. It
does not store runtime values and must not be copied into a real runtime root.
