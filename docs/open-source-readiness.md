# Open Source Readiness

Status: active release-prep track

Ophelia can be opened to the public, but the repository should not be published
until the public surface is separated from private deployment history. The
source-of-truth contract, fixture suite, command catalog, safety gates, and
read-only readiness lanes are good foundations for an open project. The current
blockers are mostly release hygiene: license choice, private host/product
references, legacy scratchpad notes, and private VPS deployment automation.

## License Recommendation

Recommended default: Apache License 2.0.

Why:

- It is OSI-approved and has SPDX identifier `Apache-2.0`.
- It is permissive, so individuals and companies can use, modify, and distribute
  Ophelia with low adoption friction.
- It includes an explicit patent grant, which is useful for infrastructure and
  control-plane software where commercial users may care about patent posture.
- It leaves room for hosted services, private integrations, enterprise support,
  and commercial add-ons later.

Alternatives:

- MIT: simpler and familiar, but less explicit about patent rights.
- AGPL-3.0: useful only if the goal is to force network-service modifications
  to be shared. That would protect openness more strongly but reduce adoption
  for companies and operators who want permissive infrastructure tooling.
- GPL/LGPL: stronger copyleft than Ophelia likely needs and less aligned with
  broad deployment-tool adoption.

Do not add the final `LICENSE` file until the maintainer approves the license.
Once external contributors exist, relicensing becomes harder unless a CLA or
other explicit contributor agreement is in place.

References:

- Open Source Initiative Apache-2.0 text: <https://opensource.org/license/apache-2-0>
- Apache Software Foundation Apache-2.0 page: <https://www.apache.org/licenses/LICENSE-2.0>
- SPDX Apache-2.0 identifier: <https://spdx.org/licenses/Apache-2.0>
- Open Source Initiative MIT text: <https://opensource.org/license/mit>
- GitHub licensing guidance: <https://docs.github.com/articles/licensing-a-repository>

## Contribution Model

Recommended default: Apache-2.0 plus DCO.

The DCO path keeps contribution overhead low while requiring contributors to
certify they have the right to submit their work. A CLA is worth considering
only if Ophelia needs strong relicensing control, commercial dual licensing, or
formal company contribution workflows.

Before public launch, add:

- `CONTRIBUTING.md` with development setup, test commands, DCO sign-off, review
  expectations, and fixture-first behavior.
- `SECURITY.md` with private vulnerability reporting instructions.
- `CODE_OF_CONDUCT.md`, likely Contributor Covenant, if the repo will accept
  public issues and pull requests.
- `SUPPORT.md` describing what is community-supported versus maintainer-owned.

References:

- Developer Certificate of Origin: <https://developercertificate.org/>
- Contributor Covenant: <https://www.contributor-covenant.org/>
- GitHub security policy docs: <https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository>

## Public Architecture Direction

Open-source Ophelia should present itself as a fixture-first deployment control
plane, not as a snapshot of one maintainer's VPS.

Keep:

- contract-first app manifests
- `ship` CLI as the primary operator and agent surface
- schema-versioned JSON envelopes
- command catalog and action registry
- redaction-first receipts and runtime state
- synthetic fixture app suites
- provider/plugin boundaries
- dry-run plans, confirmation gates, and receipts for mutations

Change before public release:

- Rewrite README quick starts around fixture apps and `example.com` domains.
- Move private migration runbooks, scratchpad notes, private host docs, and old
  product deployment notes out of the public docs surface.
- Replace hardcoded personal paths, private DNS, public IPs, and private GHCR
  owners with generic examples or configurable inputs.
- Convert private deploy workflows into disabled templates or remove them from
  the public repository.
- Keep retained-product adoption artifacts private until those apps are
  intentionally migrated or deployed through Ophelia.

## Release Gate

Run:

```bash
./cli/ship open-source audit --allow-blocked --json
```

Use `--allow-blocked` while the repo is still being prepared. For the final
release gate, run:

```bash
./cli/ship open-source audit --json
```

The strict command must pass before publishing the repository. The audit is
read-only and scans tracked files when Git metadata is available.

## Current Public-Release Blockers

The first audit intentionally reports blockers instead of editing them:

- no `LICENSE` file yet
- no security/contribution/support/community files yet
- private deployment workflow still tracked
- tracked scratchpad notes still present
- private DNS, personal paths, private host/IP references, and old product
  names still appear in private operational docs, active manifests, config,
  README, and command catalog examples

These are cleanup tasks, not architecture blockers. The architecture should move
forward around fixtures, generic providers, and Ophelia's contract.

The public example/spec/test surface has already been sanitized. Remaining
blockers are concentrated in private operational inventory and decisions that
should be approved before changing active deployment workflows.

## Prep Phases

1. Release hygiene foundation: audit command, readiness doc, changelog, Make
   targets.
2. Public surface rewrite: fixture-first README, generic command examples,
   generic manifests, and private material moved or removed.
3. Governance files: approved license, security policy, contributing guide,
   support policy, code of conduct, and DCO guidance.
4. CI/public checks: strict open-source audit in CI, fixture suite, docs check,
   unit tests, and schema/catalog smoke tests.
5. Packaging polish: install instructions, versioning policy, changelog
   discipline, release process, and optional PyPI packaging.
6. Launch review: audit passes, docs are coherent, private deployment material
   is absent, and the maintainer approves publishing.
