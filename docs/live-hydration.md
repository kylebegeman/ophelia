# Live Hydration Reports

Live hydration reports are the read-only bridge between a broad live-readiness
baseline and the first opt-in live probes. They focus on one app or one live
drill profile and list the concrete runtime evidence that must exist before
HTTP, Docker, authenticated provider, workflow, or mutation testing should run.

## Command

Run the focused local Quark staging baseline:

```bash
./cli/ship live-hydration report \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --allow-blocked \
  --json
```

Equivalent Make target:

```bash
make live-hydration-quark-staging
```

Run from explicit app inputs instead of a profile:

```bash
./cli/ship live-hydration report \
  --app quark-ops \
  --environment production \
  --host-config config/ophelia-hosts.yml \
  --provider-config config/ophelia-integrations.yml \
  --json
```

`--allow-blocked` only changes the process exit code. The JSON payload still
emits `status: "blocked"` when evidence is incomplete.

## API

The local API exposes profile-backed hydration reports:

```text
GET /live-hydration/<profile>
```

The API route uses the default local live profile file and the configured
runtime root. Custom profile files remain CLI-only.

## Payload

`kind: "ophelia.live_hydration_report"` includes:

- app, environment, profile, resolved manifest, runtime, host, and provider
  paths
- `read_only: true`, `dry_run: true`, `mutates_state: false`, and
  `confirmation_required: false`
- `probe_policy` showing that HTTP and Docker probes are disabled
- section summaries for runtime paths, env shape, secret-name observations,
  release metadata, host placement, drift, and app readiness
- ordered `hydration_steps` with copyable targets or paths
- structured blockers and warnings

## Current Quark Staging Result

The current file-based Quark staging baseline is expected to be blocked. The
local runtime root does not yet contain an app runtime tree for
`quark-ops-staging`, so the report asks for:

- runtime app root evidence under `~/ophelia-runtime/apps/quark-ops-staging`
- required env-key presence without values
- observed required secret names, never secret values
- active or latest release metadata
- explicit host capability inventory
- drift review after runtime evidence exists
- a final rerun of the live drill profile before probes

## Safety Contract

`ship live-hydration report` is read-only:

- no runtime files are created or edited
- no secret values are read
- no provider calls are made
- no HTTP or Docker probes are run
- no state refresh, workflow execution, apply, create, deploy, or rollback
  command is called
- no confirmation token is accepted
- output passes through propagated redaction

Use hydration reports before enabling probes. A focused app should have a
reviewed file-based baseline before running `--probe-http`, `--check-docker`,
authenticated provider observation, workflow rehearsal, or production mutation.
