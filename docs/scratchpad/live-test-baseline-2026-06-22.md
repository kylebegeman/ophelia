# Live Test Baseline: 2026-06-22

Scope: read-only file-based live tests against repo manifests and
`~/ophelia-runtime`. HTTP, Docker, provider mutation, workflow execution, state
refresh, and apply/create commands were not run.

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
make live-drills-fixtures
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

The local runtime root currently contains plans, audit, and jobs directories,
but no `apps/` runtime tree. That makes env, release, drift, and secret presence
blockers expected for this first live baseline.

## Notable Details

- `config/ophelia-hosts.yml` currently defines one `local` host with labels but
  no explicit Docker/Caddy/edge/data capability block.
- `ship host readiness --config config/ophelia-hosts.yml` returns warning, not
  blocked. The warning is `host_docker_unavailable`.
- `quark-ops` production focused drill is blocked with one app, 18 blockers,
  and 9 warnings.
- `quark-ops-staging` focused drill is blocked with one app, 18 blockers, and
  7 warnings.
- `quark-ops-staging` hydration is blocked and actionable: app root is absent,
  eight required env keys are missing, eight required secret-name observations
  are missing, no release metadata is present, host capabilities need explicit
  inventory, and drift should be reviewed after the runtime evidence exists.
- `quark-ops-staging` scaffold is warning and dry-run by default. It plans five
  template files in a separate hydration workspace and does not create consumed
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
6. Add or collect a real runtime app snapshot under `~/ophelia-runtime/apps` for
   one target app, starting with non-secret env shape and release metadata.
7. Expand `config/ophelia-hosts.yml` with explicit host capabilities for the
   intended live target host.
8. Add observed secret-name files for the target app/provider. Do not store
   secret values.
9. Rerun `ship live-drills run quark-ops-production-file-baseline --profiles
   config/ophelia-live-drills.yml --json`.
10. Run HTTP/Docker probes only after the file-based profile moves from blocked
   to a known reviewed baseline.
