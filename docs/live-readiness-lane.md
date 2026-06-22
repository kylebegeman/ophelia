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
  --app dragon-writer \
  --environment production \
  --manifest ./manifests/dragon-writer.ophelia.yml \
  --from local \
  --to ovh-gra \
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
5. Opt into `--probe-http` and `--check-docker` only after the file-only report
   is understood.

Use Phase 9 production hardening for actual migration rehearsals, failure drills,
provider mutation tests, and live production applies.

## Fixture Suite

Use [Fixture App Suite](fixture-app-suite.md) when changing readiness,
placement, provider, secret, backup, restore, drift, or workflow behavior. It
contains multiple synthetic app kinds plus a deliberately incomplete app, so it
can prove the live-readiness lane handles mixed states without production
runtime data.
