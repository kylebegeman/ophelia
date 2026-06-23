# Product Improvement Findings

Status: selected for phased planning

Date: 2026-06-21

Audience: Kyle, implementation agents, and future operator UI/Ophelia planning work.

This document records the product improvement opportunities selected for the
next Ophelia roadmap. Listing a finding here means it is worth designing and
sequencing. It does not mean the whole item should be implemented in one commit
or without further review.

The implementation plan lives in
[Strategic Implementation Roadmap](ophelia-strategic-implementation-roadmap.md).

## Selection Summary

Selected minor improvements:

1. Plan Digest Cards
2. `ship doctor` expansion
3. Workflow Run Preview
4. Plan/Receipt Search Aliases
5. Command Catalog Examples

Selected major systems:

1. private operator UI Operator Console
2. Full Workflow Orchestrator
3. GitHub App Integration
4. Drift Detection Engine
5. Durable State Service
6. Secrets Integration Layer
7. Multi-Host Inventory And Placement
8. Contracted Plugin System

## Findings

### F-001: Plans Are Correct But Not Always Scan-Friendly

**Selected item:** Minor 1, Plan Digest Cards

Current Ophelia plans carry rich JSON contracts, but operators often need a
short, stable summary of affected app, risk, mutations, blockers, apply command,
and rollback posture before deciding what to do.

**Product opportunity:** Add a shared `digest` block to plans, receipts, and
private operator UI surfaces.

**Why it matters:** This improves operator confidence, agent planning, private operator UI
card rendering, and approval flows without changing the underlying safety model.

**Roadmap phase:** Phase 1

**Implementation status:** Landed in changelog record
[`0019`](../../changelog/0019-phase-1-operator-readability-and-discovery.md).

### F-002: Local Setup And Runtime Health Need One Diagnostic Entry Point

**Selected item:** Minor 2, `ship doctor` expansion

Ophelia already has diagnostic commands, but setup readiness is spread across
self-test, status, providers, schema, GitHub provisioning, runtime checks, and
policy surfaces.

**Product opportunity:** Expand `ship doctor` into a prioritized diagnostic
report that checks local install health, runtime root, manifests, Docker, `gh`
auth, provider config, state DB freshness, and docs/catalog consistency.

**Why it matters:** A single command lowers setup friction and gives both humans
and agents a reliable first step when something feels wrong.

**Roadmap phase:** Phase 1

**Implementation status:** Landed in changelog record
[`0019`](../../changelog/0019-phase-1-operator-readability-and-discovery.md).

### F-003: Workflow Execution Needs A Safe Preview Between Plan And Run

**Selected item:** Minor 4, Workflow Run Preview

`ship workflow run` can execute non-mutating nodes, but operators need a final
resolved preview that shows substitutions, runnable nodes, blocked nodes,
dependency skips, and local executable resolution before execution.

**Product opportunity:** Add `ship workflow run --preview` as a no-execution
receipt/report.

**Why it matters:** This reduces friction in graph execution and becomes the
foundation for a later full workflow orchestrator.

**Roadmap phase:** Phase 2

**Implementation status:** Landed in changelog record
[`0020`](../../changelog/0020-phase-2-operation-aliases-and-workflow-preview.md).

### F-004: Receipt And Plan References Are Too Manual

**Selected item:** Minor 5, Plan/Receipt Search Aliases

Many commands require exact operation IDs or receipt paths. That is precise but
slow during rollback, traffic, restore, and workflow sessions.

**Product opportunity:** Add a shared operation reference resolver for aliases
such as `latest`, `latest:<app>`, `latest:<operation>`, short ID prefixes, and
stable receipt selectors.

**Why it matters:** Faster recovery and less copy/paste while preserving exact
resolution in receipts.

**Roadmap phase:** Phase 2

**Implementation status:** Landed in changelog record
[`0020`](../../changelog/0020-phase-2-operation-aliases-and-workflow-preview.md).

### F-005: The Command Catalog Needs Examples For Humans And Agents

**Selected item:** Minor 8, Command Catalog Examples

The command catalog carries schemas, risk, and output metadata, but it lacks
copyable examples and common workflow snippets.

**Product opportunity:** Add `examples` to command descriptors and surface them
in `ship commands catalog`, private operator UI action descriptors, and docs.

**Why it matters:** Better discoverability, safer agent tool use, and lower
operator learning cost.

**Roadmap phase:** Phase 1

**Implementation status:** Landed in changelog record
[`0019`](../../changelog/0019-phase-1-operator-readability-and-discovery.md).

### F-006: private operator UI Needs A Real Operator Console Over Ophelia Contracts

**Selected item:** Major 1, private operator UI Operator Console

Ophelia now exposes machine-readable contracts, but operators still assemble
status, readiness, workflow, traffic, observability, and receipts mostly through
CLI output.

**Product opportunity:** Build a operator console with app inventory, health,
readiness, traffic state, workflow previews/runs, approvals, and receipts.

**Why it matters:** This turns Ophelia from a powerful CLI substrate into a
cohesive operator experience while keeping execution deterministic in Ophelia.

**Roadmap phase:** Phase 8

**Implementation status:** Landed in
[`0028`](../../changelog/0028-phase-8-operator-console-payload.md). Ophelia now exposes
`ship lumen console-data` and `GET /lumen/console-data`, a read-only console
payload with overview cards, app rows, approval queue metadata, workflow
summaries, plugin inventory, quick actions, source metadata, and fixture smoke
coverage. private operator UI rendering remains outside this repo.

### F-007: Workflows Need To Become Resumable End-To-End Operations

**Selected item:** Major 2, Full Workflow Orchestrator

Before Phase 4, workflow execution was intentionally limited to non-mutating
nodes. Real app movement and incident flows need resumability, per-node plans,
confirmation, pause/resume, and rollback context.

**Product opportunity:** Add a workflow orchestrator that can run read-only
nodes, pause at mutating nodes, request confirmation, record per-node receipts,
resume safely, and stop on policy failures.

**Why it matters:** This is the bridge from individual commands to guided app
movement, incident response, and agent-executable runbooks.

**Roadmap phase:** Phase 4

**Implementation status:** Landed in
[`0022`](../../changelog/0022-phase-4-resumable-workflow-orchestrator.md). The local
orchestrator now persists workflow state, supports preview/run/pause/resume/cancel,
stores per-node status and receipts, and confirmation-gates mutating nodes.
Live GitHub, secrets, and production provider value integration remains a
separate later phase.

### F-008: GitHub Provisioning Should Move Beyond Local `gh`

**Selected item:** Major 3, GitHub App Integration

The current token-gated GitHub apply path shells out to `gh`. That is useful,
but it depends on local auth and has limited drift visibility.

**Product opportunity:** Add a GitHub App integration that can provision repos,
environments, branch protection, webhooks, status checks, and release policies
through a first-class API adapter.

**Why it matters:** Better reliability, clearer permissions, richer errors, and
the ability to react to GitHub events.

**Roadmap phase:** Phase 5

**Implementation status:** Landed in
[`0023`](../../changelog/0023-phase-5-github-app-and-secret-provider-contracts.md).
GitHub provisioning now selects a provider contract (`auto`, `gh`, or
`github-app`), exposes GitHub App configuration readiness without reading key
values, keeps `gh` as the fallback apply path, and lets local GitHub observation
files feed drift checks.

### F-009: Drift Should Become A Product-Level Signal

**Selected item:** Major 4, Drift Detection Engine

Ophelia has drift-related commands and runtime comparisons, but drift is not yet
a durable, cross-surface signal spanning manifests, runtime files, GitHub, DNS,
Caddy, backups, state DB, and provider integrations.

**Product opportunity:** Build a drift engine that records snapshots, compares
desired and observed state, emits plans to reconcile, and feeds private operator UI.

**Why it matters:** Silent operational drift is one of the highest long-term
risks in VPS platforms.

**Roadmap phase:** Phase 3

**Implementation status:** Phase 3 foundation landed in changelog
[`0021`](../../changelog/0021-phase-3-state-service-and-drift-engine.md). `ship drift`
now emits bounded snapshots, severity-sorted findings, owners, remediation
commands, and plan candidates. Local comparisons cover rendered runtime files,
release metadata, env shape, state index freshness, backup/restore evidence,
observability schedule artifacts, and receipt-backed traffic/provider state.
GitHub observations now support local observation files from Phase 5 provider
contracts. Missing observation data still remains informational; observed
GitHub settings can produce drift findings for branch protection, environments,
labels, workflows, status checks, and secret names.

### F-010: The State DB Should Become A Durable Query Service

**Selected item:** Major 6, Durable State Service

The SQLite read model exists, but dashboards and future UIs still risk repeated
filesystem scans and inconsistent query behavior.

**Product opportunity:** Promote state into a durable local service with
migrations, indexing, refresh jobs, query APIs, and stale-data diagnostics.

**Why it matters:** This improves performance, consistency, private operator UI ergonomics,
historical analytics, and long-term maintainability.

**Roadmap phase:** Phase 3

**Implementation status:** Phase 3 foundation landed in changelog
[`0021`](../../changelog/0021-phase-3-state-service-and-drift-engine.md). The state DB
schema is now versioned with refresh metadata and freshness checks. Operators
can run `ship state refresh` to rebuild the local index, `ship state summary` to
read app aggregates from SQLite without a scan, and operator UI/API consumers can use
`GET /state/summary`.

### F-011: Secret Presence Needs Integrations Without Storing Secrets

**Selected item:** Major 8, Secrets Integration Layer

Ophelia has strong redaction discipline, but operators still need a safer way to
verify secret presence across GitHub environments, local runtime env, 1Password,
Doppler, SOPS, Vault, or future private operator UI vaults.

**Product opportunity:** Add a secret-reference integration layer that validates
presence and metadata only, never values.

**Why it matters:** Most deployment failures involve secret/config drift, and
the platform should make that observable without weakening the no-secret-value
invariant.

**Roadmap phase:** Phase 5

**Implementation status:** Landed in
[`0023`](../../changelog/0023-phase-5-github-app-and-secret-provider-contracts.md).
`ship secrets providers` reports key names, provider locations, presence
booleans, freshness metadata, and blockers across local runtime env, GitHub
environment observations, and SOPS file refs. It never emits values.

### F-012: Multi-Host Operations Need Inventory And Placement Intelligence

**Selected item:** Major 9, Multi-Host Inventory And Placement

Ophelia can reason about app readiness and movement, but it does not yet model
multiple hosts as capacity and capability targets.

**Product opportunity:** Add host inventory, capability snapshots, app placement
requirements, and placement recommendations.

**Why it matters:** This unlocks safer migrations, failover planning, cost
optimization, and future automated movement.

**Roadmap phase:** Phase 6

**Implementation status:** Landed in
[`0024`](../../changelog/0024-phase-6-host-inventory-and-placement-planning.md).
`ship host inventory`, `ship host readiness`, and `ship app placement` now
provide read-only host records, readiness checks, placement requirements, and
recommendations. The `move-app` workflow consumes placement before export
planning, and private operator UIs have host inventory and placement surfaces.

### F-013: Extension Points Need Contracts Before The Platform Grows Too Wide

**Selected item:** Major 10, Contracted Plugin System

Templates, providers, policies, workflows, secrets, and host adapters will keep
growing. Without a plugin contract, core Ophelia can become a collection of
special cases.

**Product opportunity:** Define versioned plugin contracts for templates,
workflow templates, policy packs, provider adapters, secret providers, and host
inventory adapters.

**Why it matters:** A plugin boundary preserves maintainability and lets future
integrations grow without destabilizing core safety contracts.

**Roadmap phase:** Phase 7

**Implementation status:** Landed in
[`0027`](../../changelog/0027-phase-7-plugin-contracts.md). Ophelia now supports
metadata-only plugin manifests, trusted-directory discovery, validation,
`ship plugins list|catalog|validate`, `/plugins`, private operator UI capability exposure,
and fixture plugin coverage. Runtime plugin execution remains deferred.

## Prioritization Notes

Highest ROI near-term:

- F-001 Plan Digest Cards
- F-002 `ship doctor` expansion
- F-004 Plan/Receipt Search Aliases
- F-005 Command Catalog Examples
- F-009 Drift Detection Engine foundation (landed)
- F-008 GitHub App provider contracts (landed)
- F-011 Secret provider contracts (landed)

Most strategic long-term:

- F-006 private operator UI Operator Console
- F-007 Full Workflow Orchestrator (local orchestrator landed; live
  integrations continue in later phases)
- F-010 Durable State Service
- F-009 Drift Detection Engine external provider expansion
- F-012 Multi-Host Inventory And Placement
- F-013 Contracted Plugin System

Fastest visible wins:

- Add `digest` blocks.
- Add command examples.
- Add workflow run preview.
- Add alias resolution for latest receipt selectors.
- Expand doctor with GitHub/provider/state checks.
- Run `ship state refresh` and use `ship state summary` for local app
  aggregates.
- Use `ship workflow pause|resume|cancel` for local workflow control.

## Supporting Test Infrastructure

The committed [Fixture App Suite](../../fixture-app-suite.md) supports the roadmap by
providing deterministic multi-app inputs for readiness, live-readiness,
placement, drift, backup, restore, provider, secret-provider, plugin, private operator UI
console, and production-hardening behavior. It does not change the selected
product scope, but it lowers implementation risk by making mixed operational
states reproducible without production data.

The [Production Hardening Report](../../production-hardening.md) adds a read-only
go/no-go aggregate over live readiness, operator console data, plugin validation,
workflow availability, state status, command catalog safety, and fixture drills.
It is the current bridge between fixture-backed contract work and future
authenticated live provider probes or production migration rehearsals.

[Live Drill Profiles](../../live-drill-profiles.md) add named, reusable expected-state
scenarios on top of the fixture suite and hardening report. They are the
preferred way to keep dummy micro-app states, focused app drills, and later
staging/prod profile baselines reproducible.

[Live Hydration Reports](../../live-hydration.md) add the next live-testing step:
one-app evidence gaps for runtime paths, env-key presence, secret-name
observations, release metadata, host capabilities, and drift review. They keep
the live path practical by turning a broad no-go baseline into ordered,
read-only steps before probes or production rehearsals.

Live hydration scaffolds extend that path with template-only evidence kits for
one app. They are dry-run by default and write only to a separate hydration
workspace, which helps operators collect real env shape, provider observations,
release metadata, and host capability facts without accidentally changing
readiness inputs.

Hydration evidence validation adds the next guardrail: scaffold/evidence
directories can be checked for missing files, malformed JSON, leftover
placeholders, missing required names, and secret-shaped values before anything
is copied into runtime or provider observation paths.

The no-probe gate adds a final file-based go/no-go report before live checks. It
keeps probes disabled, combines hydration and evidence validation, and emits
exact probe commands only when there are no blockers for an operator to review.

The live hydration promotion plan adds the next review artifact before runtime
mutation: it maps reviewed evidence-kit files to consumed runtime, provider
observation, release metadata, and host inventory targets with source hashes and
target existence checks. It deliberately remains read-only, so it improves
handoff quality without introducing an automated promotion path yet.

The reviewed fixture evidence kit makes that handoff testable without live
values. The fixture Postgres app now carries a committed evidence directory that
validates cleanly, produces a promotion checklist, and moves the probe gate to
operator review without executing probes.

The first Legacy Console staging snapshot attempt clarified the real-live boundary: it
is safe to create the empty runtime app root and scaffold review templates, but
env values, provider-observed secret names, active release metadata, and host
capability facts still need truthful external sources before any probe or
promotion step should proceed.

The follow-up Legacy Console staging evidence pass moved the real baseline forward
without crossing that boundary: non-secret structural env keys and verified
GitHub secret-name mappings were written to the local runtime, while
database/Redis values, release metadata, and host facts remain blocked until
truthful sources are available.

That Legacy Console work is historical legacy evidence, not the forward product model.
The active improvement direction is fixture-first and contract-first: Ophelia
defines the right runtime, safety, receipt, readiness, and migration behavior,
then retained products such as `retained-app` and `retained-secondary-app` adapt to that
contract during explicit adoption or deployment phases.
