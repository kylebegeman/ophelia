# Ophelia Roadmap

Status: current for the 0.5.x line

Ophelia 0.3.0 completes the fixture-first foundation: manifest contracts,
runtime rendering, safety-gated operations, receipts, readiness, synthetic app
fixtures, app adoption planning, live evidence scaffolds, public governance, and
strict release hygiene.

The 0.5.0 line begins the accepted platform direction with strict validation,
secure filesystem primitives, immutable operation staging, an authoritative
SQLite journal, recoverable execution, truthful static activation and rollback,
and executable Forge product-operation bundles. The legacy manifest path
remains available while additional workloads migrate onto the kernel.

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

1. Promote the executor and recovery engine into the durable single-host
   `opheliad` service with a Unix-socket API and reboot reconciliation.
2. Prove encrypted backup and clean-host restore for stateful workloads, then
   attach continuous health, disk, certificate, and recovery signals.
3. Add authenticated outbound host-agent transport, replayable event cursors,
   enrollment, maintenance, drain, and staged upgrades.
4. Implement Lumen's DeployProvider against versioned Ophelia operations,
   then replace that transport with the authenticated host agent as fleet work
   lands.

Manifest v2 and its journaled Compose revision backend are implemented on
`next`. Valid v1 manifests remain an explicit compatibility boundary. V2 adds
strict workload contracts, immutable revision projects, web overlap,
non-overlapping worker and internal handoff, cron fencing metadata,
operation-scoped migrations, transactional Caddy activation, exact local
approval binding, and terminal kernel receipts.

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

1. Public launch timing

   Issue: the repository is configured for `github.com/mrbagels/ophelia`, but it
   intentionally remains private for now.

   Proposed direction: keep developing on GitHub privately until the README,
   license, docs, CI, and release audit are stable across normal work.

   Why it matters: public launch changes the support, security, and contribution
   surface.

   Impact: repository visibility, issue intake, contribution expectations, and
   release communication.

   Risks/tradeoffs: opening too early creates support load and public contract
   pressure; waiting too long reduces external feedback.

   Complexity: Medium.

2. First retained app adoption

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
