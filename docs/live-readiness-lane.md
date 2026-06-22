# Live Readiness Lane

The live readiness lane is the safe first step toward using real staging and
production values. It aggregates Ophelia's existing read-only reports against a
real runtime root, real manifests, real host inventory, and real provider
observation files without applying changes.

## Command

```bash
ship live-readiness run --environment staging --json
```

Useful options:

```bash
ship live-readiness run \
  --runtime-root ~/ophelia-runtime \
  --manifests-dir ./manifests \
  --environment staging \
  --host-config ./config/ophelia-hosts.yml \
  --provider-config ./config/ophelia-integrations.yml \
  --json
```

Limit to one app:

```bash
ship live-readiness run \
  --app demo-service \
  --environment production \
  --manifest ./manifests/demo-service.ophelia.yml \
  --from local \
  --to target-host \
  --json
```

Run against the committed fixture suite:

```bash
ship live-readiness run \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --allow-blocked \
  --json
```

`--allow-blocked` only changes the process exit code. The JSON report still
returns `status: "blocked"` when blockers exist.

For repeatable expected-state runs, prefer the committed live drill profiles:

```bash
ship live-drills run fixture-suite-review --json
ship live-drills run-all --profiles fixtures/app-suite/live-drills.yml --json
```

Live drill profiles wrap the same live-readiness report, optionally include the
production hardening report, and validate expected mixed states without hiding
the child report statuses.

For one blocked app during development, use a fixture hydration report before
enabling probes:

```bash
ship live-hydration report \
  --profile fixture-incomplete-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --allow-blocked \
  --json
```

Hydration reports do not run HTTP/Docker checks. They list missing runtime
paths, env-key presence, secret-name observations, release metadata, host
capability evidence, and drift review steps for one focused app.

Use the scaffold command to create non-secret templates for those evidence
items in a separate review directory:

```bash
ship live-hydration scaffold \
  --profile fixture-incomplete-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --output-dir /tmp/ophelia-hydration/fixture-incomplete-app/staging \
  --json
```

Before enabling probes, run the no-probe gate:

```bash
ship live-hydration probe-gate \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --allow-blocked \
  --json
```

For real retained products, add product-specific live profiles only during an
approved adoption, migration, or production deployment phase.

## What It Runs

Top-level checks:

- host inventory
- host readiness
- GitHub provider status
- secret provider status
- state DB status (read-only, no rebuild)
- Lumen dashboard data

Per-app checks:

- app readiness
- app placement
- observability status
- secret-provider presence
- drift detection

## Probe Policy

By default, the lane performs no HTTP probes and no Docker status probes beyond
the existing read-only local status surfaces.

Opt in explicitly when you want bounded live checks:

```bash
ship live-readiness run --environment staging --probe-http --check-docker --json
```

The command reports these choices under `probe_policy` so downstream agents and
operators can tell whether a result came from local files only or from bounded
live probes.

## Observation Files

The command reports expected observation paths:

- host inventory config: `config/ophelia-hosts.yml` or `--host-config`
- integration config: `config/ophelia-integrations.yml` or `--provider-config`
- GitHub repo observations: `<runtime_root>/github/observations/`
- GitHub secret observations: `<runtime_root>/github/secret-observations/`
- SOPS references: `<runtime_root>/secrets/` and `./secrets/`
- latest observability sweep: `<runtime_root>/observability/latest.json`

These files are read, not created. Provider-backed collection can plug into
Phase 7 plugin/provider contracts later.

## Safety Contract

`ship live-readiness run` is read-only:

- no `apply`, `create`, `rebuild`, or `refresh` operation is called
- no state DB is rebuilt
- no observability schedule artifacts are written
- no DNS, Caddy, GitHub, backup, runtime, or host mutation is performed
- no confirmation token is accepted or required
- all payloads pass through deep redaction

The report includes `mutation_guard` with the exact non-mutation contract.

## When To Use

Use this lane now for staging and production inspection:

1. Point it at the real runtime root.
2. Add host/provider observation files where available.
3. Run without probes first.
4. Review blockers, warnings, drift, missing observations, and placement output.
5. Run a focused `ship live-hydration report` for the first target app.
6. Generate and review `ship live-hydration scaffold` templates for that app.
7. Validate the reviewed evidence kit and run `ship live-hydration probe-gate`.
8. Opt into `--probe-http` and `--check-docker` only after the file-only report
   is understood.

Use production hardening and live drill profiles for read-only migration
rehearsal gates. Provider mutation tests and live production applies remain
behind explicit plan, confirmation-token, and receipt contracts.

## Fixture Suite

Use [Fixture App Suite](fixture-app-suite.md) when changing readiness,
placement, provider, secret, backup, restore, drift, or workflow behavior. It
contains multiple synthetic app kinds plus a deliberately incomplete app, so it
can prove the live-readiness lane handles mixed states without production
runtime data.
