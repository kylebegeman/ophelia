# Ophelia Source Of Truth

Status: active product direction

Ophelia is the source of truth for the VPS app/runtime contract. Future
software should be built to fit Ophelia's manifests, runtime layout, safety
gates, receipts, state model, and migration workflows. Existing deployments are
not the model unless we explicitly start a migration or adoption phase for that
product.

## Contract First

Ophelia defines:

- manifest shape and validation rules
- generated runtime bundle layout
- env and secret-name evidence boundaries
- deploy, render, diff, readiness, backup, restore, cutover, and traffic flows
- JSON envelopes, receipts, operation IDs, redaction, and safety gates
- fixture-backed tests for expected product behavior

Application repositories conform to this contract. They can add app-specific
build, test, and deployment details, but they should not force Ophelia to encode
one-off legacy runtime assumptions as core behavior.

## Test Substrate

Use committed synthetic fixtures for product and contract development:

- `fixtures/adoption/`
- `fixtures/app-suite/`
- `fixtures/app-suite/live-drills.yml`
- `fixtures/app-suite/hydration/fixture-postgres-api/staging/`

Fixtures are allowed to model different app shapes, state levels, failure
modes, provider observations, and migration states. They are not production
data, and they are safe to run in local tests and CI.

Do not use abandoned or old products as the primary test substrate. They can
remain as legacy inventory or historical evidence, but they should not decide
new Ophelia behavior.

## Existing Deployments

Existing products and old host contents are handled in one of three ways:

- `legacy inventory`: read-only observation, audit, or decommission context
- `retained product adoption`: an explicit phase that updates the product to fit
  Ophelia's current contract
- `production deployment`: an explicit rollout with real env values, provider
  access, backups, probes, approvals, receipts, and rollback notes

Until one of those phases is approved, active implementation should stay on
fixtures and source-of-truth contracts.

## Adoption Entry Point

Use [App Adoption Planning](app-adoption.md) when a repo is ready to be shaped
for Ophelia:

```bash
./cli/ship app adoption plan demo-app --repo-path ../demo-app --environment staging --json
```

The adoption plan is read-only. It checks the repo path, `.ophelia.yml`, pack
validation, and recommended repo-local Ophelia artifacts. It emits the next
Ophelia commands without collecting live env values, secret values, probes,
provider state, deployment state, or product-specific runtime evidence.

## Retained Products

Retained products should still start from Ophelia's contract and fixture
coverage. When it is time to deploy or migrate one, add product-specific
manifests, live profiles, runbooks, and evidence as private adoption artifacts,
not as new core assumptions in the public repository.

## Practical Rule

When adding a feature, ask:

```text
Can this be proven with synthetic fixtures first?
```

If yes, build the fixture and the contract behavior there. If no, document the
reason and treat the product-specific work as an approved deployment,
migration, or integration phase.
