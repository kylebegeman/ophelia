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

Plan a non-secret evidence scaffold for the same app:

```bash
./cli/ship live-hydration scaffold \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --json
```

Equivalent Make target:

```bash
make live-hydration-scaffold-quark-staging
```

Write the scaffold templates to a review directory:

```bash
./cli/ship live-hydration scaffold \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --output-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging \
  --write \
  --json
```

Validate a scaffold or evidence directory:

```bash
./cli/ship live-hydration validate-evidence \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging \
  --json
```

Temporary scaffold validation smoke:

```bash
make live-hydration-validate-evidence-quark-staging
```

Check whether opt-in probes are allowed later:

```bash
./cli/ship live-hydration probe-gate \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --allow-blocked \
  --json
```

Equivalent current-state audit:

```bash
make live-hydration-probe-gate-quark-staging
```

Plan reviewed evidence promotion without copying files:

```bash
./cli/ship live-hydration promotion-plan \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging \
  --json
```

Temporary scaffold promotion-plan smoke:

```bash
make live-hydration-promotion-plan-quark-staging
```

Rehearse the reviewed-evidence flow against the committed fixture kit:

```bash
make live-hydration-reviewed-fixture
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

`kind: "ophelia.live_hydration_scaffold"` includes:

- the same app/profile resolution as the report
- `target_paths` for the real runtime env, secret-name observation, release
  metadata, and host inventory destinations
- five template files: `README.md`, `env.required.template`,
  `github-secret-observation.template.json`, `release-metadata.template.json`,
  and `host-capabilities.template.yml`
- `template_only: true` and `values_redacted: true`
- `dry_run: true` unless `--write` is supplied

Scaffold templates are not consumed by readiness. They live under
`<runtime_root>/hydration/<app>/<environment>` by default, so generating them
does not claim fake env values, secret observations, release metadata, or host
capabilities.

`kind: "ophelia.live_hydration_evidence_validation"` includes:

- file checks for the scaffold/evidence directory
- expected env keys, secret names, release metadata shape, and host capability
  flags
- blocker codes for missing files, invalid JSON, missing required names, and
  secret-looking values
- warning codes when templates are still placeholders
- `values_redacted: true`; values are never emitted

Validation is intentionally not promotion. A warning result can be useful while
templates are still blank. A blocked result means the evidence directory is
missing, malformed, or contains data that should not live in a scaffold.

`kind: "ophelia.live_hydration_probe_gate"` includes:

- compact hydration and evidence validation child summaries
- `go_no_go: "go" | "review" | "no_go"`
- `probes_executed: false`
- `probe_policy` with HTTP and Docker disabled
- `probe_commands` only when there are no blockers
- `next_commands` for the blocked path

The gate is a decision report, not a runner. Current Quark staging returns
`no_go` because hydration and evidence validation are still blocked.

`kind: "ophelia.live_hydration_promotion_plan"` includes:

- compact evidence validation and probe-gate child summaries
- file actions for runtime env, GitHub secret-name observations, active
  release metadata, legacy release metadata, and host inventory
- source path, source existence, source byte count, and source SHA-256
- target path, target existence, and target parent existence
- `copy_performed: false`, `manual_promotion_required: true`,
  `future_apply_supported: false`, and `future_apply_requires_confirmation:
  true`
- `values_not_included: true`; file contents and runtime values are never
  emitted

The promotion plan is an evidence handoff checklist. It intentionally does not
copy reviewed files into runtime paths, edit provider observation files, merge
host inventory, or run probes.

## Reviewed Fixture Kit

`fixtures/app-suite/hydration/fixture-postgres-api/staging/` is a committed,
synthetic reviewed evidence kit for `fixture-postgres-focused`. It is useful
when changing hydration contracts because it exercises the post-template flow:

- evidence validation returns `ok`
- promotion planning returns a warning checklist because fixture drift still
  needs review
- probe gate returns `review` and emits explicit probe commands without running
  them

The kit stores env shape, observed secret names, synthetic release metadata, and
host capability facts. It does not contain runtime values and must not be copied
into a real runtime root.

## Current Quark Staging Result

The current file-based Quark staging baseline is expected to be blocked. The
local runtime root now contains an empty app runtime tree for
`quark-ops-staging` and a separate template-only hydration review scaffold under
`~/ophelia-runtime/hydration/quark-ops-staging/staging`, so the report still
asks for:

- required env-key presence without values
- observed required secret names, never secret values
- active or latest release metadata
- explicit host capability inventory
- drift review after runtime evidence exists
- a final rerun of the live drill profile before probes

The bounded live snapshot attempt is recorded in
[`quark-ops-staging-live-snapshot-2026-06-22.md`](scratchpad/quark-ops-staging-live-snapshot-2026-06-22.md).

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

`ship live-hydration scaffold` is read-only by default. With `--write`, it only
writes template files under the scaffold output directory. It does not modify
`apps/<app>/env`, `active_release.json`, `release.json`, GitHub secret
observation paths, host inventory, provider config, state DB files, workflows,
or production runtime paths.

`ship live-hydration validate-evidence` is always read-only. It never copies,
promotes, or mutates runtime files. It reports secret-shaped values by key name
and issue code only.

`ship live-hydration probe-gate` is always read-only. It never performs network
or Docker probes. When the gate is clear enough, it emits exact commands an
operator can review and run explicitly.

`ship live-hydration promotion-plan` is always read-only. It reports source
hashes and target paths for reviewed evidence but does not copy, promote, or
merge files. Any future automated promotion would require a separate confirmed
apply command.

Use hydration reports before enabling probes. A focused app should have a
reviewed file-based baseline before running `--probe-http`, `--check-docker`,
authenticated provider observation, workflow rehearsal, or production mutation.
