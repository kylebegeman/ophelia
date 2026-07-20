<div align="center">

<img src="docs/assets/persephone.svg" alt="Persephone illustration" width="150" />

# Ophelia

**A fixture-first deployment control plane for VPS apps, static sites, safe operations, and agent-readable workflows.**

[![CI](https://github.com/mrbagels/ophelia/actions/workflows/ci.yml/badge.svg?branch=next)](https://github.com/mrbagels/ophelia/actions/workflows/ci.yml)
![Version](https://img.shields.io/badge/version-0.6.1-2563EB)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Status](https://img.shields.io/badge/status-pre--1.0-orange)
![Distribution](https://img.shields.io/badge/distribution-GitHub%20only-111827)
![Static](https://img.shields.io/badge/static%20sites-first--class-059669)
![Safety](https://img.shields.io/badge/safety-dry--run%20first-16A34A)
![Agents](https://img.shields.io/badge/agents-JSON%20contracts-7C3AED)
![DCO](https://img.shields.io/badge/contributions-DCO-2563EB)

[Quick Start](#quick-start) · [Static Sites](#deploy-a-static-site) · [Service Apps](#deploy-a-service-app) · [Docs](docs/README.md) · [Roadmap](docs/ROADMAP.md) · [License](#license)

</div>

Ophelia turns application manifests into validated runtime bundles, dry-run
plans, confirmation-gated applies, redacted receipts, readiness reports, and
schema-versioned JSON surfaces. It is built for operators and agents that need
to manage VPS-hosted apps without hand-editing Compose files, Caddy snippets,
release records, backup metadata, and deployment state.

Each app declares its runtime contract in `.ophelia.yml`. Ophelia validates that
contract, renders the runtime files, checks safety policy, records what
happened, and exposes predictable command and JSON surfaces for automation.

The current repository target is `https://github.com/mrbagels/ophelia`.
Distribution is GitHub-only, and the repository can remain private until a
public launch decision is made.

## What You Can Run

| App type | Ophelia support |
| --- | --- |
| Static sites | First-class `kind: static` manifests, repo-local build roots, immutable static releases, Caddy serving, and no Docker image requirement. |
| Single services | One container behind the shared Caddy edge with release metadata and health checks. |
| Multi-service apps | Multiple Compose services with shared or per-app internal networks. |
| Stateful apps | Postgres, Redis, named volumes, host-path volumes, export contracts, restore rehearsals, and backup readiness. |
| Redirects and tunnels | Caddy-rendered redirect and tunnel routes for migration or edge compatibility. |
| Non-live apps | Explicit lifecycle fields, fresh-install planning, safe volume reset gates, and data verifier execution. |
| Workload manifests | Strict manifest v2 support for web, worker, cron, task, migration, internal, and static lifecycles through the journaled revision engine. |
| Independent hosts | Durable `opheliad` authority with outbound mutual-TLS Lumen control, encrypted host recovery, and continuous observations. |

## Why Ophelia Exists

| Area | What it gives you |
| --- | --- |
| Manifest contract | One app-owned declaration for services, routes, lifecycle, data, backups, readiness, and verification. |
| Runtime rendering | Deterministic Docker Compose, Caddy snippets, env templates, lock files, release metadata, and rollback bundles. |
| Safety gates | Dry-run plans, production confirmation tokens, redacted receipts, policy checks, and command-string secret scrubbing. |
| Static publishing | Apply publishes relative `static_root` directories into `<runtime-root>/static/<app>/releases/<release-id>` and serves through `current`. |
| Readiness | App movement scoring, host placement checks, restore drill evidence, conflict warnings, secret metadata, and live observations. |
| Agent contracts | Command catalog, action registry, JSON schemas, operation envelopes, receipts, state queries, and LLM entrypoint docs. |
| Fixture-first QA | Synthetic apps, reviewed evidence fixtures, and live-readiness harnesses that test behavior without private products or real secrets. |

## Project Status

| Item | Status |
| --- | --- |
| Current version | `0.6.1` |
| Stability | Pre-1.0. Core contracts are active, but JSON and CLI surfaces may still evolve deliberately. |
| Distribution | GitHub only. No PyPI release path is configured. |
| Runtime state | Kept outside the source checkout, usually under `~/ophelia-runtime` or a configured runtime root. |
| Public data model | Synthetic examples, fixture apps, `example.com` domains, redacted reports, and schema examples only. |
| License | Apache License 2.0 with DCO sign-off for contributions. |

Real host registries, production env files, provider credentials, reviewed live
evidence, and product-specific migration notes should stay outside this
repository unless they have been deliberately sanitized into reusable fixtures.

## Quick Start

```bash
git clone https://github.com/mrbagels/ophelia.git ophelia
cd ophelia

python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[test]"

./cli/ship self-test --json
./cli/ship version --json
./cli/ship schema manifest --json
./cli/ship validate examples/service-app.ophelia.yml
./cli/ship manifest check examples/service-app.ophelia.yml --json
./cli/ship render examples/service-app.ophelia.yml --output-dir ./build/demo-service
```

For a manifest v2 application, calculate a read-only revision plan and apply
the exact reviewed plan through `opheliad`:

```bash
sudo ship manifest plan /srv/example/.ophelia.yml --json
sudo ship manifest apply <plan-id> --confirm <token> --json
```

The daemon journals acceptance before returning, executes asynchronously,
recovers after restart, runs active-revision cron and task workloads, and
exposes replayable host events. Its optional outbound agent adds mutual-TLS
enrollment, signed scoped Lumen commands, disconnected event and result replay,
certificate rotation, encrypted host-control backups, clean-host recovery,
continuous health observations, and rollback-protected staged upgrades without
opening a public host port. See the [Ophelia Host Daemon](docs/daemon.md) for
installation, configuration, enrollment, API, and recovery procedures.

After editable install, the console scripts are available too:

```bash
ship self-test
ship version
ophelia self-test
ship validate examples/service-app.ophelia.yml
```

Confirm installed metadata:

```bash
ship version --json
```

Expected output excerpt:

```json
{
  "kind": "ophelia.version",
  "name": "ophelia",
  "schema_version": 1,
  "source": "pyproject",
  "version": "0.6.1"
}
```

## Deploy A Static Site

Static sites are the simplest first-class deploy path. They do not need Docker
images. Ophelia stages a repo-local asset directory and publishes it into the
runtime static release tree.

Create a static app scaffold:

```bash
./cli/ship pack init \
  --app demo-static \
  --environment staging \
  --directory ../demo-static \
  --include-manifest \
  --kind static \
  --domain demo-static.example.com \
  --write
```

Build or copy your static output into `../demo-static/public/`, then validate
and plan:

```bash
./cli/ship validate ../demo-static/.ophelia.yml
./cli/ship deploy ../demo-static/.ophelia.yml --plan --runtime-root ~/ophelia-runtime
```

Apply locally on the host:

```bash
./cli/ship deploy ../demo-static/.ophelia.yml \
  --apply \
  --runtime-root ~/ophelia-runtime
```

Or stage and apply over SSH:

```bash
./cli/ship deploy ../demo-static/.ophelia.yml \
  --host deploy@example-host \
  --ssh-port 22022 \
  --remote-runtime-root ~/ophelia-runtime \
  --remote-ophelia-root ~/ophelia \
  --apply
```

For production over SSH, generate the plan on the remote host and use that
remote token for apply:

```bash
./cli/ship deploy ../demo-static/.ophelia.yml \
  --host deploy@example-host \
  --remote-runtime-root ~/ophelia-runtime \
  --remote-ophelia-root ~/ophelia \
  --plan --json

./cli/ship deploy ../demo-static/.ophelia.yml \
  --host deploy@example-host \
  --remote-runtime-root ~/ophelia-runtime \
  --remote-ophelia-root ~/ophelia \
  --apply --confirm <remote-token>
```

Minimal static manifest:

```yaml
version: 1
app: demo-static
environment: staging
kind: static
static_root: public

routes:
  - domain: demo-static.example.com

verify:
  - name: homepage
    url: https://demo-static.example.com/
    expect_status: 200
```

How the static path works:

| Step | Behavior |
| --- | --- |
| Plan | Reports source path, source digest, serving root, and whether assets need syncing. |
| Stage | Copies the relative `static_root` into the manifest lock bundle for local render, remote deploy, and rollback evidence. |
| Apply | Copies assets into `<runtime-root>/static/<app>/releases/<release-id>` and updates `<runtime-root>/static/<app>/current`. |
| Serve | Caddy serves `{$OPHELIA_STATIC_ROOT}/<app>/current`. The shared Caddy runtime sets `OPHELIA_STATIC_ROOT`. |
| Compatibility | Absolute `static_root` values are still supported as externally managed Caddy roots. |

## Deploy A Service App

Create a service scaffold:

```bash
./cli/ship app create plan \
  --app demo-service \
  --template docker-web \
  --environment staging \
  --owner personal \
  --json
```

Scaffold app-pack support directly into an existing repository:

```bash
./cli/ship pack init \
  --app demo-service \
  --environment staging \
  --directory ../demo-service \
  --include-manifest \
  --kind service \
  --domain demo-service.example.com \
  --image ghcr.io/example/demo-service:latest \
  --write
```

Minimal service manifest:

```yaml
version: 1
app: demo-service
environment: staging
kind: service
image: ghcr.io/example/demo-service:latest

services:
  web:
    port: 8080
    healthcheck:
      path: /health

routes:
  - domain: demo-service.example.com
    service: web

verify:
  - name: ophelia-health
    service: app
    path: /ophelia/health
    method: GET
    expect_status: 200
    json_assertions:
      - $.kind == "product.runtime.health"
      - $.ok == true
```

Deploy flow:

```bash
./cli/ship validate ../demo-service/.ophelia.yml
./cli/ship explain ../demo-service/.ophelia.yml --json
./cli/ship deploy ../demo-service/.ophelia.yml --plan --json
./cli/ship deploy ../demo-service/.ophelia.yml \
  --apply \
  --release-id demo-service-v1.2.3 \
  --commit-sha <app-commit-sha> \
  --build-time 2026-06-25T12:00:00Z \
  --verify
```

Production apply requires the confirmation token from the matching plan:

```bash
./cli/ship deploy ../demo-service/.ophelia.yml --plan --json
./cli/ship deploy ../demo-service/.ophelia.yml --apply --confirm <token> --verify
```

Remote production applies use the same rule, but the matching plan must be run
with `--host` so the token is calculated from the staged remote runtime bundle:

```bash
./cli/ship deploy ../demo-service/.ophelia.yml --host deploy@example-host --plan --json
./cli/ship deploy ../demo-service/.ophelia.yml --host deploy@example-host --apply --confirm <remote-token> --verify
```

## Standard Workflow

| Workflow | Commands |
| --- | --- |
| Validate a manifest | `ship validate <manifest>` and `ship explain <manifest> --json` |
| Preview runtime changes | `ship deploy <manifest> --plan` and `ship diff <manifest>` |
| Pin production images | `ship release image-lock plan <manifest> --json` then `ship release image-lock apply <manifest> --confirm <token>` |
| Apply a release | `ship deploy <manifest> --apply --verify` |
| Bootstrap the shared edge | `ship caddy bootstrap --runtime-root /var/lib/ophelia --start --json` |
| Reload the shared edge | `ship caddy reload --runtime-root /var/lib/ophelia --json` |
| Inspect runtime | `ship status`, `ship doctor`, `ship releases <app>`, `ship release show <app> active` |
| Verify after apply | `ship verify <manifest>` or `ship verify <app>` |
| Roll back | `ship rollback plan <app> <release-id>` then `ship rollback apply <app> <release-id> --confirm <token>` |
| Back up and rehearse | `ship backup plan <app>`, `ship backup create <app> --confirm <token>`, `ship backup rehearse <artifact> --manifest <manifest>` |
| Adopt an existing repo | `ship app adoption plan <app> --repo-path <repo> --environment <env> --json` |
| Check readiness | `ship app readiness <app> --manifest <manifest> --json` and `ship live-readiness run --json` |
| Discover contracts | `ship commands catalog --json`, `ship actions --json`, `ship schema manifest --json` |

## Runtime Layout

Ophelia writes runtime state outside the source checkout:

```text
<runtime-root>/
  apps/<app>/
    compose.yml
    env
    env.example
    manifest.lock.json
    release.json
    active_release.json
    releases/<release-id>.json
    release-bundles/<release-id>/
  caddy/
    env
    global.d/
    sites.d/
  receipts/
  state/
  static/<app>/
    current -> releases/<release-id>
    releases/<release-id>/
```

Source checkouts keep manifests and app-owned support files. Runtime roots keep
generated state, env files, receipts, backups, release bundles, and static
release artifacts.

## Manifest Essentials

| Section | Purpose |
| --- | --- |
| `kind` | Chooses service, multi-service, static, tunnel, or redirect behavior. |
| `services` | Defines container ports, images, commands, env, mounts, and health checks. |
| `routes` | Defines Caddy host routing and service targets. |
| `static_root` | Defines static-site source or serving root. Relative paths are managed by Ophelia. |
| `required_env` | Names env keys that must exist in runtime env files without rendering inline secret values. |
| `data` | Defines databases, volumes, object storage, backups, restore rehearsals, and data verifiers. |
| `lifecycle` | Declares live status, reset permissions, and production apply policy. |
| `verify` | Defines public URL checks, internal service checks, command checks, and JSON assertions. |
| `pack` | Adds portability, ownership, and movement metadata for adoption and inventory. |

Full reference: [Manifest Spec](docs/manifest-spec.md).

## Safety Model

Ophelia's default posture is conservative:

- Read-only inspection comes before mutation.
- Deploys and high-risk operations produce dry-run plans.
- Production apply, rollback, restore, traffic movement, backup creation, and
  mutating workflow nodes require confirmation tokens.
- Receipts, reports, command strings, state payloads, and JSON output are
  redacted before storage or display.
- Apps reference env var names and provider identifiers, not secret values.
- Public examples use synthetic fixtures and `example.com` domains.

## Fixture Suite

The fixture suite is the fastest way to understand behavior without touching a
real host:

```bash
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
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

Some fixtures are intentionally incomplete or blocked. That is how readiness
and remediation behavior are tested.

## Agent And LLM Surfaces

Ophelia is designed to be operated by humans and downstream agents:

```bash
./cli/ship commands catalog --json
./cli/ship actions --json
./cli/ship schema manifest --json
./cli/ship workflow list --json
./cli/ship receipts list --json
./cli/ship state query receipts --json
```

Agent entrypoints:

- [LLM Start Here](docs/llm/START_HERE.md)
- [LLM Manifest](docs/llm/manifest.json)
- [0.3.0 Upgrade Prompt](docs/llm/UPGRADE_TO_0_3_0_PROMPT.md)

## Documentation Map

Start with [Documentation](docs/README.md). Common destinations:

| Need | Doc |
| --- | --- |
| Product direction and implementation strategy | [Platform Strategy And Technical Architecture](docs/product/ophelia-platform-strategy.md) |
| Platform model | [Platform Handbook](docs/platform-handbook.md) |
| Architecture | [Architecture](docs/architecture.md) |
| Manifest fields | [Manifest Spec](docs/manifest-spec.md) |
| Portable app packs | [Portable App Pack Spec](docs/portable-app-pack-spec.md) |
| Operator flow | [Operator Runbook](docs/operator-runbook.md) |
| Release history | [Releases And Rollback](docs/releases-and-rollback.md) |
| Safety gates | [Preflight And Safety](docs/preflight-and-safety.md) |
| Existing app adoption | [App Adoption](docs/app-adoption.md) |
| Fixture apps | [Fixture App Suite](docs/fixture-app-suite.md) |
| Live readiness | [Live Readiness Lane](docs/live-readiness-lane.md) |
| Production hardening | [Production Hardening](docs/production-hardening.md) |
| Public-release hygiene | [Open Source Readiness](docs/open-source-readiness.md) |
| Change records | [Change Records](docs/changelog/README.md) |

## Release Gate

Run the full gate before merging release, contract, runtime, or docs-navigation
changes:

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
