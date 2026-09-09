# Ophelia Roadmap

Status: current for the 0.6.x line

Ophelia 0.3.0 completes the fixture-first foundation: manifest contracts,
runtime rendering, safety-gated operations, receipts, readiness, synthetic app
fixtures, app adoption planning, live evidence scaffolds, public governance, and
strict release hygiene.

The 0.5.0 line began the accepted platform direction with strict validation,
secure filesystem primitives, immutable operation staging, an authoritative
SQLite journal, recoverable execution, truthful static activation and rollback,
and executable Forge product-operation bundles. The legacy manifest path
remains available while additional workloads migrate onto the kernel.

The 0.6.0 line adds manifest v2 execution, the independent `opheliad` host
authority, authenticated outbound fleet protocol, lifecycle-safe upgrades,
encrypted host-control backup, clean-host recovery, and continuous durable
observations. Lumen's matching server authority is the active integration edge.

This roadmap replaces the older long-form implementation plans now stored in
[Archive](archive/README.md). Use the archive for historical context only.
Current work should be tracked here, in change records, and in the relevant
reference doc for the feature being changed.

The accepted product direction, target host-agent architecture, verified
risk register, Lumen integration boundary, and dependency-ordered P0 through P5
implementation plan are consolidated in
[Platform Strategy And Technical Architecture](product/ophelia-platform-strategy.md).
Work on `next` follows that direction. This roadmap remains the active delivery
record as the strategy is converted into reviewed releases.

## Completed In 0.3.0

| Area | Status |
| --- | --- |
| Manifest and schema contract | Landed for service, multi-service, static, tunnel, redirect, routes, checks, dependencies, backups, and metadata. |
| Runtime rendering | Landed for Compose, Caddy snippets, env fragments, lock files, static asset releases, releases, and rollback support. |
| Safety model | Landed with dry-run plans, confirmation tokens, redacted receipts, command-string scrubbing, and policy checks. |
| Operation contracts | Landed with command catalog, action registry, plan envelopes, operation IDs, receipts, state indexing, and local API surfaces. |
| App factory and adoption | Landed with per-template scaffolds, overwrite guards, manifest bootstrap, adoption fixtures, and redacted metadata. |
| Fixture suite | Landed with synthetic service, static, stateful, worker, provider, incomplete, multi-service, and console-profile fixtures. |
| Readiness and hardening | Landed with live-readiness lanes, reviewed evidence promotion, no-probe gates, drill profiles, and production hardening reports. |
| SaaS onboarding contracts | Landed with internal service verification, schema-aware JSON assertions, lifecycle reset policy, fresh-install plans, one-shot backup rehearsals, actionable offsite backup policy, runtime release metadata env, and app-owned readiness evidence. |
| Release hardening | Landed with `ship version`, production image digest lock planning/apply, offsite rehearsal evidence freshness checks, and shared Postgres cutover evidence artifacts. |
| Operator console and plugins | Landed as read-only console payloads, API summaries, and fixture plugin contracts. |
| Open-source preparation | Landed with Apache-2.0, DCO, governance files, public-safe docs, CI, docs-check, and strict zero-warning audit. |

## Next Practical Work

These are the next dependency-ordered outcomes:

1. Implement Lumen's certificate authority, host exchange, signed command
   queue, acknowledgement projection, and DeployProvider against the completed
   Ophelia host-agent protocol.
2. Prove enrollment, encrypted recovery, revocation, disconnect replay,
   certificate rotation, and
   rollback-protected upgrades on disposable Linux hosts before production
   enrollment.

The durable `opheliad` single-host authority, Unix-socket API, reboot recovery,
cron and task runner, global host event cursor, systemd service, and installer
are implemented on `next` for the next pre-release line.

The outbound host-agent client is also implemented on `next`: confirmation-bound
enrollment, mutual TLS, signed and scoped command envelopes, strict sequence
replay, independent event and result acknowledgement, Decision-bound deploys,
certificate rotation, revocation posture, and staged self-upgrade with startup
rollback. The matching Lumen server authority is the next integration boundary.

Encrypted host-control backup, clean-host restore, independently stored host
identity, bounded continuous observations, backup freshness, and observation
delivery are implemented on `next`. Real stateful application restore remains
subject to each product's declared backup and verification contract.

Manifest v2 and its journaled Compose revision backend are implemented on
`next`. Valid v1 manifests remain an explicit compatibility boundary. V2 adds
strict workload contracts, immutable revision projects, web overlap,
non-overlapping worker and internal handoff, cron fencing metadata,
operation-scoped migrations, transactional Caddy activation, exact local
approval binding, and terminal kernel receipts.

## Selected Portability Program

The eight selected minor portability features are implemented. This completion
pass closes the remaining gaps without changing their read-only or
confirmation-gated safety boundaries:

| # | Minor feature | Current status |
| ---: | --- | --- |
| 1 | App Move Readiness Checklist | Landed through `ship app readiness`, with blockers, warnings, score factors, source reports, and typed next actions. |
| 2 | Redacted Env Shape Diff | Landed through `ship env diff`; key names and status are visible while values remain redacted. |
| 3 | Backup Freshness Receipt | Landed through `ship backup status`; successful backup verification evidence is surfaced, otherwise validation is explicitly metadata-only. |
| 4 | Route And Domain Conflict Scanner | Landed through `ship inspect conflicts`, including host-global on-demand TLS ask and catch-all ownership conflicts. |
| 5 | Portability Score | Landed as a deterministic, explainable readiness score that never overrides blockers. |
| 6 | Generated App Runbook | Landed with one structured redacted model rendered as aligned JSON and Markdown. |
| 7 | Receipt Browser Commands | Landed across legacy files, global product wrappers, and authoritative kernel terminal receipts, with source and integrity diagnostics. |
| 8 | Pack Init Scaffolder | Landed as preview-first scaffolding with explicit write and overwrite gates. |

Major features 1, 2, 3, 4, 5, 6, and 8 are selected for a later implementation
phase. They are specified in detail in
[Portability Major Features](features/portability-major-features.md), with a
[static HTML reading version](features/portability-major-features.html). The
specification records current `0.6.x` foundations separately from remaining
work, then defines contracts, safety invariants, recovery behavior,
dependency-ordered slices, acceptance tests, and definitions of done.

Feature 6 is a working interpretation named **Host Bootstrap And Reconcile**.
The archived taxonomy omitted its number, while current architecture contains
the corresponding installer, daemon, host inventory, recovery, and observation
foundations. A later approved taxonomy correction may rename or renumber it
without weakening the documented contract.

This selected program does not replace the immediate Lumen integration work in
[Next Practical Work](#next-practical-work). Its dependency order begins with
contract characterization, a canonical portability domain, and isolation and
host foundations before real import, restore, cutover, and UI completion.

## Adoption And Live-Values Phase

Live values and production adoption should start only when an actual retained
app is ready to move onto Ophelia. The expected flow is:

1. Create or update the app repository's `.ophelia.yml`.
2. Run adoption planning and manifest validation locally.
3. Generate or review required env names, provider references, and backup
   expectations without storing secret values in Git.
4. Add app-owned `/ophelia/health`, `/ophelia/release`, and `npm run
   ophelia:*` checks before production onboarding.
5. Capture reviewed live evidence outside the source checkout.
6. Promote sanitized observations into fixtures only when they define reusable
   Ophelia behavior.
7. Run `make open-source-audit-strict` before any material returns to this repo.

## Deferred Decisions

1. First retained app adoption

   Issue: old projects should not shape Ophelia's architecture unless they are
   intentionally retained and migrated.

   Proposed direction: use Ophelia as the source of truth, then adapt retained
   apps to the current manifest and runtime contract when they are ready.

   Why it matters: fixtures should model the desired platform, not accidental
   legacy host state.

   Impact: app manifests, app repositories, provider values, backups, and live
   readiness evidence.

   Risks/tradeoffs: adoption can expose real deployment constraints that require
   contract changes; those should be explicit product decisions.

   Complexity: Medium to Large, depending on the app.

3. Distribution beyond GitHub

   Issue: the 0.4.x line is GitHub-only and has no PyPI release path.

   Proposed direction: keep GitHub-only for the private phase; revisit package
   publishing after public support expectations are clear.

   Why it matters: package distribution creates installation, versioning, and
   support commitments.

   Impact: packaging metadata, release process, install docs, and CI.

   Risks/tradeoffs: GitHub-only is simpler but less convenient; PyPI improves
   install ergonomics but raises release discipline requirements.

   Complexity: Medium.

4. Remote host bootstrap automation

   Issue: Ophelia can model host contracts and runtime roots, but a fully
   automated first-host bootstrap still needs more operational hardening before
   broad reuse.

   Proposed direction: keep bootstrap steps explicit until a retained deployment
   proves the desired workflow end to end.

   Why it matters: first-host automation touches SSH, Docker, Caddy, env layout,
   backups, and rollback expectations.

   Impact: operator onboarding, disaster recovery, and future app rollout speed.

   Risks/tradeoffs: too much automation too early can hide dangerous host
   assumptions; too little automation increases operator friction.

   Complexity: Large.
