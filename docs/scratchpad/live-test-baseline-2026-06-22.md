# Live Test Baseline: 2026-06-22

Scope: read-only file-based live tests against repo manifests and
`~/ophelia-runtime`. HTTP, Docker, provider mutation, workflow execution, state
refresh, and apply/create commands were not run. Later the same day, the Quark
staging app root and template-only hydration review scaffold were created; see
[`quark-ops-staging-live-snapshot-2026-06-22.md`](quark-ops-staging-live-snapshot-2026-06-22.md).
The follow-up partial evidence pass is recorded in
[`quark-ops-staging-live-evidence-pass-2026-06-22.md`](quark-ops-staging-live-evidence-pass-2026-06-22.md).

## Commands Run

```bash
./cli/ship hardening production-readiness --json
./cli/ship live-readiness run \
  --runtime-root ~/ophelia-runtime \
  --manifests-dir manifests \
  --host-config config/ophelia-hosts.yml \
  --provider-config config/ophelia-integrations.yml \
  --allow-blocked \
  --json
./cli/ship live-drills run quark-ops-production-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --json
./cli/ship live-hydration report \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --allow-blocked \
  --json
./cli/ship live-hydration scaffold \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --json
./cli/ship live-hydration validate-evidence \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --json
./cli/ship live-hydration probe-gate \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --allow-blocked \
  --json
./cli/ship live-hydration promotion-plan \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --json
./cli/ship live-hydration promotion-plan \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --json
make live-drills-fixtures
make live-hydration-reviewed-fixture
```

## Result Summary

- Production hardening: `blocked`, `go_no_go: "no_go"`.
- Live readiness: `blocked`.
- Real manifest app count: 8.
- App statuses: 8 blocked, 0 warning, 0 ok.
- Drift count: 8.
- Fixture live drills: `ok`, 4 profiles matched expected mixed states.
- GitHub provider status: `ok`, selected provider `gh`.
- Secret provider status: `ok`, 3 provider contracts.

## Main Blocker Groups

Top aggregate blocker codes:

- `env_key_missing`: required runtime env keys are not present under
  `~/ophelia-runtime/apps/<app>/env`.
- `required_secret_provider_missing`: observed providers do not report required
  secret names for the live apps.
- `no_eligible_host`: the configured local host inventory is minimal and does
  not satisfy app placement requirements.
- `release_missing`: no active/latest release metadata exists under the local
  runtime root.

The initial local runtime root contained plans, audit, and jobs directories but
no `apps/` runtime tree. The follow-up Quark staging snapshot attempt created
`~/ophelia-runtime/apps/quark-ops-staging`, so the runtime app-root blocker has
cleared for that one app. The partial evidence pass added three non-secret env
keys and three verified GitHub secret-name observations. Database/Redis env,
release, drift, host, and two secret-name blockers remain expected until real
evidence is supplied.

## Notable Details

- `config/ophelia-hosts.yml` currently defines one `local` host with labels but
  no explicit Docker/Caddy/edge/data capability block.
- `ship host readiness --config config/ophelia-hosts.yml` returns warning, not
  blocked. The warning is `host_docker_unavailable`.
- `quark-ops` production focused drill is blocked with one app, 18 blockers,
  and 9 warnings.
- `quark-ops-staging` focused drill is blocked with one app, 18 blockers, and
  7 warnings.
- `quark-ops-staging` hydration is blocked and actionable: app root now exists,
  three non-secret env keys are present, three Prism staging secret names are
  observed through GitHub workflow evidence, five runtime env keys are still
  missing, two provider secret names are still missing, no release metadata is
  present, host capabilities need explicit inventory, and drift should be
  reviewed after runtime evidence exists.
- `quark-ops-staging` scaffold is warning and has now been written to
  `~/ophelia-runtime/hydration/quark-ops-staging/staging`. It created five
  template files in a separate hydration workspace and did not create consumed
  runtime env, release, provider observation, or host inventory files.
- `quark-ops-staging` evidence validation blocks when the default evidence
  directory is absent. A temporary generated scaffold validates as warning
  because placeholders and template markers remain, which is expected before
  operator evidence collection.
- `quark-ops-staging` probe gate is `no_go`, with no probe commands emitted,
  because hydration and evidence validation are still blocked.
- `quark-ops-staging` promotion planning is read-only. It blocks when the
  default evidence directory is absent and can produce a warning checklist from
  a temporary generated scaffold with source hashes, target paths, and no file
  contents.
- After the bounded live snapshot attempt, `quark-ops-staging` promotion
  planning against the written scaffold is `warning`, and the probe gate remains
  `no_go`.
- After the partial evidence pass, focused live-readiness still reports
  `blocked` with nine blockers: five missing env keys, missing release metadata,
  no eligible host, and two missing provider secret names.
- `fixture-postgres-focused` now has a committed reviewed evidence kit that
  validates as `ok`. Its promotion plan remains read-only and warning-only
  because fixture drift still requires review before probes.
- Secret-provider metadata is useful after the live-readiness redaction fix:
  compact secret reports now show kind, status, key count, missing count,
  blocker count, warning count, and summary without exposing values.

## Next Safe Steps

1. Use `ship live-hydration report --profile quark-ops-staging-file-baseline
   --profiles config/ophelia-live-drills.yml --allow-blocked --json` as the
   focused baseline gate for the first app.
2. Use `ship live-hydration scaffold --profile quark-ops-staging-file-baseline
   --profiles config/ophelia-live-drills.yml --json` to generate the non-secret
   evidence kit for review.
3. Run `ship live-hydration validate-evidence --profile
   quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml
   --input-dir <reviewed-kit> --json` before copying any evidence into consumed
   runtime paths.
4. Run `ship live-hydration probe-gate --profile
   quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml
   --input-dir <reviewed-kit> --allow-blocked --json` before enabling any
   probes.
5. Run `ship live-hydration promotion-plan --profile
   quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml
   --input-dir <reviewed-kit> --json` to review exact source hashes and target
   paths before any manual promotion.
6. Add real runtime env values for `DATABASE_URL`, `REDIS_URL`,
   `PRISM_CONSOLE_SETUP_TOKEN`, `PRISM_CREDENTIAL_ENCRYPTION_KEY`, and
   `PRISM_MFA_ENCRYPTION_KEY` to
   `~/ophelia-runtime/apps/quark-ops-staging/env`. Keep this file outside Git.
7. Expand `config/ophelia-hosts.yml` with explicit host capabilities for the
   intended live target host.
8. Add observed secret-name files for the target app/provider. Do not store
   secret values.
9. Rerun `ship live-drills run quark-ops-production-file-baseline --profiles
   config/ophelia-live-drills.yml --json`.
10. Run HTTP/Docker probes only after the file-based profile moves from blocked
   to a known reviewed baseline.
