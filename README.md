<div align="center">

# Ophelia

**A fixture-first deployment control plane for VPS apps, safe operations, and agent-readable workflows.**

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Status](https://img.shields.io/badge/status-pre--1.0-orange)
![CLI](https://img.shields.io/badge/CLI-ship-0F766E)
![Safety](https://img.shields.io/badge/safety-dry--run%20first-16A34A)
![Agents](https://img.shields.io/badge/agents-JSON%20contracts-7C3AED)
![DCO](https://img.shields.io/badge/contributions-DCO-2563EB)

</div>

Ophelia is a small Python control plane for running many apps on a VPS without
turning the host into a pile of hand-edited Compose files, Caddy snippets, and
untracked deployment notes.

Application repositories declare their runtime contract in `.ophelia.yml`.
Ophelia validates that contract, renders runtime bundles, plans changes, gates
risky mutations behind confirmation tokens, writes receipts, and exposes
schema-versioned JSON for operators, automation, Lumen, and downstream agents.

The public repo is intentionally fixture-first. Real host registries, production
env files, provider credentials, and product-specific migration notes belong in
private operator material until a deployment or adoption phase is explicitly
approved.

Repository target: `github.com/mrbagels/ophelia`. The project is GitHub-only
and remains private until the public release decision is made.

## What Ophelia Gives You

| Area | What it does |
| --- | --- |
| Manifest contract | Validates service, multi-service, static, tunnel, and redirect app manifests. |
| Runtime rendering | Produces Docker Compose, Caddy snippets, env fragments, lock files, and release metadata. |
| Safety gates | Uses dry-run plans, confirmation tokens, redacted receipts, and policy checks for risky commands. |
| Readiness | Scores app movement, host placement, backup freshness, restore drills, conflicts, secrets, and live evidence. |
| Fixtures | Ships deterministic synthetic apps and live-state observations for repeatable local and CI tests. |
| Agent surfaces | Provides command catalogs, JSON schemas, operation envelopes, receipts, and LLM entrypoint docs. |
| Open-source hygiene | Includes Apache-2.0 licensing, DCO contribution rules, security policy, support policy, and a release audit. |

## Project Status

Ophelia is pre-1.0. The core CLI, manifest parser, render path, readiness
surfaces, fixture suite, local API, workflow previews, receipts, and open-source
audit are active. Treat CLI and JSON contracts as important, but expect
carefully documented changes while the project is still being shaped.

The recommended public gate is:

```bash
make validate-examples
make validate-manifests
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
make render-examples
make render-manifests
make test
make compile
make docs-check
make open-source-audit-strict
```

## Quick Start

```bash
git clone https://github.com/mrbagels/ophelia.git ophelia
cd ophelia

python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e ".[test]"

./cli/ship self-test --json
./cli/ship schema manifest --json
./cli/ship validate examples/service-app.ophelia.yml
./cli/ship render examples/service-app.ophelia.yml --output-dir ./build/demo-service
make validate-fixtures
```

You can also use the installed console script after editable install:

```bash
ship self-test
ship validate examples/service-app.ophelia.yml
```

## Try The Fixture Suite

The fixture suite is the fastest way to understand Ophelia without touching a
real host:

```bash
make validate-fixtures
make validate-adoption-fixtures
make live-readiness-fixtures
make live-drills-fixtures
make live-hydration-reviewed-fixture
make lumen-console-fixtures
make production-hardening-fixtures
```

Useful single commands:

```bash
./cli/ship live-readiness run \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --allow-blocked \
  --json

./cli/ship live-drills run fixture-suite-review --json

./cli/ship live-hydration promotion-plan \
  --profile fixture-postgres-focused \
  --profiles fixtures/app-suite/live-drills.yml \
  --input-dir fixtures/app-suite/hydration/fixture-postgres-api/staging \
  --json
```

One fixture is intentionally incomplete, so some fixture readiness reports are
expected to be blocked. That is a test of the gate, not a broken install.

## Create Or Adopt An App

Preview app-pack scaffolding:

```bash
./cli/ship pack init \
  --app demo-service \
  --environment staging \
  --directory ../demo-service \
  --include-manifest \
  --kind service \
  --domain demo-service.example.com \
  --image ghcr.io/example/demo-service:latest \
  --json
```

Check an existing app repository without mutating it:

```bash
./cli/ship app adoption plan demo-service \
  --repo-path ../demo-service \
  --environment staging \
  --json
```

Render and plan deployment from a manifest:

```bash
./cli/ship validate examples/service-app.ophelia.yml
./cli/ship explain examples/service-app.ophelia.yml --json
./cli/ship deploy examples/service-app.ophelia.yml --plan --json
```

Production apply requires a confirmation token from the matching plan:

```bash
./cli/ship deploy examples/service-app.ophelia.yml \
  --apply \
  --confirm <token>
```

## Safety Model

Ophelia's default posture is conservative:

- Read-only commands are preferred for inspection, readiness, hardening, and
  provider discovery.
- Mutating flows are dry-run-first when risk is material.
- Production deploys, rollback, restore, traffic movement, export creation, and
  workflow mutating nodes require confirmation tokens from matching plans.
- Receipts and reports pass through redaction before they are stored or printed.
- Secret values should never be committed, printed, or embedded in command
  strings. Use env var names and provider references instead.
- The public repository uses synthetic examples and `example.com` domains.

## Repository Layout

```text
ophelia/
  cli/                    # Local entrypoints for ship and ophelia
  config/                 # Public example configs and policy defaults
  docs/                   # Architecture, operations, roadmap, and LLM docs
  examples/               # Public manifest examples
  fixtures/               # Synthetic apps, runtime state, providers, plugins
  manifests/              # Public demo platform-owned manifests
  platform/               # Shared host scripts, Caddy, Compose, static fixtures
  src/ophelia/            # Python control plane
  templates/              # Render templates for Compose and Caddy
  tests/                  # Unit and contract tests
```

Runtime state belongs outside the source checkout, usually under
`~/ophelia-runtime`. Env files, backups, provider evidence, pulled images,
runtime bundles, and production registries should not be committed.

## Agent And LLM Entrypoints

Start here:

- [LLM Start Here](docs/llm/START_HERE.md)
- [LLM Manifest](docs/llm/manifest.json)
- [Command Catalog](docs/job-action-api.md)
- [Manifest Spec](docs/manifest-spec.md)
- [Fixture App Suite](docs/fixture-app-suite.md)
- [Open Source Readiness](docs/open-source-readiness.md)

Machine-readable discovery:

```bash
./cli/ship commands catalog --json
./cli/ship schema manifest --json
./cli/ship actions --json
./cli/ship open-source audit --json
```

Guidance for agents:

- Prefer JSON output over human text.
- Treat `kind`, `schema_version`, `operation`, and `operation_id` as routing
  keys.
- Do not execute mutating commands unless a matching plan produced the required
  confirmation token.
- Use fixtures first, then sanitized operator-provided inputs.
- Never request, print, or store secret values.

## Command Map

| Command group | Purpose |
| --- | --- |
| `validate`, `explain`, `schema` | Manifest validation and contract discovery. |
| `render`, `deploy`, `diff`, `rollback` | Runtime bundle planning and controlled apply flows. |
| `pack`, `app adoption`, `app readiness` | App portability, migration readiness, and adoption checks. |
| `backup`, `restore`, `restore-drills` | Backup planning, restore previews, and drill receipts. |
| `host`, `live-readiness`, `live-drills`, `live-hydration` | Host and app readiness lanes, from fixtures to reviewed live evidence. |
| `workflow`, `operations`, `receipts`, `state` | Agent-executable operation graphs, receipts, and local state indexing. |
| `providers`, `secrets`, `policy`, `hardening` | Provider readiness, secret references, safety policy, and production go/no-go checks. |
| `plugins`, `lumen`, `api` | Plugin metadata, read-only Lumen console payloads, and local API integration. |
| `open-source` | Public-release hygiene scanning. |

## Documentation

Core docs:

- [Platform Handbook](docs/platform-handbook.md)
- [Architecture](docs/architecture.md)
- [Manifest Spec](docs/manifest-spec.md)
- [Preflight And Safety](docs/preflight-and-safety.md)
- [Releases And Rollback](docs/releases-and-rollback.md)
- [Host Contract](docs/host-contract.md)
- [App Adoption Planning](docs/app-adoption.md)
- [Portable App Pack Spec](docs/portable-app-pack-spec.md)
- [Live Readiness Lane](docs/live-readiness-lane.md)
- [Live Drill Profiles](docs/live-drill-profiles.md)
- [Live Hydration](docs/live-hydration.md)
- [Production Hardening](docs/production-hardening.md)
- [Plugin Contracts](docs/plugin-contracts.md)
- [Lumen Operator Console](docs/lumen-operator-console.md)
- [Source Of Truth](docs/ophelia-source-of-truth.md)
- [Strategic Roadmap](docs/ophelia-strategic-implementation-roadmap.md)
- [Change Records](docs/changelog/README.md)

## Contributing

Contributions use Apache-2.0 plus DCO sign-off.

```bash
git commit -s -m "feat: describe the change"
```

Before opening a pull request, run:

```bash
make validate-examples
make validate-manifests
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
make render-examples
make render-manifests
make test
make compile
make docs-check
make open-source-audit-strict
```

Read:

- [Contributing](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Support Policy](SUPPORT.md)
- [Code Of Conduct](CODE_OF_CONDUCT.md)

## License

Ophelia is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE)
and [NOTICE](NOTICE).
