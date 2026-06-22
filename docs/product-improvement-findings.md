# Product Improvement Findings

Status: selected for phased planning

Date: 2026-06-21

Audience: Kyle, implementation agents, and future Lumen/Ophelia planning work.

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

1. Lumen Operator Console
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
Lumen surfaces.

**Why it matters:** This improves operator confidence, agent planning, Lumen
card rendering, and approval flows without changing the underlying safety model.

**Roadmap phase:** Phase 1

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

### F-005: The Command Catalog Needs Examples For Humans And Agents

**Selected item:** Minor 8, Command Catalog Examples

The command catalog carries schemas, risk, and output metadata, but it lacks
copyable examples and common workflow snippets.

**Product opportunity:** Add `examples` to command descriptors and surface them
in `ship commands catalog`, Lumen action descriptors, and docs.

**Why it matters:** Better discoverability, safer agent tool use, and lower
operator learning cost.

**Roadmap phase:** Phase 1

### F-006: Lumen Needs A Real Operator Console Over Ophelia Contracts

**Selected item:** Major 1, Lumen Operator Console

Ophelia now exposes machine-readable contracts, but operators still assemble
status, readiness, workflow, traffic, observability, and receipts mostly through
CLI output.

**Product opportunity:** Build a Lumen console with app inventory, health,
readiness, traffic state, workflow previews/runs, approvals, and receipts.

**Why it matters:** This turns Ophelia from a powerful CLI substrate into a
cohesive operator experience while keeping execution deterministic in Ophelia.

**Roadmap phase:** Phase 8

### F-007: Workflows Need To Become Resumable End-To-End Operations

**Selected item:** Major 2, Full Workflow Orchestrator

Current workflow execution is intentionally limited to non-mutating nodes.
Real app movement and incident flows need resumability, per-node plans,
confirmation, pause/resume, and rollback context.

**Product opportunity:** Add a workflow orchestrator that can run read-only
nodes, pause at mutating nodes, request confirmation, record per-node receipts,
resume safely, and stop on policy failures.

**Why it matters:** This is the bridge from individual commands to guided app
movement, incident response, and agent-executable runbooks.

**Roadmap phase:** Phase 4

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

### F-009: Drift Should Become A Product-Level Signal

**Selected item:** Major 4, Drift Detection Engine

Ophelia has drift-related commands and runtime comparisons, but drift is not yet
a durable, cross-surface signal spanning manifests, runtime files, GitHub, DNS,
Caddy, backups, state DB, and provider integrations.

**Product opportunity:** Build a drift engine that records snapshots, compares
desired and observed state, emits plans to reconcile, and feeds Lumen.

**Why it matters:** Silent operational drift is one of the highest long-term
risks in VPS platforms.

**Roadmap phase:** Phase 3

### F-010: The State DB Should Become A Durable Query Service

**Selected item:** Major 6, Durable State Service

The SQLite read model exists, but dashboards and future UIs still risk repeated
filesystem scans and inconsistent query behavior.

**Product opportunity:** Promote state into a durable local service with
migrations, indexing, refresh jobs, query APIs, and stale-data diagnostics.

**Why it matters:** This improves performance, consistency, Lumen ergonomics,
historical analytics, and long-term maintainability.

**Roadmap phase:** Phase 3

### F-011: Secret Presence Needs Integrations Without Storing Secrets

**Selected item:** Major 8, Secrets Integration Layer

Ophelia has strong redaction discipline, but operators still need a safer way to
verify secret presence across GitHub environments, local runtime env, 1Password,
Doppler, SOPS, Vault, or future Lumen vaults.

**Product opportunity:** Add a secret-reference integration layer that validates
presence and metadata only, never values.

**Why it matters:** Most deployment failures involve secret/config drift, and
the platform should make that observable without weakening the no-secret-value
invariant.

**Roadmap phase:** Phase 5

### F-012: Multi-Host Operations Need Inventory And Placement Intelligence

**Selected item:** Major 9, Multi-Host Inventory And Placement

Ophelia can reason about app readiness and movement, but it does not yet model
multiple hosts as capacity and capability targets.

**Product opportunity:** Add host inventory, capability snapshots, app placement
requirements, and placement recommendations.

**Why it matters:** This unlocks safer migrations, failover planning, cost
optimization, and future automated movement.

**Roadmap phase:** Phase 6

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

## Prioritization Notes

Highest ROI near-term:

- F-001 Plan Digest Cards
- F-002 `ship doctor` expansion
- F-004 Plan/Receipt Search Aliases
- F-005 Command Catalog Examples
- F-009 Drift Detection Engine foundation

Most strategic long-term:

- F-006 Lumen Operator Console
- F-007 Full Workflow Orchestrator
- F-010 Durable State Service
- F-012 Multi-Host Inventory And Placement
- F-013 Contracted Plugin System

Fastest visible wins:

- Add `digest` blocks.
- Add command examples.
- Add workflow run preview.
- Add alias resolution for latest receipt selectors.
- Expand doctor with GitHub/provider/state checks.
