# Ophelia LLM Start Here

This directory is the first stop for agents working in the Ophelia repository.
Use it to discover the public contracts before editing code or running
operator commands.

## Mission

Ophelia is a fixture-first VPS deployment control plane. It validates app
manifests, renders runtime bundles, plans and gates risky operations, records
receipts, and exposes JSON surfaces for humans, automation, Lumen, and agents.

Do not use private deployments as examples. Work from public fixtures,
`example.com` domains, and synthetic app names unless an operator explicitly
provides sanitized live material for an approved migration or deployment phase.

## Read In This Order

1. [Repository README](../../README.md)
2. [Platform Handbook](../platform-handbook.md)
3. [Architecture](../architecture.md)
4. [Manifest Spec](../manifest-spec.md)
5. [Preflight And Safety](../preflight-and-safety.md)
6. [Fixture App Suite](../fixture-app-suite.md)
7. [Job And Action API](../job-action-api.md)
8. [Open Source Readiness](../open-source-readiness.md)

## Stable Discovery Commands

Prefer machine-readable commands:

```bash
./cli/ship self-test --json
./cli/ship commands catalog --json
./cli/ship schema manifest --json
./cli/ship actions --json
./cli/ship open-source audit --json
```

Fixture validation:

```bash
make validate-examples
make validate-manifests
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
make render-examples
make render-manifests
make live-drills-fixtures
make live-hydration-reviewed-fixture
make production-hardening-fixtures
```

## Safety Rules For Agents

- Prefer read-only commands.
- Do not run mutating commands unless the user explicitly asks and the matching
  plan produced a confirmation token.
- Never print, store, request, or infer secret values.
- Use `--json` where available.
- Treat `kind`, `schema_version`, `operation`, and `operation_id` as contract
  fields.
- Keep examples synthetic and public-safe.
- Update docs and `docs/changelog/INDEX.md` when behavior or contracts change.

## Common Task Routing

| Task | Start with |
| --- | --- |
| Understand manifest fields | `docs/manifest-spec.md`, then `./cli/ship schema manifest --json` |
| Inspect command contracts | `./cli/ship commands catalog --json` |
| Validate fixture behavior | `make validate-fixtures` |
| Check public release hygiene | `make open-source-audit-strict` |
| Plan app adoption | `./cli/ship app adoption plan <app> --repo-path <path> --json` |
| Review readiness | `./cli/ship live-readiness run ... --json` |
| Prepare live evidence | `./cli/ship live-hydration report ... --json` |
| Review operation history | `./cli/ship receipts list --json` |

## Data Boundaries

Public repo data may include:

- synthetic fixtures
- public example manifests
- redacted receipts
- schema examples
- non-secret provider shape

Public repo data must not include:

- `.env` values
- production database dumps
- provider tokens
- private keys
- private hostnames or IPs
- personal workstation or home-directory paths
- private product migration notes
