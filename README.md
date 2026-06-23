<div align="center">

# Ophelia

**A fixture-first deployment control plane for VPS apps, safe operations, and agent-readable workflows.**

[![CI](https://github.com/mrbagels/ophelia/actions/workflows/ci.yml/badge.svg?branch=next)](https://github.com/mrbagels/ophelia/actions/workflows/ci.yml)
![Version](https://img.shields.io/badge/version-0.3.0-2563EB)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Status](https://img.shields.io/badge/status-pre--1.0-orange)
![Distribution](https://img.shields.io/badge/distribution-GitHub%20only-111827)
![Safety](https://img.shields.io/badge/safety-dry--run%20first-16A34A)
![Agents](https://img.shields.io/badge/agents-JSON%20contracts-7C3AED)
![DCO](https://img.shields.io/badge/contributions-DCO-2563EB)

[Get Started](#quick-start) · [Upgrade To 0.3.0](#upgrade-to-030) · [Docs](docs/README.md) · [Roadmap](docs/ROADMAP.md) · [License](#license)

</div>

Ophelia turns application manifests into validated runtime bundles, dry-run
plans, confirmation-gated applies, redacted receipts, readiness reports, and
schema-versioned JSON surfaces. It is designed for operators and agents that
need to manage many VPS-hosted apps without hand-editing Compose files, Caddy
snippets, release notes, and deployment state.

Application repositories declare their runtime contract in `.ophelia.yml`.
Ophelia validates that contract, renders the runtime files, checks safety
policy, records what happened, and exposes machine-readable command surfaces
for automation, private operator UIs, and downstream agents.

The current repository target is `https://github.com/mrbagels/ophelia`. The
project is GitHub-only and remains private until the public launch decision is
made.

## What Ophelia Gives You

| Area | What it does |
| --- | --- |
| Manifest contract | Validates service, multi-service, static, tunnel, redirect, route, backup, dependency, and verification fields. |
| Runtime rendering | Produces Docker Compose, Caddy snippets, env fragments, lock files, release metadata, and rollback bundles. |
| Safety gates | Uses dry-run plans, confirmation tokens, redacted receipts, policy checks, and strict command-string redaction. |
| Readiness | Scores app movement, host placement, backup freshness, restore drills, conflicts, secrets, image evidence, and live observations. |
| Fixture-first testing | Ships synthetic apps and reviewed evidence fixtures so behavior can be tested without private products or live secrets. |
| Agent contracts | Provides command catalogs, JSON schemas, operation envelopes, receipts, state queries, and LLM entrypoint docs. |
| Release hygiene | Includes Apache-2.0 licensing, DCO contribution rules, CI, docs checks, and a zero-warning open-source audit. |

## Project Status

| Item | Status |
| --- | --- |
| Current version | `0.3.0` |
| Stability | Pre-1.0, core contracts are active but still evolving deliberately. |
| Distribution | GitHub only. No PyPI release path is configured. |
| Visibility | Private until the public release decision is made. |
| Runtime state | Kept outside the source checkout, usually under `~/ophelia-runtime` or a configured runtime root. |
| Public data model | Synthetic examples, fixture apps, `example.com` domains, redacted reports, and schema examples only. |

Real host registries, production env files, provider credentials, reviewed live
evidence, and product-specific migration notes belong outside this repository
unless they have been deliberately sanitized into reusable fixtures.

## Quick Start

```bash
git clone https://github.com/mrbagels/ophelia.git ophelia
cd ophelia

python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[test]"

./cli/ship self-test --json
./cli/ship schema manifest --json
./cli/ship validate examples/service-app.ophelia.yml
./cli/ship render examples/service-app.ophelia.yml --output-dir ./build/demo-service
make validate-fixtures
```

After editable install, the console script is available too:

```bash
ship self-test
ship validate examples/service-app.ophelia.yml
```

## Upgrade To 0.3.0

Use [Ophelia 0.3.0 Upgrade Prompt](docs/llm/UPGRADE_TO_0_3_0_PROMPT.md) when
handing the upgrade to an agent or another engineer. The short manual path is:

```bash
git fetch origin
git checkout next
git pull --ff-only
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[test]"
```

Confirm installed metadata:

```bash
.venv/bin/python - <<'PY'
from importlib.metadata import metadata, version
print(version("ophelia"))
for value in metadata("ophelia").get_all("Project-URL") or []:
    print(value)
PY
```

Expected output:

```text
0.3.0
Repository, https://github.com/mrbagels/ophelia
Issues, https://github.com/mrbagels/ophelia/issues
```

Then run the release gate:

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

## Try The Fixture Suite

The fixture suite is the fastest way to understand Ophelia without touching a
real host:

```bash
make validate-fixtures
make validate-adoption-fixtures
make live-readiness-fixtures
make live-drills-fixtures
make live-hydration-reviewed-fixture
make production-hardening-fixtures
```

Useful JSON smoke command:

```bash
./cli/ship live-readiness run \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --allow-blocked \
  --json
```

Some fixtures are intentionally incomplete or blocked. That is how the readiness
gate is tested.

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

Plan adoption for an existing app repository without mutating it:

```bash
./cli/ship app adoption plan demo-service \
  --repo-path ../demo-service \
  --environment staging \
  --json
```

Validate, explain, and plan deployment from a manifest:

```bash
./cli/ship validate examples/service-app.ophelia.yml
./cli/ship explain examples/service-app.ophelia.yml --json
./cli/ship deploy examples/service-app.ophelia.yml --plan --json
```

Production apply requires the confirmation token from the matching plan:

```bash
./cli/ship deploy examples/service-app.ophelia.yml \
  --apply \
  --confirm <token>
```

## Manifest Shape

Minimal service manifest:

```yaml
schema_version: 1
app: demo-service
environment: staging
kind: service
image: ghcr.io/example/demo-service:latest
runtime:
  internal_port: 8080
routes:
  - host: demo-service.example.com
    path: /
checks:
  health_url: https://demo-service.example.com/health
verify:
  restore:
    command: ./scripts/verify-restore.sh
```

Full field reference: [Manifest Spec](docs/manifest-spec.md).

## Safety Model

Ophelia's default posture is conservative:

- Prefer read-only commands for inspection, readiness, hardening, and provider
  discovery.
- Produce dry-run plans before material mutations.
- Require confirmation tokens for production deploys, rollback, restore,
  traffic movement, export creation, and workflow mutating nodes.
- Redact receipts, reports, command strings, and stored state before output.
- Reference env var names and provider identifiers, not secret values.
- Keep public examples synthetic and use `example.com` domains.

## Command Map

| Command group | Purpose |
| --- | --- |
| `validate`, `explain`, `schema` | Manifest validation and contract discovery. |
| `render`, `deploy`, `diff`, `rollback` | Runtime bundle planning and controlled apply flows. |
| `pack`, `app adoption`, `app readiness` | App portability, manifest bootstrap, migration readiness, and adoption checks. |
| `backup`, `restore`, `restore-drills` | Backup planning, restore previews, and drill receipts. |
| `host`, `live-readiness`, `live-drills`, `live-hydration` | Host readiness, app readiness, evidence scaffolds, and reviewed live observations. |
| `workflow`, `operations`, `receipts`, `state` | Agent-executable operation graphs, receipts, and local state indexing. |
| `providers`, `secrets`, `policy`, `hardening` | Provider readiness, secret references, safety policy, and production go/no-go checks. |
| `plugins`, `lumen`, `api` | Plugin metadata, read-only operator-console payloads, and local API integration. |
| `open-source` | Public-release hygiene scanning. |

Machine-readable discovery:

```bash
./cli/ship commands catalog --json
./cli/ship schema manifest --json
./cli/ship actions --json
./cli/ship open-source audit --json
```

## Repository Layout

```text
ophelia/
  cli/                    # Local entrypoints for ship and ophelia
  config/                 # Public example configs and policy defaults
  docs/                   # Architecture, operations, roadmap, changelog, LLM docs
  examples/               # Public manifest examples
  fixtures/               # Synthetic apps, runtime state, providers, plugins
  manifests/              # Public demo platform-owned manifests
  platform/               # Shared host scripts, Caddy, Compose, static fixtures
  src/ophelia/            # Python control plane
  templates/              # Render templates for Compose and Caddy
  tests/                  # Unit and contract tests
```

Runtime state belongs outside the source checkout. Env files, backups, provider
evidence, pulled images, generated bundles, and production registries should not
be committed.

## Documentation

Start with [Documentation](docs/README.md). Key references:

- [Platform Handbook](docs/platform-handbook.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/ROADMAP.md)
- [Manifest Spec](docs/manifest-spec.md)
- [Preflight And Safety](docs/preflight-and-safety.md)
- [Operator Runbook](docs/operator-runbook.md)
- [Fixture App Suite](docs/fixture-app-suite.md)
- [Live Readiness Lane](docs/live-readiness-lane.md)
- [Live Hydration](docs/live-hydration.md)
- [Production Hardening](docs/production-hardening.md)
- [Operator Console Adapter](docs/operator-console-adapter.md)
- [Open Source Readiness](docs/open-source-readiness.md)
- [Change Records](docs/changelog/README.md)

Agent and LLM entrypoints:

- [LLM Start Here](docs/llm/START_HERE.md)
- [LLM Manifest](docs/llm/manifest.json)
- [0.3.0 Upgrade Prompt](docs/llm/UPGRADE_TO_0_3_0_PROMPT.md)

## Development Gate

Run the full gate before merging release, contract, or docs-navigation changes:

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

For docs-only changes, run at least:

```bash
make docs-check
make open-source-audit-strict
```

## Contributing

Contributions use Apache-2.0 plus DCO sign-off:

```bash
git commit -s -m "feat: describe the change"
```

Read:

- [Contributing](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Support Policy](SUPPORT.md)
- [Code Of Conduct](CODE_OF_CONDUCT.md)

## License

Ophelia is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE) and [NOTICE](NOTICE).
