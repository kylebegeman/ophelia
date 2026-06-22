# Ophelia

Ophelia is the deployment control plane for your VPS platform. It owns shared
infrastructure, deployment conventions, and the `ship` CLI. Application source
code stays in each app's own repository.

## Current Scope

This first pass establishes:

- a dedicated platform repo
- a manifest format for app repos
- render and validation flows for runtime bundles
- shared Caddy/Postgres/Redis platform definitions
- a local runtime layout that mirrors the eventual VPS shape
- branch conventions where `next` is the working integration branch and `master`
  is the production-sync branch

The VPS should hold runtime state only: generated config, secrets, volumes,
logs, and pulled images.

## Core Docs

- [Platform Handbook](docs/platform-handbook.md)
- [Architecture](docs/architecture.md)
- [AspectAvy Host Layout](docs/aspectavy-host-layout.md)
- [Manifest Spec](docs/manifest-spec.md)
- [Releases and Rollback](docs/releases-and-rollback.md)
- [Preflight and Safety](docs/preflight-and-safety.md)
- [Operator Runbook](docs/operator-runbook.md)
- [Job/Action API Notes](docs/job-action-api.md)
- [Host Contract](docs/host-contract.md)
- [Live Readiness Lane](docs/live-readiness-lane.md)
- [Live Drill Profiles](docs/live-drill-profiles.md)
- [Live Hydration Reports](docs/live-hydration.md)
- [Ophelia Source Of Truth](docs/ophelia-source-of-truth.md)
- [App Adoption Planning](docs/app-adoption.md)
- [Open Source Readiness](docs/open-source-readiness.md)
- [Lumen Operator Console](docs/lumen-operator-console.md)
- [Production Hardening Report](docs/production-hardening.md)
- [Fixture App Suite](docs/fixture-app-suite.md)
- [Plugin Contracts](docs/plugin-contracts.md)
- [Migration Plan](docs/migration-plan.md)
- [Ophelia Next Architecture](docs/ophelia-next-architecture.md)
- [Portable App Pack Spec](docs/portable-app-pack-spec.md)
- [Dragon Writer Migration Runbook](docs/dragon-writer-migration-runbook.md)
- [Selected Portability Feature Roadmap](docs/selected-portability-feature-roadmap.md)
- [Ophelia Improvement Execution Plan](docs/ophelia-improvement-execution-plan.md)
- [Product Improvement Findings](docs/product-improvement-findings.md)
- [Strategic Implementation Roadmap](docs/ophelia-strategic-implementation-roadmap.md)
- [Ophelia Change Records](docs/changelog/README.md)

## Repository Layout

```text
ophelia/
  cli/                    # Local entrypoints
  docs/                   # Architecture and migration notes
  examples/               # Example app manifests
  fixtures/               # Synthetic app suites and runtime observations for tests
  manifests/              # Platform-owned operational manifests
  platform/               # Shared infrastructure and host scripts
  src/ophelia/            # Python control plane
  templates/              # Render templates for compose and Caddy
```

## Canonical Locations

The canonical workstation checkout is:

```text
/Users/kyle/Developer/platforms/ophelia
```

The current checkout may still live at
`/Users/kyle/Developer/projects/web/ophelia` during migration. Commands are
repo-relative and compute template paths from the installed Python package, so
they work from either location as long as they are run from a complete checkout.

The VPS copy remains `~/ophelia`, and runtime state remains
`~/ophelia-runtime`. Runtime state, env files, backups, pulled images, and
generated bundles are not moved into the source checkout.

The local `_worktrees/` directory is a Git worktree holding branch
`codex/quark-image-namespace`. It is intentionally ignored by the main repo and
should not be deleted as part of the canonical path migration unless that
worktree is removed with `git worktree remove`.

## Quick Start

```bash
cd /Users/kyle/Developer/platforms/ophelia
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install PyYAML

./cli/ship self-test                 # first smoke command: confirm the install is healthy
./cli/ship schema manifest --json    # export the manifest JSON schema (draft 2020-12)
./cli/ship validate examples/dragonwriter.ophelia.yml
./cli/ship render examples/dragonwriter.ophelia.yml --output-dir ./build/dragonwriter
./cli/ship deploy examples/dragonwriter.ophelia.yml
./cli/ship deploy examples/dragonwriter.ophelia.yml --plan
./cli/ship diff examples/dragonwriter.ophelia.yml
./cli/ship explain examples/dragonwriter.ophelia.yml
./cli/ship pack validate examples/dragonwriter.ophelia.yml
./cli/ship pack explain examples/dragonwriter.ophelia.yml --json
./cli/ship env diff dragon-writer --environment production --json
./cli/ship backup status dragon-writer --environment production --json
./cli/ship app readiness dragon-writer --environment production --json
./cli/ship app runbook dragon-writer --environment production
./cli/ship app export plan dragon-writer --environment production --json
./cli/ship app export create dragon-writer --environment production --confirm <token> --json
./cli/ship app import plan ./exports/dragon-writer.production.export/manifest.json --json
./cli/ship app import apply ./exports/dragon-writer.production.export/manifest.json --confirm <token> --json
./cli/ship app restore-drill plan dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --json
./cli/ship app restore-drill apply dragon-writer --environment production --source ./exports/dragon-writer.production.export.tar --confirm <token> --json
./cli/ship app cutover plan dragon-writer --from spaceship --to ovh --environment production --json
./cli/ship app cutover apply dragon-writer --from spaceship --to ovh --environment production --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider file --caddy-provider file --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --dns-provider cloudflare --provider-config ./traffic-providers.json --execute-provider-mutation --json
./cli/ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --json
./cli/ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --target-health-url https://dragonwriter-target.example.net/health --run-target-health --confirm <token> --json
./cli/ship app traffic rollback plan dragon-writer --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply dragon-writer --receipt <traffic-receipt-id> --environment production --confirm <token> --json
./cli/ship host inventory --json
./cli/ship host readiness local --json
./cli/ship app placement dragon-writer --environment production --from spaceship --to ovh --json
./cli/ship live-readiness run --environment staging --json
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
make live-readiness-fixtures
make live-drills-fixtures
make live-hydration-reviewed-fixture
make lumen-console-fixtures
make production-hardening-fixtures
./cli/ship live-drills run fixture-suite-review --json
./cli/ship live-hydration report --profile fixture-incomplete-focused --profiles fixtures/app-suite/live-drills.yml --allow-blocked --json
./cli/ship live-hydration validate-evidence --profile fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging --json
./cli/ship live-hydration probe-gate --profile fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging --json
./cli/ship live-hydration promotion-plan --profile fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging --json
./cli/ship hardening production-readiness --json
./cli/ship providers github status --json
./cli/ship secrets providers dragon-writer --environment production --json
./cli/ship app github plan --app demo-app --template static-site --owner example --repo example/demo-app --github-provider auto --json
./cli/ship workflow plan move-app --app dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --json
./cli/ship workflow run latest:dragon-writer --preview --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --set PROVIDER_CONFIG=providers.json --set MANIFEST_DIR=manifests --json
./cli/ship workflow run latest:dragon-writer --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --set PROVIDER_CONFIG=providers.json --set MANIFEST_DIR=manifests --json
./cli/ship workflow resume latest:dragon-writer --confirm-node export-create=<token> --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --set PROVIDER_CONFIG=providers.json --set MANIFEST_DIR=manifests --set EXPORT_BUNDLE=exports/dragon-writer --json
./cli/ship workflow pause latest:dragon-writer --json
./cli/ship workflow cancel latest:dragon-writer --json
./cli/ship receipts list --app dragon-writer --json
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
./cli/ship pack init --app demo-service --environment staging --directory ../demo-service --include-manifest --kind service --domain demo-service.example.com --image ghcr.io/example/demo-service:latest --json
./cli/ship app adoption plan demo-app --repo-path ../demo-app --environment staging --json
./cli/ship open-source audit --allow-blocked --json
./cli/ship inspect conflicts
./cli/ship status
./cli/ship doctor
./cli/ship verify examples/dragonwriter.ophelia.yml
./cli/ship verify dragon-writer
./cli/ship bootstrap-host kyle@209.74.71.165 --ssh-port 22022
./cli/ship list
```

By default `ship deploy` writes generated runtime state into
`~/ophelia-runtime/apps/<app>/`.
Shared Caddy snippets are staged into `~/ophelia-runtime/caddy/sites.d/`;
global edge snippets and Caddy env placeholders live under
`~/ophelia-runtime/caddy/global.d/` and `~/ophelia-runtime/caddy/env`.

## Phase 1 Principles

- Prefer immutable images over source builds on the VPS.
- Make manifests explicit instead of relying on auto-detection.
- Keep shared services lean: Caddy, Postgres, Redis.
- Treat Caddy config as generated output, not hand-edited source.
- Let each app repo declare its own runtime contract through
  `.ophelia.yml`.

## Phased Migration

You do not need to cut public ingress over immediately.

Recommended order:

1. bootstrap Ophelia runtime and networks
2. bring up shared foundation services first: Postgres and Redis
3. stage and test app runtime bundles
4. bridge migrated apps back into the legacy `~/edge` Caddy with localhost
   `host_port` mappings when needed
5. move remaining public hosts into platform-owned manifests
6. validate generated Caddy config through `platform/scripts/validate-caddy.sh`
7. cut over public ingress through `platform/scripts/cutover-public-edge.sh`

## Branch Strategy

- `next` is the default working branch.
- `master` is the production branch for platform sync to the VPS.
- pushes to `master` run the platform deployment workflow in `.github/workflows`
  once the repository is connected to GitHub secrets.

Required deployment secrets are `VPS_HOST`, `VPS_PORT`, `VPS_USER`, and
`VPS_SSH_KEY`. The workflow validates that those names are present before
starting SSH and never prints their values.

## Next Milestones

1. Phase 1 of the [Strategic Implementation Roadmap](docs/ophelia-strategic-implementation-roadmap.md) has landed: plan digest cards, expanded `ship doctor`, and command catalog examples.
2. Phase 2 has landed: workflow run preview plus shared plan/receipt search aliases.
3. Phase 3 has landed: durable state refresh/summary plus structured drift snapshots, findings, and remediation commands.
4. Phase 4 has landed: resumable, receipt-backed workflow orchestration with pause/resume/cancel and confirmation-gated mutating nodes.
5. Phase 5 has landed: GitHub App and secret provider contracts, provider-aware GitHub provisioning, local GitHub drift observations, and provider doctor checks.
6. Phase 6 has landed: multi-host inventory, host readiness, and app placement planning.
7. The read-only live readiness lane is available now for real staging/prod inspection without mutation.
8. The fixture app suite is available for deterministic multi-app readiness, placement, drift, backup, restore, and provider testing.
9. Phase 7 has landed: metadata-only plugin contracts, trusted-directory discovery, plugin catalog/validation commands, API/Lumen discovery, and fixture plugin coverage.
10. Phase 8 has landed: a read-only Lumen console payload over apps, approvals, workflows, plugins, quick actions, and fixture live-state smoke coverage.
11. Phase 9 has landed: a read-only production hardening report and fixture go/no-go drill.
12. Phase 10 has landed: fixture-backed live drill profiles for repeatable read-only staging/prod rehearsal scenarios.
13. Phase 11 has landed: local live-test profile baselines and the first read-only file-based live test against `~/ophelia-runtime`.
14. Phase 12 has landed: focused live hydration reports now turn one blocked app baseline into ordered runtime/env/secret-name/release/host evidence steps without mutation.
15. Phase 13 has landed: live hydration scaffolds now generate non-secret evidence templates before any live runtime/probe work.
16. Phase 14 has landed: hydration evidence validation now checks scaffold/evidence directories without promotion or value emission.
17. Phase 15 has landed: a no-probe gate now combines hydration and evidence validation before suggesting opt-in probe commands.
18. Phase 16 has landed: a read-only promotion plan now turns reviewed evidence kits into hashed source and target checklists without copying files.
19. Phase 17 has landed: a committed reviewed fixture evidence kit now rehearses validation, promotion planning, and probe review without live values.
20. Phase 18 has landed: the first bounded Quark staging live snapshot attempt created the empty runtime app root, wrote the review scaffold, and recorded the remaining real-evidence blockers.
21. Phase 19 has landed: the Quark staging runtime env now has non-secret structural keys and name-only GitHub observations for the three verified Prism staging secrets.
22. Phase 20 has landed: active guidance now treats Ophelia as the source-of-truth contract, uses synthetic fixtures as the development test substrate, and leaves old deployments as legacy inventory until an explicit migration or deployment phase.
23. Phase 21 has landed: app adoption planning now checks future app repos against the Ophelia contract without collecting live values or mutating product code.
24. Phase 22 has landed: committed future-app adoption fixture repos now validate the Ophelia repo contract without using legacy products as examples.
25. Phase 23 has landed: pack validation, pack explanation, export command summaries, and adoption-embedded pack validation now scrub secret-shaped command literals.
26. Phase 24 has landed: `pack init` now writes executable hook/check scripts and adoption planning reports non-executable required scripts as warnings.
27. Phase 25 has landed: `pack init --include-manifest` now previews or writes valid service/static `.ophelia.yml` bootstraps for app repos.
28. Phase 26 has landed: `ship open-source audit` now provides a read-only public-release hygiene gate and documents the license/readiness path.
29. Phase 27 has landed: public examples, tests, generated domains, and shared Caddy static mount defaults no longer assume private hostnames or one operator path.
30. Next work: use adoption plans for future app repos and retained products only when we are ready to migrate or deploy them through Ophelia.

## Deploy Flows

### Operator-first flow

Use this for initial setup, testing, or one-off deploys from your machine:

```bash
./cli/ship deploy path/to/.ophelia.yml --plan
./cli/ship deploy path/to/.ophelia.yml --host kyle@209.74.71.165 --ssh-port 22022 --apply
```

Production manifests require a confirmation token from `ship deploy --plan`
before local apply. Mutating operator commands follow the same dry-run-first
shape: plan, inspect the report, then pass the matching `--confirm` token.

### Operator command groups

- `ship validate|render|explain|diff|deploy --plan` for manifest inspection.
- `ship pack validate|explain` for portable app pack contracts, data declarations, host requirements, and movement readiness.
- `ship env diff`, `ship backup status`, and `ship app readiness` for redacted movement readiness checks.
- `ship app runbook` for generated per-app operator runbooks from the readiness model.
- `ship app adoption plan` for read-only app repo contract adoption before live hydration, provider setup, or deployment work begins.
- `ship app export plan` and `ship app import plan` for read-only app movement planning receipts.
- `ship app export create --confirm <token>` for confirmed metadata/runtime export bundles with redacted env shape, a `.tar` fallback archive, optional `.tar.zst`, and receipts.
- `ship app import apply --confirm <token>` for isolated rehearsal import previews that do not change active runtime.
- `ship app restore-drill plan|apply` for isolated artifact/listability drill receipts.
- `ship app cutover plan|apply` for confirmed cutover checkpoint receipts; Caddy and DNS are not mutated by the checkpoint.
- `ship app traffic plan|apply` for production traffic automation intent, readiness gates, optional target health checks, checkpoint receipts, explicit file-backed provider execution, and gated Cloudflare DNS updates when `--provider-config` and `--execute-provider-mutation` are both supplied.
- `ship app traffic rollback plan|apply` for receipt-backed rollback of file provider traffic changes when previous DNS/Caddy state was captured.
- `ship app isolation plan` for per-app network compatibility planning; manifests can opt into `networking.internal: per-app`.
- `ship host inventory`, `ship host readiness`, and `ship app placement` for read-only host and placement intelligence.
- `ship live-readiness run` for a read-only aggregate over real runtime/manifests, provider observations, host readiness, placement, observability, secrets, and drift. HTTP and Docker probes are opt-in.
- `make validate-fixtures` and `make live-readiness-fixtures` for the committed synthetic app suite. The live-readiness fixture target uses `--allow-blocked` because one fixture is intentionally incomplete.
- `make validate-adoption-fixtures` for future-app repo contract fixtures that exercise `ship app adoption plan`.
- `ship live-drills list|run|run-all` for named read-only drill profiles over live-readiness and optional hardening. `make live-drills-fixtures` runs the committed fixture profiles and validates their expected mixed states.
- `make live-hydration-reviewed-fixture` for a committed reviewed Postgres evidence kit that validates cleanly and produces a read-only promotion checklist before probe review.
- `ship live-hydration report` for one-app, read-only baseline evidence gaps before enabling probes. Use fixture profiles first, then add a product-specific profile only for an approved migration or deployment phase.
- `ship live-hydration scaffold` for non-secret env, secret-name, release, and host inventory templates in a separate hydration workspace. It is dry-run by default; `--write` writes templates only, not live runtime evidence.
- `ship live-hydration validate-evidence` for read-only scaffold/evidence validation. It blocks malformed kits and secret-looking scaffold values without copying or promoting runtime files.
- `ship live-hydration probe-gate` for a no-probe go/no-go report before opt-in HTTP/Docker checks. It emits suggested commands only when file-based blockers are clear enough for operator review.
- `ship live-hydration promotion-plan` for a read-only checklist of reviewed evidence sources, hashes, and runtime/provider/host target paths. It never copies or promotes files.
- `ship plugins list|catalog|validate` for metadata-only plugin discovery from trusted directories. Plugin descriptors are not executed or injected into the command catalog in this phase.
- `ship lumen console-data` for the read-only Lumen operator console payload. `make lumen-console-fixtures` runs it against the committed fixture suite.
- `ship hardening production-readiness` for a read-only production go/no-go aggregate over live readiness, console data, plugins, workflow templates, state status, command catalog safety, and optional fixture drills. `make production-hardening-fixtures` runs the expected-state fixture drill.
- `ship open-source audit` for a read-only public-release hygiene scan over tracked files, governance files, private host references, scratchpad notes, and private deploy workflow risks. Use `--allow-blocked` while preparing the repo; omit it for the final gate.
- `ship receipts list|show` for local operation receipt browsing.
- `ship state refresh|summary` for the local SQLite state service and app aggregates.
- `ship workflow list|plan|show|run|pause|resume|cancel` for resumable, receipt-backed workflow orchestration. `run --preview` resolves nodes without executing them; mutating nodes pause until `--confirm-node NODE_ID=TOKEN` is supplied from that node's own plan.
- `ship providers github status` and `ship secrets providers` for GitHub App/gh and secret-reference provider readiness without reading or printing secret values.
- `ship app github plan|apply --github-provider auto|gh|github-app` for provider-aware GitHub provisioning; `gh` remains the apply fallback unless a GitHub App runner is configured.
- `ship pack init` for preview-first app pack scaffolding; pass `--include-manifest` to scaffold `.ophelia.yml`, and pass `--write` before it creates files.
- `ship deploy --apply --confirm <token>` for confirmed production apply.
- `ship releases <app>` and `ship release show <app> <release-id>` for release history.
- `ship rollback plan|apply` for file-level rollback from release bundle snapshots.
- `ship backup plan|create` and `ship restore plan|apply` for backup creation and safe restore previews.
- `ship drift <manifest>` and `ship drift all` for runtime/state drift snapshots with severity, owners, remediation commands, and plan candidates.
- `ship inspect conflicts` for cross-manifest platform conflict scanning.
- `ship status`, `ship doctor`, and `ship list` for read-only runtime inspection.
- `ship actions`, `ship jobs`, and `ship api serve` for local job and agent integration.

Most read-only commands accept `--json` for Lumen, automation, and agent integration.

For static apps, sync built assets first or pass a static build directory once that
workflow is added to the app repo.

### App-repo CI flow

Use this after an app repo is set up with GitHub Actions:

1. CI builds the app artifact or image.
2. CI syncs the artifact to the VPS or pushes the image to GHCR.
3. CI SSHes into the VPS and runs `~/ophelia/cli/ship deploy ... --apply`.

If the app has public health checks or operator surfaces that should be part of the release contract, run `~/ophelia/cli/ship deploy ... --apply --verify` instead.
Verification retries by default for first-deploy TLS races and records apply
status separately from verify status. If apply succeeds but external
verification fails, rerun it with `~/ophelia/cli/ship verify <app>`.

For static sites this means:

1. build `dist/`
2. rsync `dist/` into `~/ophelia-runtime/static/<app>/`
3. run `~/ophelia/cli/ship deploy /path/to/.ophelia.yml --runtime-root ~/ophelia-runtime --apply`

## Shared Services

The shared compose is intentionally split into:

- foundation: Postgres and Redis
- edge: Caddy

That lets you migrate the platform in-place without fighting the current
public `~/edge` Caddy container on ports `80/443`.

For apps that still need to sit behind the legacy public edge during
migration, set `services.<name>.host_port` in the manifest. Ophelia will bind
that service to `127.0.0.1:<host_port>` while still attaching it to the shared
Docker networks, so `~/edge` can proxy to it before full ingress cutover.

For host-based ingress that still points at legacy localhost-bound apps,
use tunnel manifests plus route rewrites. The shared Caddy service now exposes
`host.docker.internal` through Docker's host-gateway mapping so Ophelia-managed
ingress can proxy to existing host services without hand-maintained Caddy rules.

## Legacy Prism/Quark Deployments

This section is retained for legacy compatibility only. Quark and old
Prism-backed deployments are not examples for future Ophelia products, and they
should not drive core architecture or tests.

Ophelia can host Prism as a normal service, and it has a Prism-first manifest profile for old Prism-backed products that still need compatibility support.

Use `profile: prism` when a manifest is primarily hosting:

- a Prism runtime
- a Prism-backed product surface such as Quark
- a Prism service that needs a dedicated admin host and a baked Console bundle

The Prism profile adds:

- a `prism:` block for dedicated admin-host and surface metadata
- manifest-relative `env_files` copied into the runtime bundle
- service `mounts` when a product genuinely needs extra runtime files or directories
- optional inferred verification checks for `/health` and `/console`
- automatic routing for `prism.admin_domain` when that host should proxy to the primary Prism service

Use [examples/quark-ops.ophelia.yml](examples/quark-ops.ophelia.yml) and [manifests/quark-ops-staging.ophelia.yml](manifests/quark-ops-staging.ophelia.yml) only when auditing or explicitly deploying that legacy surface. For new work, start from [Ophelia Source Of Truth](docs/ophelia-source-of-truth.md) and the synthetic fixture suite.

For the dedicated Quark hosts:

1. build and push the Prism image from the `prism` repo
2. sync the Ophelia control plane onto the VPS without deleting remote-only state
3. deploy the staging or production manifest with `platform/scripts/deploy-quark-ops.sh`

`platform/scripts/apply-manifests.sh` intentionally skips `quark-ops*.ophelia.yml` unless `OPHELIA_APPLY_QUARK=1` is set. Broad platform deploys should not depend on the private Prism Quark image, GHCR package access, or `ops.begam.in` verification. Explicit Quark deploys can pull the private Prism image by exporting `GHCR_USERNAME` and `GHCR_TOKEN` before running `deploy-quark-ops.sh`.

Example:

```bash
cd /Users/kyle/Developer/projects/web/prism/platform
./scripts/release/build-image.sh ghcr.io/bagelworks/prism:quark-next

cd /Users/kyle/Developer/platforms/ophelia
./platform/scripts/deploy-quark-ops.sh --environment staging --verify
```

`ops.begam.in` is intended to be a root-hosted Prism admin domain for the Quark surface. `/console` remains available as a compatibility fallback on that same host.
