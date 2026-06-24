# Live Hydration Reports

Live hydration reports are the read-only bridge between a broad live-readiness
baseline and the first opt-in live probes. They focus on one app or one live
drill profile and list the concrete runtime evidence that must exist before
HTTP, Docker, authenticated provider, workflow, or mutation testing should run.

## Command

Run a blocked synthetic fixture baseline:

```bash
./cli/ship live-hydration report \
  --profile fixture-incomplete-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --allow-blocked \
  --json
```

Equivalent Make target:

```bash
make live-hydration-reviewed-fixture
```

Plan a non-secret evidence scaffold for the blocked fixture:

```bash
./cli/ship live-hydration scaffold \
  --profile fixture-incomplete-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --json
```

Write the scaffold templates to a review directory:

```bash
./cli/ship live-hydration scaffold \
  --profile fixture-incomplete-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --output-dir /tmp/ophelia-hydration/fixture-incomplete-app/staging \
  --write \
  --json
```

Validate the committed reviewed fixture evidence directory:

```bash
./cli/ship live-hydration validate-evidence \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --json
```

Check whether opt-in probes are allowed later:

```bash
./cli/ship live-hydration probe-gate \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --json
```

Plan reviewed evidence promotion without copying files:

```bash
./cli/ship live-hydration promotion-plan \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --json
```

Rehearse the reviewed-evidence flow against the committed fixture kit:

```bash
make live-hydration-reviewed-fixture
```

Run from explicit app inputs instead of a profile:

```bash
./cli/ship live-hydration report \
  --app fixture-postgres-api \
  --environment staging \
  --runtime-root fixtures/app-suite/runtime \
  --manifest fixtures/app-suite/manifests/fixture-postgres-api.ophelia.yml \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
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

The gate is a decision report, not a runner. The committed reviewed fixture kit
returns `review` and emits commands for operator inspection without running
probes.

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

## Legacy Snapshot Records

Earlier private live snapshot scratchpad records were removed from the public
tree during open-source readiness cleanup. They are not the forward test model.
New hydration behavior should be proved with synthetic fixtures first. Real
retained products should receive their own profiles and evidence only during an
approved adoption, migration, or production deployment phase.

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
