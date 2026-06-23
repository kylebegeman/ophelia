# Open Source Readiness

Status: active public-release gate

Ophelia is being prepared as a public, fixture-first infrastructure project.
The public repository should describe Ophelia's reusable contract, not one
operator's private host. Real deployment registries, production env files,
provider evidence, and product-specific migration notes belong outside the
public tree until a deliberate adoption or deployment phase starts.

Current publication posture:

- Repository target: `https://github.com/mrbagels/ophelia`
- Visibility: private until the public release decision is made
- Distribution: GitHub only for now, no PyPI release path is configured

## License And Contribution Model

Approved default:

- License: Apache License 2.0
- SPDX identifier: `Apache-2.0`
- Contribution certification: Developer Certificate of Origin

Why Apache-2.0:

- permissive adoption for individuals and companies
- explicit patent grant
- compatible with hosted services, private integrations, enterprise support,
  and commercial add-ons
- familiar to infrastructure users

Why DCO:

- lower overhead than a CLA
- clear contributor certification through signed-off commits
- works well for a small public project before complex relicensing needs exist

Required files now live at the repo root:

- `LICENSE`
- `NOTICE`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `SUPPORT.md`
- `CODE_OF_CONDUCT.md`

## Public Architecture Direction

Open-source Ophelia should present itself as:

- contract-first app manifests
- `ship` CLI as the primary operator and agent surface
- schema-versioned JSON envelopes
- command catalog and action registry
- redaction-first receipts and runtime state
- synthetic fixture app suites
- provider/plugin boundaries
- dry-run plans, confirmation gates, and receipts for mutations
- private live deployment material kept outside the public repository

## Release Gate

Run the strict gate before publishing or merging release-prep changes:

```bash
make open-source-audit-strict
```

Equivalent direct command:

```bash
./cli/ship open-source audit --fail-on-warnings --json
```

The strict command exits non-zero for blockers or warnings. This keeps the
public tree at a zero-warning baseline once release cleanup has landed. For
exploratory reporting on an in-progress branch, run `make open-source-audit` or
pass `--allow-blocked` directly.

CI runs the same strict audit in `.github/workflows/ci.yml`.

## What The Audit Checks

The audit is read-only. It scans tracked text files for:

- missing license or governance files
- private deploy workflow files
- tracked scratchpad notes
- high-confidence secret literals
- private DNS names, private host IPs, personal paths, and SSH targets
- private registry or owner references
- private or legacy product terms that should be reviewed before public release

Use this while preparing a repo that still has blockers:

```bash
./cli/ship open-source audit --allow-blocked --json
```

## Public Surface Rules

Do:

- use fixtures and synthetic app names
- use `example.com` domains
- keep live values outside the repo
- write docs around reusable contracts
- prefer read-only fixture reproductions for bugs
- describe private or internal UIs as optional consumers of Ophelia contracts,
  not as public dependencies or repos users can access

Do not commit:

- `.env` values
- provider tokens
- private keys
- production host registries
- personal workstation paths
- private DNS names or host IPs
- product-specific migration evidence
- one-off deployment automation for a private VPS

## Zero-Warning Policy

The public tree is expected to stay at a zero-warning baseline. Warning findings
are treated as release failures in `make open-source-audit-strict`, not as
long-lived review debt.

If a warning appears:

- fix it when the reference is stale, private, or fixture material
- move deployment-specific material outside the public tree
- add a narrowly scoped audit exception only after documenting why the public
  repository needs that exact term
