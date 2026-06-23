# Ophelia Documentation

Status: active documentation index

This folder is the durable documentation surface for Ophelia. Start here when
you need to understand the product, operate it safely, update an app manifest,
or hand work to an agent.

Historical implementation plans and old handoff prompts live under
[Archive](archive/README.md). They are retained for context, but current work
should be planned from [Roadmap](ROADMAP.md), the change records, and the active
reference docs below.

## Start Here

| Document | Use it for |
| --- | --- |
| [Platform Handbook](platform-handbook.md) | Public operating model, runtime boundaries, and fixture-first workflow. |
| [Architecture](architecture.md) | Ownership boundaries, runtime shape, deployment model, and API surfaces. |
| [Roadmap](ROADMAP.md) | Current 0.3.0 status, next tasks, and deferred decisions. |
| [LLM Start Here](llm/START_HERE.md) | Agent routing, safe command discovery, and data boundaries. |
| [0.3.0 Upgrade Prompt](llm/UPGRADE_TO_0_3_0_PROMPT.md) | Copyable prompt for upgrading existing Ophelia checkouts and app repositories. |

## Operator References

| Document | Use it for |
| --- | --- |
| [Operator Runbook](operator-runbook.md) | Common operator commands and runtime procedures. |
| [Preflight And Safety](preflight-and-safety.md) | Dry-run, confirmation-token, policy, and receipt discipline. |
| [Releases And Rollback](releases-and-rollback.md) | Release metadata, rollback behavior, and recovery flow. |
| [Production Hardening](production-hardening.md) | Read-only production hardening report and go/no-go checks. |
| [Open Source Readiness](open-source-readiness.md) | Release audit, public-surface rules, license, and governance posture. |

## Manifest And App Contracts

| Document | Use it for |
| --- | --- |
| [Manifest Spec](manifest-spec.md) | `.ophelia.yml` fields, validation rules, and examples. |
| [App Adoption Planning](app-adoption.md) | Planning existing app migrations into the Ophelia contract. |
| [Portable App Pack Spec](portable-app-pack-spec.md) | App-pack scaffolding, scripts, redaction, and manifest bootstrap behavior. |
| [Stateful App Migration Runbook](stateful-app-migration-runbook.md) | Backup, restore, and cutover workflow for stateful apps. |
| [Host Contract](host-contract.md) | VPS host expectations, runtime roots, networking, and generated files. |
| [Host Inventory And Placement](host-inventory-and-placement.md) | Host inventory schema and placement planning behavior. |

## Readiness, Evidence, And Fixtures

| Document | Use it for |
| --- | --- |
| [Fixture App Suite](fixture-app-suite.md) | Synthetic apps and runtime observations used in tests and demos. |
| [Live Readiness Lane](live-readiness-lane.md) | Read-only readiness scoring for fixture or reviewed live evidence. |
| [Live Drill Profiles](live-drill-profiles.md) | Reusable expected-state profiles and drill receipts. |
| [Live Hydration](live-hydration.md) | Evidence scaffold, validation, reviewed promotion, and no-probe gates. |
| [Demo Docs Host Layout](demo-docs-host-layout.md) | Public-safe docs host fixture layout. |

## API, Agents, And Integrations

| Document | Use it for |
| --- | --- |
| [Job And Action API](job-action-api.md) | Command catalog, operation envelopes, local API, and JSON contracts. |
| [Plugin Contracts](plugin-contracts.md) | Fixture plugin manifests, validation, and metadata surfaces. |
| [Operator Console Adapter](operator-console-adapter.md) | Read-only console payloads for private or internal operator UIs. |
| [Source Of Truth](ophelia-source-of-truth.md) | Source-of-truth position for future apps and legacy inventory boundaries. |

## Change Records

Ophelia uses a file-per-change record system for product memory:

- [Change Record Practice](changelog/README.md)
- [Change Record Index](changelog/INDEX.md)
- [Change Record Template](changelog/TEMPLATE.md)

Add a record for behavior, contract, operator workflow, or docs changes that a
future agent would need to understand.

## Where Future Work Goes

Use this structure for new documentation:

- durable operator docs: `docs/`
- LLM and agent entrypoints: `docs/llm/`
- change records: `docs/changelog/`
- historical plans and superseded prompts: `docs/archive/`

Keep examples synthetic and public-safe. Runtime evidence, provider values,
private deployment notes, and product-specific migration details belong outside
this repository unless explicitly sanitized for a public fixture.
