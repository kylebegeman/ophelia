# Quark Ops Staging Live Evidence Pass: 2026-06-22

Scope: bounded follow-up to the Quark staging live snapshot. This pass wrote
only non-secret runtime env shape and name-only provider observations that could
be verified from local manifests and the upstream Prism workflow. Secret values
were not read, printed, written, or committed.

## Commands Run

```bash
gh secret list --repo bagelworks/prism
gh api repos/bagelworks/prism/contents/.github/workflows/quark-image.yml
./cli/ship live-hydration report \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --allow-blocked \
  --json
./cli/ship live-hydration probe-gate \
  --profile quark-ops-staging-file-baseline \
  --profiles config/ophelia-live-drills.yml \
  --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging \
  --allow-blocked \
  --json
./cli/ship live-readiness run \
  --app quark-ops-staging \
  --environment staging \
  --runtime-root ~/ophelia-runtime \
  --manifests-dir manifests \
  --host-config config/ophelia-hosts.yml \
  --provider-config config/ophelia-integrations.yml \
  --allow-blocked \
  --json
```

## Runtime Evidence Written

Created or updated `~/ophelia-runtime/apps/quark-ops-staging/env` with only
these non-secret keys:

- `OPHELIA_APP`
- `PRISM_CONSOLE_ASSET_PATH`
- `PRISM_CONSOLE_SURFACE`

Created
`~/ophelia-runtime/github/secret-observations/quark-ops-staging.staging.json`
with name-only mappings confirmed by `.github/workflows/quark-image.yml` in
`bagelworks/prism`:

- `QUARK_STAGING_SETUP_TOKEN` -> `PRISM_CONSOLE_SETUP_TOKEN`
- `QUARK_STAGING_CREDENTIAL_ENCRYPTION_KEY` ->
  `PRISM_CREDENTIAL_ENCRYPTION_KEY`
- `QUARK_STAGING_MFA_ENCRYPTION_KEY` -> `PRISM_MFA_ENCRYPTION_KEY`

## Results

- Hydration remains `blocked`.
- Env evidence improved from 0/8 present keys to 3/8 present keys.
- Secret-provider evidence improved from 0/8 present required keys to 6/8
  present required keys because local runtime env now supplies three structural
  names and GitHub name observations supply three Prism secret names.
- Remaining env keys missing from runtime env:
  - `DATABASE_URL`
  - `PRISM_CONSOLE_SETUP_TOKEN`
  - `PRISM_CREDENTIAL_ENCRYPTION_KEY`
  - `PRISM_MFA_ENCRYPTION_KEY`
  - `REDIS_URL`
- Remaining provider secret-name blockers:
  - `DATABASE_URL`
  - `REDIS_URL`
- Focused live-readiness remains `blocked` with 9 blockers:
  - five `env_key_missing`
  - `release_missing`
  - `no_eligible_host`
  - two `required_secret_provider_missing`
- Probe gate remains `no_go` and emitted no probe commands.

## Boundary

The next step still requires real external evidence:

- actual runtime values for `DATABASE_URL`, `REDIS_URL`, and the three Prism
  secret env keys
- provider-observed names for database and Redis runtime secrets, or another
  truthful provider source
- active/latest release metadata from a real deployment
- host capability facts for an eligible runtime host

Do not fabricate those values in repo docs, committed fixtures, or generated
review scaffolds.
