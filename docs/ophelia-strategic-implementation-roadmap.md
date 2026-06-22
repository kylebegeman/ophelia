# Ophelia Strategic Implementation Roadmap

Status: selected next-stage roadmap, Phases 1-15 landed plus read-only live readiness and fixture testing lanes

Date: 2026-06-21

Audience: implementation agents, Kyle, and future Lumen/Ophelia planning work.

This roadmap sequences the selected product improvements from
[Product Improvement Findings](product-improvement-findings.md). It is designed
for phased implementation with milestone commits between phases. The order is
dependency-driven: foundational contracts, state, and safety primitives come
before orchestration, integrations, placement, plugin boundaries, and Lumen UI.

## Scope

Selected minor improvements:

- Plan Digest Cards
- `ship doctor` expansion
- Workflow Run Preview
- Plan/Receipt Search Aliases
- Command Catalog Examples

Selected major systems:

- Lumen Operator Console
- Full Workflow Orchestrator
- GitHub App Integration
- Drift Detection Engine
- Durable State Service
- Secrets Integration Layer
- Multi-Host Inventory And Placement
- Contracted Plugin System

## Current Foundation

The following foundations already exist and should be reused instead of
rebuilt:

- Standard plan, report, receipt, and error envelopes in `operation_schema.py`.
- Central redaction and propagated data redaction.
- Command catalog and Lumen action descriptors.
- App factory, scaffold apply, and token-gated GitHub provisioning via `gh`.
- GitHub provider contracts with GitHub App metadata and `gh` fallback.
- Secret provider reports for runtime env, GitHub environment observations, and
  SOPS file refs.
- Workflow graph planning, preview, pause/resume/cancel, and confirmation-gated
  mutating workflow nodes.
- Receipt timeline and state DB read model.
- Policy engine and readiness aggregation.
- Traffic status, traffic provider plans, and rollback receipts.
- Observability status, export, dashboard aggregates, and schedule runner.
- Backup, restore verification, export/import, and restore drill receipts.
- Read-only host inventory, host readiness, app placement planning, and the
  live readiness aggregate lane.
- A committed fixture app suite for deterministic multi-app readiness,
  placement, drift, backup, restore, provider, and secret-provider testing.
- Metadata-only plugin contracts, trusted-directory discovery, validation, and
  plugin catalog surfaces.
- Read-only Lumen operator console payload over dashboard, app rows, approvals,
  workflows, plugins, and quick actions.
- Read-only production hardening report that composes live readiness, console
  data, plugin validation, workflow availability, state status, command catalog
  safety, and fixture drills into one go/no-go payload.
- Fixture-backed live drill profiles for repeatable read-only live-readiness and
  hardening rehearsal scenarios.
- Local live-test profile baselines and first read-only file-based live test
  results against `~/ophelia-runtime`.
- Focused live hydration reports that turn one blocked live app baseline into
  ordered runtime/env/secret-name/release/host evidence steps before probes.
- Live hydration scaffold templates for collecting one app's non-secret
  runtime, provider observation, release, and host capability evidence.
- Read-only hydration evidence validation for scaffold directories before any
  runtime promotion or probe enablement.
- A no-probe gate that combines hydration and evidence validation before
  suggesting opt-in HTTP/Docker probe commands.

## Implementation Rules

- Do not mutate production VPS state during implementation or tests.
- Every risky operation remains plan-first with a confirmation token and a
  receipt.
- JSON contracts must stay stable and documented.
- Secret values must never be stored, printed, or included in receipts.
- Lumen UI work should consume Ophelia contracts; Ophelia remains the
  deterministic executor.
- Each phase gets a changelog record and a milestone commit.
- If a phase changes public JSON shape, update docs and tests in that phase.

## Phase 0: Roadmap Documentation And Contract Inventory

**Goal:** Establish the selected scope, findings, phase order, and routing docs.

**Includes:**

- Product findings document.
- Strategic implementation roadmap.
- README and architecture routing updates.
- Agent prompt updates.
- Changelog record for the roadmap documentation.

**Dependencies:** None.

**Milestone commit:** `docs: add strategic Ophelia roadmap`

**Definition of done:**

- Docs link to the new roadmap from README and architecture docs.
- Changelog index includes the roadmap record.
- Docs checks pass.

## Phase 1: Operator Readability And Discoverability

**Status:** Landed in changelog record
[`0019`](changelog/0019-phase-1-operator-readability-and-discovery.md).

**Goal:** Make existing plans and commands easier to understand before adding
larger orchestration.

**Implements:**

- Minor 1: Plan Digest Cards
- Minor 2: `ship doctor` expansion
- Minor 8: Command Catalog Examples

**Work items:**

- Add a shared `digest` helper that can be attached to plan and receipt
  envelopes.
- Attach digests to canonical plan/report/receipt envelopes and the legacy
  deploy plan surface.
- Cover deploy, app factory, traffic, workflow, backup/restore, GitHub
  provisioning, and observability through the shared envelope path.
- Extend `CommandDescriptor` with `examples`.
- Surface examples through `ship commands catalog`, Lumen action descriptors,
  and docs.
- Expand `ship doctor` to check:
  - Python/import health
  - runtime root writability and layout
  - manifest registry parse health
  - state DB freshness
  - command catalog completeness
  - docs/changelog consistency
  - Docker availability
  - `gh` authentication and GitHub provisioning prerequisites
  - provider config validity
  - redaction safety smoke checks

**Dependencies:** Existing operation envelopes, command catalog, state DB, and
provider validation.

**Milestone commit:** `feat: improve plan digests and command discovery`

**Definition of done:**

- New digest fields are documented and covered by tests.
- Existing JSON consumers remain compatible.
- `ship doctor --json` is stable and actionable.
- Command catalog examples are present for high-value commands first.

## Phase 2: Operation Reference Ergonomics And Workflow Preview

**Status:** Landed in changelog record
[`0020`](changelog/0020-phase-2-operation-aliases-and-workflow-preview.md).

**Goal:** Make operation continuation safer and less copy/paste heavy.

**Implements:**

- Minor 4: Workflow Run Preview
- Minor 5: Plan/Receipt Search Aliases
- Foundation for Major 2: Full Workflow Orchestrator

**Work items:**

- Add a shared `operation_refs.py` resolver for:
  - `latest`
  - `latest:<app>`
  - `latest:<operation>`
  - short operation ID prefixes
  - receipt path passthrough
  - ambiguity reporting
- Use the resolver in traffic rollback, restore, receipts, workflow, and state
  query commands.
- Add `ship workflow run --preview`.
- Preview resolved command arrays, substitutions, dependency outcomes, blockers,
  skipped nodes, and local executable paths without running nodes.
- Add digest output to workflow preview and run receipts.

**Dependencies:** Receipt timeline and workflow run contracts.

**Milestone commit:** `feat: add operation aliases and workflow preview`

**Definition of done:**

- Alias resolution is deterministic and returns explicit ambiguity errors.
- Existing exact IDs and paths keep working.
- Workflow preview never executes a node.
- Tests cover ambiguous aliases and unresolved placeholders.

## Phase 3: Durable State Service And Drift Engine

**Status:** Landed in changelog record
[`0021`](changelog/0021-phase-3-state-service-and-drift-engine.md).

**Goal:** Promote state and drift from ad hoc scans into durable, queryable
product signals.

**Implements:**

- Major 4: Drift Detection Engine
- Major 6: Durable State Service

**Work items:**

- Version the state DB schema with explicit migrations.
- Add a refresh/index command that updates apps, manifests, receipts,
  backups, restore drills, releases, and local receipt-backed traffic and
  observability snapshots.
- Add stale-data metadata and freshness checks.
- Add `ship state refresh`, `ship state summary`, and `GET /state/summary` as
  local state-service surfaces.
- Build drift snapshots for:
  - manifest versus rendered runtime files
  - runtime root versus state DB index
  - GitHub desired settings versus observed settings
  - DNS/Caddy desired state versus provider observations
  - backup/restore requirements versus receipts
  - observability schedule status versus current manifest registry
- Emit drift reports with severity, owner, remediation commands, and plan
  candidates.
- Record missing external observations as bounded `not_observed` snapshot slots;
  Phase 5 adds local GitHub observation contracts, and later provider adapters
  can add authenticated live probes.

**Dependencies:** Digest fields, operation references, state DB read model,
traffic status, GitHub provisioning contracts, observability schedule runs.

**Milestone commit:** `feat: add durable state refresh and drift engine`

**Definition of done:**

- State refresh is idempotent and safe to run repeatedly.
- Drift reports are redacted and bounded.
- Lumen dashboard can read aggregates from the state service instead of
  re-scanning every file.
- Tests cover stale index behavior and drift severity ordering.

## Phase 4: Full Workflow Orchestrator

Status: landed in [`0022`](changelog/0022-phase-4-resumable-workflow-orchestrator.md).

**Goal:** Turn workflow graphs into resumable, receipt-backed operations.

**Implements:**

- Major 2: Full Workflow Orchestrator

**Work items:**

- Add workflow run state persisted in the durable state service.
- Add node lifecycle states: `planned`, `previewed`, `ready`, `running`,
  `paused_for_confirmation`, `succeeded`, `failed`, `blocked`, `skipped`,
  `rolled_back`.
- Allow mutating nodes only through their own plan/confirm/apply contracts.
- Store per-node plan IDs, receipt IDs, blockers, warnings, artifacts, and
  rollback metadata.
- Add pause/resume/cancel commands.
- Add policy gates before mutating nodes.
- Add workflow templates for:
  - app move
  - incident triage
  - release readiness
  - GitHub provisioning
  - restore rehearsal

**Dependencies:** Workflow preview, operation aliases, durable state service,
policy engine, receipt timeline.

**Milestone commit:** `feat: add resumable workflow orchestrator`

**Definition of done:**

- Mutating workflow nodes never run without matching confirmation.
- Resume is deterministic after process restart.
- Failed nodes stop dependent nodes.
- Lumen can render workflow state from one read model.

**Boundary:** Phase 4 persists and resumes local workflow state and executes
existing typed commands. Full authenticated live GitHub, secrets, and production
provider value integration remains in Phase 5 and later provider phases.

## Phase 5: GitHub App And Secrets Integrations

Status: landed in [`0023`](changelog/0023-phase-5-github-app-and-secret-provider-contracts.md).

**Goal:** Move external integrations from local CLI assumptions to first-class,
redacted provider contracts.

**Implements:**

- Major 3: GitHub App Integration
- Major 8: Secrets Integration Layer

**Work items:**

- Define provider interfaces for GitHub and secret stores.
- Add GitHub App configuration model with installation ID, permissions, and
  repository bindings.
- Keep `gh` as a fallback provider while adding GitHub App as the preferred
  provider.
- Add GitHub drift checks for branch protection, environments, labels,
  workflows, secrets presence, and deployment status checks.
- Add secret reference providers for initial targets:
  - GitHub environments
  - local runtime env shape
  - SOPS file references
  - 1Password/Doppler/Vault as optional later adapters
- Validate presence and metadata only. Never read or emit secret values.
- Add doctor checks for configured providers.

**Dependencies:** Durable state service, drift engine, provider config
validation, redaction, command catalog examples.

**Milestone commit:** `feat: add GitHub app and secret provider contracts`

**Definition of done:**

- GitHub App and `gh` provider paths share plan/apply semantics.
- Secret reports expose key names, location, presence, freshness, and blockers,
  never values.
- Provider failures degrade to structured blockers/warnings.

**Boundary:** Phase 5 adds provider contracts, local observation formats, and
runner hooks. Real production GitHub App HTTP execution and external vault APIs
can plug into these contracts later without changing the CLI envelope shape.

## Phase 6: Multi-Host Inventory And Placement

**Goal:** Make hosts first-class placement targets.

**Implements:**

- Major 9: Multi-Host Inventory And Placement

**Work items:**

- Add host records with provider, region, OS, architecture, memory, disk,
  Docker, Caddy, network, backup, and foundation service capabilities.
- Add read-only host inventory collection.
- Add app placement requirements derived from manifests and runtime history.
- Add placement scoring:
  - hard blockers
  - warnings
  - capacity fit
  - data locality
  - backup/restore readiness
  - provider constraints
- Add `ship host inventory`, `ship host readiness`, and `ship app placement`
  plan surfaces.
- Feed placement results into workflow templates and Lumen.

**Dependencies:** Durable state service, drift engine, secrets provider presence,
observability schedule, readiness reports.

**Milestone commit:** `feat: add host inventory and placement planning`

**Implementation status:** Landed in
[`0024`](changelog/0024-phase-6-host-inventory-and-placement-planning.md).
Phase 6 adds read-only host inventory and readiness reports, app placement
planning and scoring, move-app workflow integration, Lumen surfaces, default
local host metadata, and durable operator documentation. Placement remains a
recommendation surface; it does not reserve hosts or execute migrations.

**Definition of done:**

- Host inventory is read-only by default.
- Placement plans do not mutate hosts.
- App move workflows can consume placement recommendations.

## Parallel Lane: Read-Only Live Readiness

**Goal:** Start using real staging/prod runtime values safely before live
mutation hardening.

**Work items:**

- Aggregate host inventory/readiness, provider status, state status, Lumen
  dashboard data, app readiness, placement, observability, secrets, and drift.
- Keep HTTP and Docker probes disabled unless explicitly requested.
- Report observation file paths for GitHub, secret-provider, host, and
  observability snapshots.
- Prove the lane does not call apply/create/rebuild/refresh operations or write
  state/observability artifacts.

**Contract:** `ship live-readiness run --json` emits
`kind: "ophelia.live_readiness_report"`.

**Boundary:** This lane is for real inspection and gap discovery. Actual live
provider mutations, migration rehearsals, and production applies remain later
work behind explicit plan, confirmation-token, and receipt contracts.

## Parallel Lane: Fixture App Suite

**Goal:** Keep live-readiness and production-hardening work testable without
depending on production data, real provider credentials, or mutable VPS state.

**Work items:**

- Store synthetic micro apps covering static, service, multi-service, tunnel,
  Postgres, Redis, bind-mounted volumes, and an intentionally incomplete app.
- Store synthetic runtime metadata, backup manifests, restore-drill receipts,
  GitHub observations, secret observations, SOPS-shaped key files, host
  inventory, and provider config.
- Add `make validate-fixtures`, `make live-readiness-fixtures`,
  `make live-drills-fixtures`, and `make production-hardening-fixtures`.
- Add `--allow-blocked` to the live-readiness CLI so expected-failure suites can
  return blocked reports while exiting zero.
- Add tests for manifest loading, read-only execution, redaction, expected
  blocked state, placement recommendation coverage, and command catalog
  discoverability.

**Contract:** Fixture values are fake and must never be production data. The
fixture live-readiness command is expected to emit a blocked report because
`fixture-incomplete-app` is deliberately broken.

**Boundary:** The suite is test infrastructure. It does not replace real
staging/prod live-value runs, live probes, authenticated provider adapters, the
Phase 9 production hardening report, or production mutation rehearsals.

## Phase 7: Contracted Plugin System

**Status:** Landed in changelog record
[`0027`](changelog/0027-phase-7-plugin-contracts.md).

**Goal:** Keep Ophelia extensible without turning core modules into integration
sprawl.

**Implements:**

- Major 10: Contracted Plugin System

**Work items:**

- Define plugin manifest schema with name, version, capabilities, commands,
  schemas, examples, safety notes, and compatibility.
- Define plugin interfaces for:
  - app templates
  - workflow templates
  - policy packs
  - provider adapters
  - secret providers
  - host inventory adapters
  - Lumen surface extensions
- Add plugin discovery from an explicit trusted directory.
- Add validation and catalog surfaces.
- Keep plugins disabled by default until validated.
- Add tests proving plugin metadata cannot weaken redaction, mutate without a
  plan, or corrupt command catalog schemas.

**Dependencies:** Command catalog examples, provider contracts, workflow
orchestrator, state service, policy engine.

**Milestone commit:** `feat: add plugin contract and discovery`

**Definition of done:**

- Core Ophelia can list, validate, and reject plugins deterministically.
- Plugins cannot bypass plan/apply or redaction rules.
- Built-in templates/providers can be represented through the same contract over
  time.

**Boundary:** Phase 7 is metadata-only. Ophelia validates plugin manifests and
surfaces capabilities to CLI/API/Lumen, but it does not import, install, execute,
or sandbox plugin code. Runtime plugin execution remains later hardening work.

## Phase 8: Lumen Operator Console

**Status:** Landed in changelog record
[`0028`](changelog/0028-phase-8-lumen-console-mvp.md).

**Goal:** Build the operator cockpit on top of Ophelia contracts.

**Implements:**

- Major 1: Lumen Operator Console

**Work items:**

- Define Lumen views for:
  - app inventory
  - readiness
  - observability and trend history
  - traffic state
  - drift
  - receipts
  - workflow preview/run/resume
  - GitHub and secrets integration health
  - host placement
- Add approval UX for confirmation-token flows.
- Add copyable exact apply commands and digest cards.
- Add Lumen-side filtering and search over the state service.
- Add accessibility and keyboard navigation requirements.
- Keep mutation execution in Ophelia; Lumen collects approval and invokes the
  stable CLI/API/action contract.

**Dependencies:** Digest cards, command examples, operation aliases, durable
state service, drift engine, workflow orchestrator, GitHub/secrets providers,
host placement.

**Milestone commit:** `feat: add Lumen operator console MVP`

**Definition of done:**

- Lumen can inspect and approve high-value Ophelia operations without raw shell
  composition.
- Console views are backed by stable Ophelia JSON contracts.
- Mutating actions show digest, risk, token, expected changes, and rollback
  posture before execution.

**Boundary:** Phase 8 adds the read-only console data contract and fixture
smoke target. Lumen UI rendering lives outside this repo; Ophelia exposes the
bounded payload and keeps mutation execution inside existing plan/confirm/apply
contracts.

## Phase 9: Production Hardening And Migration

**Status:** Landed in changelog record
[`0029`](changelog/0029-phase-9-production-hardening-report.md).

**Goal:** Make the new systems production-ready and migrate existing workflows
onto them.

**Work items:**

- Add a read-only production hardening aggregate over live readiness, Lumen
  console data, plugin validation, workflow availability, state status, command
  catalog safety, and fixture drills.
- Expose `ship hardening production-readiness`, command catalog examples, and
  `GET /hardening/production-readiness`.
- Add `make production-hardening-fixtures` as the deterministic fixture go/no-go
  drill.
- Update operator docs, fixture docs, roadmap, findings, and changelog records.

**Dependencies:** All previous phases.

**Milestone commit:** `chore: add production hardening report`

**Definition of done:**

- Hardening report is read-only and redacted.
- Fixture hardening target exits zero in the expected review state.
- CLI/API/catalog contracts are documented and tested.
- Remaining live mutation and external provider risks are explicit.

**Boundary:** Phase 9 is a hardening gate, not a production mutation executor.
Authenticated live provider probes, production migration rehearsals, performance
work, and compatibility migration notes remain future work behind explicit
operator approval.

## Phase 10: Fixture-Backed Live Drill Profiles

**Status:** Landed in changelog record
[`0030`](changelog/0030-live-drill-profiles.md).

**Goal:** Turn ad hoc fixture and live-readiness commands into reusable named
drill profiles that can later point at real staging/prod roots.

**Work items:**

- Add `fixtures/app-suite/live-drills.yml` with full-suite, Postgres-focused,
  static-focused, and intentionally incomplete app profiles.
- Add a live drill runner that composes live-readiness and optional production
  hardening reports.
- Validate expected aggregate status, app counts, drift counts, app statuses,
  blocked/warning app sets, and hardening go/no-go state.
- Expose `ship live-drills list|run|run-all`, command catalog descriptors,
  `GET /live-drills`, and `GET /live-drills/<profile>`.
- Add `make live-drills-fixtures` and focused regression tests.

**Dependencies:** Live readiness lane, fixture app suite, production hardening
report, command catalog, and local API route manifest.

**Milestone commit:** `feat: add live drill profiles`

**Definition of done:**

- Fixture profiles run read-only and return `status: "ok"` when expected mixed
  states match.
- Missing or mismatched profile expectations block with structured issues.
- CLI/API/catalog/docs surfaces are updated and tested.

**Boundary:** Phase 10 does not add authenticated provider collection or mutate
production. Real staging/prod profiles can reuse the profile file shape, but
operator-reviewed baselines and opt-in probes still come later.

## Phase 11: Read-Only Live Test Baseline

**Status:** Landed in changelog record
[`0031`](changelog/0031-live-test-baseline-and-secret-metadata.md).

**Goal:** Start real live testing with file-based, non-mutating baselines before
running HTTP, Docker, authenticated provider, or mutation probes.

**Work items:**

- Add `config/ophelia-live-drills.yml` with local runtime baseline and Quark
  Ops production/staging focused profiles.
- Run first read-only live baseline against repo manifests and
  `~/ophelia-runtime`.
- Record live baseline findings in `docs/scratchpad/`.
- Preserve secret-provider metadata counts/status in live-readiness output
  without exposing values.

**Dependencies:** Phase 10 live drill profiles, live-readiness lane, local
host/provider configs, and production hardening report.

**Milestone commit:** `chore: add live test baseline profiles`

**Definition of done:**

- Local live profiles load and run without mutation.
- Current blocked live baseline is documented with blocker groups and next safe
  steps.
- Redaction keeps useful secret-provider metadata visible while blocking secret
  values.

**Boundary:** Phase 11 does not run HTTP probes, Docker probes, authenticated
provider collection, state refresh, workflow execution, or production
mutations. Those should wait until a file-based baseline for one target app is
operator-reviewed.

## Phase 12: Focused Live Baseline Hydration

**Status:** Landed in changelog record
[`0032`](changelog/0032-live-baseline-hydration-report.md).

**Goal:** Convert the first broad live no-go result into focused, one-app,
read-only evidence steps before enabling any probes.

**Work items:**

- Add `ship live-hydration report` for app/profile-backed runtime evidence
  audits.
- Compose existing env-shape, secret-provider, placement, readiness, drift, and
  release metadata reports into a compact hydration payload.
- Expose `GET /live-hydration/<profile>`, command catalog examples, and
  `make live-hydration-quark-staging`.
- Preserve blocked JSON status while allowing expected blocked baseline audits
  to exit zero with `--allow-blocked`.
- Document the current Quark staging hydration blockers and safe next steps.

**Dependencies:** Phase 10 live drill profiles, Phase 11 local baselines,
live-readiness, secret-provider metadata, host placement, release metadata, and
drift reports.

**Milestone commit:** `feat: add live baseline hydration report`

**Definition of done:**

- Hydration reports are read-only, redacted, and never create runtime files or
  read secret values.
- Focused fixture and local profile tests cover blocked and warning reports.
- CLI/API/catalog/docs surfaces are updated and tested.
- The next safe live-testing gate is explicit before HTTP, Docker,
  authenticated provider, workflow, or mutation probes.

**Boundary:** Phase 12 does not hydrate runtime files, create env snapshots,
record secret-name observations, refresh state, run probes, call providers, or
execute workflows. It identifies the exact evidence to collect first.

## Phase 13: Live Evidence Scaffold Templates

**Status:** Landed in changelog record
[`0033`](changelog/0033-live-evidence-scaffold-templates.md).

**Goal:** Make the first live evidence collection step repeatable and safe
before touching consumed runtime files or enabling probes.

**Work items:**

- Add `ship live-hydration scaffold` as a dry-run-first companion to hydration
  reports.
- Generate template-only files for env shape, GitHub secret-name observations,
  release metadata, host capabilities, and operator instructions.
- Keep scaffold output under a separate hydration workspace by default, not
  `apps/<app>/env`, `active_release.json`, provider observation paths, or host
  inventory files.
- Add `--write` and `--force` guards for explicit scaffold template writes.
- Expose command catalog examples, `make live-hydration-scaffold-quark-staging`,
  tests, and docs.

**Dependencies:** Phase 12 hydration reports and Phase 11 local profile
baselines.

**Milestone commit:** `feat: add live evidence scaffold templates`

**Definition of done:**

- Dry-run scaffold output is redacted, useful, and writes nothing.
- `--write` creates templates only under the requested scaffold directory.
- Existing scaffold files are not overwritten without `--force`.
- Docs make clear that templates are not readiness evidence until replaced by
  reviewed real observations.

**Boundary:** Phase 13 does not create or edit consumed runtime env files,
release metadata, provider observation files, host inventory, state DB files,
or workflow artifacts. It does not run HTTP, Docker, authenticated provider, or
production probes.

## Phase 14: Hydration Evidence Validation

**Status:** Landed in changelog record
[`0034`](changelog/0034-hydration-evidence-validation.md).

**Goal:** Let operators and agents check a scaffold/evidence directory before
copying any evidence into consumed runtime paths.

**Work items:**

- Add `ship live-hydration validate-evidence`.
- Validate expected scaffold files, env key names, secret-name observation JSON,
  release metadata shape, and required host capability flags.
- Detect missing files, invalid JSON, missing required names, template
  placeholders, and secret-looking values without printing values.
- Add `make live-hydration-validate-evidence-quark-staging` using a temporary
  scaffold directory.
- Update command catalog examples, tests, docs, roadmap, and changelog.

**Dependencies:** Phase 13 scaffold templates.

**Milestone commit:** `feat: validate live hydration evidence`

**Definition of done:**

- Validation is read-only and emits `ophelia.live_hydration_evidence_validation`.
- Missing evidence directories block with structured issue codes.
- Template-only kits return warnings, not promotion.
- Secret-shaped scaffold values block and are not emitted.

**Boundary:** Phase 14 does not promote, copy, or mutate runtime env files,
release metadata, provider observations, host inventory, state DB files, or
workflow artifacts. It does not run probes.

## Phase 15: No-Probe Live Probe Gate

**Status:** Landed in changelog record
[`0035`](changelog/0035-no-probe-live-gate.md).

**Goal:** Add a final file-based go/no-go report before any opt-in HTTP or
Docker probes are run.

**Work items:**

- Add `ship live-hydration probe-gate`.
- Compose hydration report status and evidence validation status into
  `go_no_go: "go" | "review" | "no_go"`.
- Keep `probe_policy` disabled and `probes_executed: false`.
- Emit exact probe commands only when the gate has no blockers.
- Add command catalog examples, `make live-hydration-probe-gate-quark-staging`,
  tests, docs, roadmap, scratchpad, and changelog.

**Dependencies:** Phase 14 evidence validation.

**Milestone commit:** `feat: add no-probe live gate`

**Definition of done:**

- Current Quark staging returns `no_go` without running probes.
- Fixture review cases can return `review` and emit explicit probe commands for
  operator review.
- The gate is read-only, redacted, and never calls HTTP, Docker, providers,
  workflows, or runtime mutation commands.

**Boundary:** Phase 15 does not run the emitted commands. It does not perform
network/Docker probes, provider calls, state refresh, runtime writes, workflow
execution, or production mutation.

## Phase Dependency Graph

```text
Phase 0 docs
  -> Phase 1 digests/catalog/doctor
    -> Phase 2 aliases/workflow preview
      -> Phase 3 state service/drift
        -> Phase 4 workflow orchestrator
        -> Phase 5 GitHub App/secrets
          -> Phase 6 multi-host placement
            -> Phase 7 plugin contracts
              -> Phase 8 Lumen console
                -> Phase 9 hardening
                  -> Phase 10 live drill profiles
                    -> Phase 11 live test baseline
                      -> Phase 12 live hydration reports
                        -> Phase 13 live evidence scaffold templates
                          -> Phase 14 hydration evidence validation
                            -> Phase 15 no-probe live gate
```

Phase 4 and Phase 5 can proceed partly in parallel after Phase 3 if their
shared provider/state contracts are stable.

## Changelog And Commit Practice

Each phase should add one changelog record and one milestone commit after the
phase passes focused verification. Large phases may have internal commits, but
the milestone commit should be the handoff point.

Recommended verification before each milestone commit:

```bash
python3 -m compileall -q src
PYTHONPATH=src python3 -m unittest discover -s tests
git diff --check
PYTHONPATH=src python3 -m ophelia.docs_check
```

Add phase-specific CLI smoke checks whenever a phase exposes new commands.
