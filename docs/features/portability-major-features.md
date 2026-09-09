# Ophelia Portability Major Features

Status: detailed implementation specification for a later major-feature phase

Baseline: Ophelia 0.6.17, current `0.6.x` architecture and public contracts

Date: 2026-08-30

Audience: Ophelia implementers, Lumen integrators, operators, reviewers, and
implementation agents

<div class="artifact-actions">
  <a href="portability-major-features.md">View Markdown source</a>
  <a href="portability-major-features.md" download>Download Markdown source</a>
</div>

## Purpose And Authorization Boundary

This document promotes the selected portability taxonomy into a durable build
specification:

| Feature | Name |
| ---: | --- |
| 1 | Portable App Pack System |
| 2 | Data Export And Import Pipeline |
| 3 | Restore Drill System |
| 4 | Cutover Orchestrator |
| 5 | Per-App Isolation Refactor |
| 6 | Host Bootstrap And Reconcile |
| 8 | Private Operator UI Adapter Surface |

These are major completion tracks over an existing platform. Ophelia already
has app-pack fields, export bundles, isolated import previews, restore-drill
receipts, cutover checkpoints, traffic providers, per-app network foundations,
an independent host daemon, a journaled manifest v2 executor, and read-only UI
adapter payloads. The work below turns those foundations into coherent,
truthful, recoverable product capabilities.

This document does not itself authorize implementation. If Kyle explicitly
starts the later major-feature phase, this specification authorizes
implementation and verification against public fixtures, temporary runtime
roots, local containers, and disposable hosts within that phase. Until then,
it is a planning and review reference.

Even after that phase starts, this specification does not authorize production
deployment, production data movement, DNS changes, credential rotation, host
enrollment, destructive restore, or any other live mutation. Every future live
step requires a separately reviewed plan and its exact approval evidence.

### Taxonomy Note For Feature 6

**Feature 6, Host Bootstrap And Reconcile, is the working interpretation of
the selected number and name pairing.** The old archived taxonomy omitted item
6, while the active architecture and command set now contain clear bootstrap,
daemon installation, host inventory, recovery, and reconciliation foundations.
This document uses those current contracts as authority. If a later approved
taxonomy record clarifies the number, update the mapping without weakening the
feature contract defined here.

### Active Sources Of Truth

Implementation should begin with the current documents and executable
contracts, especially:

- [Platform Strategy And Technical Architecture](../product/ophelia-platform-strategy.md)
- [Roadmap](../ROADMAP.md)
- [Architecture](../architecture.md)
- [Manifest Spec](../manifest-spec.md)
- [Portable App Pack Spec](../portable-app-pack-spec.md)
- [Stateful App Migration Runbook](../stateful-app-migration-runbook.md)
- [Ophelia Host Daemon](../daemon.md)
- [Preflight And Safety](../preflight-and-safety.md)
- [Job And Action API](../job-action-api.md)
- [Operator Console Adapter](../operator-console-adapter.md)
- [Fixture App Suite](../fixture-app-suite.md)

The archive is historical context only. Source, tests, command discovery, and
the active documents above win when they disagree with an older plan.

## Architectural Commitments

Every feature in this specification must preserve these `0.6.x` commitments.

### Manifest v1 And v2 Remain Distinct

Manifest v1 is the compatibility boundary that currently owns the portable
`pack`, `host_requirements`, `networking`, `data`, and `hooks` fields. Manifest
v2 is the strict workload and revision contract. It rejects unknown keys,
requires explicit lifecycles, uses immutable artifacts, and runs through the
journaled execution kernel.

Portability work must compile both supported source versions into a canonical
domain model. It must not silently reinterpret v1 as v2 or accept portability
keys in v2 without an intentional schema revision, migration command, and
compatibility tests.

### `opheliad` Is The Single-Host Mutation Authority

Completed host and application mutations run through the same journaled
kernel, whether initiated by `ship`, the Unix-socket API, recovery mode, or
Lumen. Direct execution remains a separately identified local recovery lane,
not a second implementation.

Each host owns its local runtime effects. Cross-host workflows coordinate
host-local operations and link their receipts. They do not bypass either
host's policy, lease, idempotency, or recovery rules.

### Lumen Is The Supervisory Control Plane

Lumen owns Projects, targets, Decisions, Jobs, Runs, Activity, Incidents, and
the private operator experience. Ophelia owns manifests, plans, runtime
execution, host observations, traffic and data effects, events, and receipts.

A normal Lumen mutation must carry a short-lived, signed, exact-plan Decision
claim. A local recovery mutation must carry separately typed break-glass
evidence. A Decision identifier or caller-provided actor label is not approval
by itself.

### Plans, Approvals, Effects, And Receipts Stay Truthful

Read-only planning may write only immutable operation staging and append-only
plan evidence. It may not change protected desired state, active runtime,
Docker, Caddy, data stores, providers, or the rebuildable state projection.

Every mutating receipt must describe what actually happened. Until all legacy
names acquire their full semantics, receipts use an explicit effect such as:

| Effect | Meaning |
| --- | --- |
| `preview` | Evidence or an isolated preview was written. No runtime or data target changed. |
| `files_only` | Local artifacts changed, but no application runtime was activated. |
| `data_restored_isolated` | Data was restored only into a quarantined drill or candidate target. |
| `runtime_activated` | Observed application runtime reached the approved state. |
| `traffic_changed` | A named provider change was observed after apply. |

`status: succeeded` is never enough. The receipt also carries observed outcome,
artifact digests, authorization kind, plan linkage, checks, compensation, and
remaining recovery obligations.

### Safety Is Shared, Not Reimplemented Per Command

All features use the canonical validation, path containment, archive safety,
redaction, policy, approval, operation journal, lease, subprocess deadline,
cancellation, and receipt primitives. No portability feature adds an arbitrary
shell endpoint or a private mutation path.

Secrets remain references or presence and freshness metadata in plans,
artifacts, events, receipts, logs controlled by Ophelia, and UI projections.
Imported artifacts, repositories, manifests, hooks, verifier output, provider
responses, and application logs are treated as untrusted input.

## Status Matrix

| # | Major feature | Current foundation | Completion gap | Primary dependency | Target status |
| ---: | --- | --- | --- | --- | --- |
| 1 | Portable App Pack System | v1 pack fields; `pack init`, `validate`, and `explain`; readiness and runbook reports | One canonical, versioned, digest-bound pack model for supported v1 and v2 inputs | Manifest boundary and validation primitives | Planned |
| 2 | Data Export And Import Pipeline | Export plan/create, bundle format v1, checksums, local volume archives, optional `pg_dump`; import plan and isolated preview | Consistent encrypted exports and real resumable import into an isolated target candidate | 1, 5, 6 | Planned |
| 3 | Restore Drill System | Confirmation-bound extraction, app-owned volume verifiers, backup rehearsal, drill receipts | Full disposable data and service restore with objective verification, cleanup, RPO, and RTO evidence | 1, 2, 5, 6 | Planned |
| 4 | Cutover Orchestrator | Cutover checkpoint, target health checks, file and Cloudflare traffic providers, receipt-backed traffic rollback | Durable cross-host state machine for freeze, final sync, activation, traffic, verification, and compensation | 2, 3, 5, 6 | Planned |
| 5 | Per-App Isolation Refactor | v1 per-app internal network opt-in; v2 app, edge, and data networks; strict v2 security and resource defaults | Measured isolation across network, credentials, data, runtime privileges, and migration | Kernel and manifest contracts | Planned |
| 6 | Host Bootstrap And Reconcile | Daemon installer, strict config, systemd service, host inventory/readiness, encrypted host-control backup and recovery, journal integrity and health loop | Idempotent host foundation profile plus observable plan, apply, drift, repair, and rollback | Journaled kernel | Planned, working interpretation |
| 8 | Private Operator UI Adapter Surface | Read-only `ophelia.lumen.*` CLI and local API payloads, command catalog, dashboard, console data, redaction | Stable opheliad-backed projections, event cursors, Lumen provider integration, and approval-safe operator actions | 6 for host truth; 2 through 4 for full workflow coverage | Planned |

## Shared Contract Baseline

The major features add operation-specific data without creating bespoke
envelope systems.

### CLI And API Baseline

Current public discovery remains authoritative:

```bash
ship commands catalog --json
ship actions --json
ship schema manifest --json
ship self-test --json
```

Canonical single-host operations use the `opheliad` API resources documented
for `0.6.x`:

```text
POST /v1/plans
POST /v1/operations
GET  /v1/operations/{operation_id}
POST /v1/operations/{operation_id}/cancel
GET  /v1/events?cursor=<cursor>
GET  /v1/apps/{app}/{environment}
GET  /v1/observations/latest
```

Portability operation classes are added to capability negotiation before a
client may invoke them. Every mutation requires an idempotency key, an exact
approved plan, an authenticated actor derived by the server, and an operation
scope advertised by the host.

### JSON Envelope Baseline

Existing CLI compatibility surfaces retain the generic envelope kinds:

- `ophelia.report` for read-only explanation and validation
- `ophelia.plan` for deterministic plans
- `ophelia.receipt` for terminal operation evidence
- `ophelia.command_catalog` for command discovery

The journaled manifest v2 path also has its existing strict kernel artifacts:
`ophelia.manifest-v2-plan`, `ophelia.manifest-v2-plan-evidence`, and
`ophelia.manifest-v2-operation`. Portability plans may reference those artifacts
by id and digest, but must not flatten or replace their revision truth.

Every major-feature payload includes, where applicable:

```json
{
  "schema_version": 1,
  "kind": "ophelia.plan",
  "operation": "app.export.plan",
  "operation_id": "plan_fixture_01",
  "app": "demo-service",
  "environment": "staging",
  "status": "ok",
  "digest": {},
  "blockers": [],
  "warnings": [],
  "checks": [],
  "artifacts": [],
  "inputs_redacted": true
}
```

New nested artifacts use their own `kind` and version. Readers reject unknown
major versions and ignore unknown additive fields only where the contract says
that is safe. Artifact bytes are referenced by digest and bounded path, not
inlined into ordinary UI payloads. Each new artifact schema must also be
available through machine-readable discovery rather than existing only in
prose; the current `ship schema manifest` surface is the model, not sufficient
coverage by itself.

### Core State Identities

The shared identity chain is:

```text
request_id
  -> plan_id + plan_digest
  -> approval evidence
  -> operation_id
  -> host event cursor(s)
  -> artifact, revision, export, drill, or cutover ids
  -> receipt_id
```

Retries with the same idempotency key and same canonical request return the
same operation. Reuse with different content fails. Terminal evidence links
all related host-local operations and never stores a reusable approval secret.

## 1. Portable App Pack System

### Product Outcome

An application repository can declare one complete, inspectable portability
contract that answers what runs, what data must survive, what a host must
provide, how consistency is obtained, how data is verified, and what blocks
movement. Ophelia compiles that declaration into an immutable pack lock that
every later export, import, drill, cutover, and UI surface consumes.

### Current Foundation

- Manifest v1 supports `pack`, `host_requirements`, `networking`, `data`, and
  `hooks`.
- `ship pack init` previews scaffolding and writes only with `--write`; `--force`
  is required to overwrite an existing scaffold file.
- `ship pack validate` and `ship pack explain` emit redacted
  `ophelia.report` envelopes.
- Pack validation already evaluates critical data contracts, off-site backup
  expectations, hook containment, command redaction, and readiness scoring.
- Hook paths and verifier files are validated, scaffolded, and copied into
  runtime support bundles, but the current generic portability operations do
  not execute the pack lifecycle hooks.
- Static and service scaffolds, app-owned checks, hooks, and fixture manifests
  provide the public test substrate.
- Manifest v2 already supplies strict workloads, immutable artifacts, explicit
  networks, secret references, security settings, and revision identity, but
  it does not yet carry the v1 portability model.

### Remaining Completion Scope

1. Introduce a frozen canonical `AppPack` domain model independent of source
   manifest version.
2. Keep the v1 adapter compatible with existing pack fields and inferred addon
   contracts, while marking every inference as non-authoritative for critical
   movement.
3. Add portability to v2 through an intentional strict schema revision or a
   separately versioned app-owned pack document. Do not weaken v2 unknown-key
   rejection.
4. Compile both supported source forms into `ophelia.pack.lock` with source
   version, canonical digest, artifact locks, data items, consistency class,
   hooks, verifiers, host requirements, routes, and redacted secret bindings.
5. Make every portability consumer accept the canonical lock rather than
   reparsing manifest dictionaries independently.
6. Add `ship pack migrate` and `ship pack diff` so a reviewer can see changes
   before adopting a new pack schema or v1-to-v2 source shape.
7. Split the current portability hotspot behind stable compatibility exports,
   with characterization tests before responsibilities move.

### Non-Goals

- Bundling application source, build systems, live secrets, or private data.
- Silently converting a valid v1 manifest into v2.
- Executing arbitrary repository scripts because they appear in a pack.
- Treating a high readiness score as permission to ignore a blocker.
- Making one app pack portable to a host that fails its declared requirements.

### Contracts, CLI, API, And JSON Artifacts

Existing commands remain:

```bash
ship pack init --app demo-service --environment staging --json
ship pack validate /srv/demo-service/.ophelia.yml --json
ship pack explain /srv/demo-service/.ophelia.yml --json
```

Completion adds read-only commands:

```bash
ship pack lock /srv/demo-service/.ophelia.yml --output build/ophelia.pack.lock.json --json
ship pack diff /srv/demo-service/.ophelia.yml --against build/ophelia.pack.lock.json --json
ship pack migrate /srv/demo-service/.ophelia.yml --to <supported-version> --json
```

The lock artifact has `kind: ophelia.pack.lock`, its own schema version, a
canonical `pack_digest`, and these bounded sections:

- `identity`: app, environment, source manifest version, source digest
- `runtime`: revision and artifact requirements
- `host_requirements`: normalized capability and capacity predicates
- `data_items`: stable ids, ownership, criticality, consistency, export,
  import, verification, retention, and dependencies
- `hooks` and `verifiers`: typed argv or contained repository paths plus digests
- `routes` and `networks`: canonical desired topology
- `secret_bindings`: names, references, targets, presence requirements, never
  values
- `compatibility`: inferred fields, deprecated fields, and reader requirements

`GET /v1/capabilities` advertises supported pack lock versions. Planning embeds
or references the exact lock digest, so changing a hook, verifier, artifact, or
data contract invalidates the old approval.

### State And Data Model

| Entity | Required identity and state |
| --- | --- |
| `AppPack` | app, environment, source version, pack version, canonical digest |
| `PackArtifact` | stable name, type, immutable digest, source provenance |
| `DataItem` | stable id, class, owner, source, consistency mode, dependencies |
| `PackHook` | lifecycle phase, contained source, digest, typed inputs, deadline |
| `PackVerifier` | target data item or service, assertion contract, deadline |
| `HostPredicate` | capability, comparison, required value, evidence freshness |
| `SecretBinding` | opaque ref, target scope, required presence and freshness |

Generated locks are immutable evidence. The app-owned source remains the
desired declaration. `state.db` may index locks for queries, but it must not
invent or overwrite a source contract.

### Safety And Redaction Invariants

- All app, environment, data-item, hook, route, and artifact identifiers use
  canonical grammars before path or renderer use.
- Repository paths resolve beneath the pack root after symlink resolution.
- Hooks are allowlisted by lifecycle phase and executed as argv with a bounded
  environment, deadline, output quota, and cancellation behavior.
- Lock files contain secret refs and env-key names only.
- Command metadata passes command-string redaction before reports, locks, or
  UI projections.
- Unsupported pack or manifest versions fail before semantic parsing.
- A critical data item without export, import, and verification is a blocker.

### Failure And Recovery Semantics

Lock compilation is read-only except for an explicitly requested output path.
It uses atomic replacement and never changes app runtime. A failed compile
leaves the previous lock intact. Consumers reject a lock whose source digest,
artifact digest, or required reader version no longer matches.

If v1 inference cannot produce a complete critical contract, the pack remains
inspectable but movement is blocked. Migration produces a candidate and diff;
it never overwrites the source manifest without an explicit local write flag.

### Dependencies

This feature depends on current manifest validators, canonical identifiers and
paths, command redaction, and fixture manifests. Features 2, 3, and 4 depend on
its canonical data and verification model. Features 5, 6, and 8 consume its
network, host, and summary projections.

### Phased Implementation Slices

1. Characterize all current v1 pack reports and fixture outputs.
2. Introduce the canonical domain model and v1 adapter behind existing commands.
3. Specify the deliberate v2 source contract and migration path.
4. Emit and validate digest-bound lock artifacts.
5. Move export, import, drill, cutover, readiness, and UI consumers to the lock.
6. Decompose compatibility modules only after parity tests pass.

### Test And Acceptance Criteria

- Every committed fixture compiles deterministically; identical input yields
  byte-identical canonical JSON and digest.
- v1 reports remain compatible except for documented additive fields.
- v2 rejects portability keys until the selected strict schema supports them.
- Unknown versions, unknown required fields, traversal, symlink escape, command
  injection, and secret-shaped values fail or redact before output.
- Changing any hook, verifier, artifact, route, data item, or host requirement
  changes the pack digest.
- The fixture suite covers static, stateless service, stateful volume,
  PostgreSQL, Redis, multi-workload, incomplete, and unsupported contracts.
- `make open-source-audit-strict` finds no private values in generated examples.

### Definition Of Done

Feature 1 is complete when every supported v1 and v2 app shape produces one
validated, deterministic, redaction-safe pack lock; every portability workflow
uses that lock; critical incompleteness blocks movement; and compatibility is
proven by fixture and contract tests.

## 2. Data Export And Import Pipeline

### Product Outcome

Ophelia can capture every declared application data item at a known consistency
point, publish a verified portable artifact, transfer or retrieve it safely,
and restore it idempotently into an isolated target candidate. Export does not
imply recoverability, and import does not imply activation.

### Current Foundation

- `ship app export plan` is read-only and confirmation-binds export create.
- `ship app export create` writes bundle format v1 with
  `kind: ophelia.export.metadata`, source metadata, redacted runtime files,
  checksums, receipts, local static or volume archives, a plain `.tar`, optional
  `.tar.zst`, and optional allowlisted `pg_dump`.
- Export create does not mutate source runtime, Caddy, DNS, or providers.
- `ship app import plan` reads a directory, metadata JSON, `.tar`, or `.tar.zst`
  and reports host match, networks, volumes, routes, env shape, restore commands,
  and checks.
- Current `ship app import apply` is a confirmation-bound rehearsal preview. It
  writes `import-previews/<id>` and does not restore production data, activate a
  runtime, or change Caddy or DNS.
- Archive containment, external symlink checks, redaction, backup freshness,
  receipts, host placement reports, and the opheliad journal are available
  foundations.

### Remaining Completion Scope

1. Define export bundle format v2 while retaining safe inspection and rehearsal
   support for v1 bundles.
2. Bind every export to a canonical pack lock, source revision, consistency
   class, data-item inventory, tool versions, checksums, and completion status.
   Normalize archive ordering, timestamps, ownership, groups, and modes where
   deterministic bytes are part of the contract.
3. Add coordinated consistency modes: immutable, application quiesce, database
   snapshot or dump, crash-consistent, and externally managed.
4. Encrypt sensitive archives before off-host publication with a versioned,
   authenticated envelope. Record recipient key references and fingerprints,
   provider object references, and ciphertext digests, not credentials or
   private identities.
5. Move export and import mutations into journaled opheliad operation classes
   with deadlines, cancellation, leases, restart recovery, and idempotency.
6. Implement actual import into candidate-only databases, volumes, static
   roots, and revision storage. Resolve target secret refs locally without
   copying values into the bundle.
7. Verify archive integrity, quotas, host fit, tool compatibility, target free
   space, restored data structure, and post-import app checks before declaring
   the candidate usable.
8. Keep activation and traffic outside import. Feature 4 owns cutover.

### Non-Goals

- Copying live secrets into ordinary export bundles.
- Claiming application consistency for an uncoordinated filesystem copy.
- Activating target runtime or changing traffic as an import side effect.
- Supporting every database or object store without a declared adapter.
- Treating external SaaS data as portable when the pack only declares a
  dependency.
- Overwriting populated target data unless a separately scoped replacement
  plan proves ownership and rollback.

### Contracts, CLI, API, And JSON Artifacts

Compatibility commands remain:

```bash
ship app export plan demo-service --environment staging --json
ship app export create demo-service --environment staging --confirm <token> --json
ship app import plan ./demo-service.staging.export.tar.zst --json
ship app import apply ./demo-service.staging.export.tar.zst --mode rehearsal --confirm <token> --json
```

The completed import adds an explicit candidate mode. It must not overload the
current rehearsal behavior silently:

```bash
ship app import plan ./demo-service.staging.export.tar.zst --mode candidate --json
ship app import apply ./demo-service.staging.export.tar.zst --mode candidate --confirm <token> --json
```

Encrypted export and import add typed key-reference inputs to the same plan:

```bash
ship app export plan demo-service \
  --environment staging \
  --encryption-profile age-v1 \
  --recipient-ref recovery-primary \
  --json
ship app import plan <sealed-bundle> \
  --mode candidate \
  --identity-ref recovery-primary \
  --json
ship app export rotate-encryption plan <export-id> \
  --add-recipient-ref recovery-next \
  --json
```

The named refs resolve through host configuration or a secret provider. Plans
report ref names, public fingerprints, and presence only. Apply never accepts a
private identity value on argv.

The same operations are accepted through `/v1/plans` and `/v1/operations`
only when host capabilities advertise the required bundle and data adapters.

Bundle v2 contains bounded, digest-addressed entries such as:

```text
manifest.json                  ophelia.export.metadata
pack.lock.json                 ophelia.pack.lock
checksums.sha256
source-host.json
consistency.json               ophelia.export.consistency
runtime/
data/<data-item-id>/metadata.json
data/<data-item-id>/<sealed-or-public-artifact>
receipts/export-plan.json
receipts/export-create.json
```

Sensitive bundles use an `age-v1` authenticated-encryption profile unless a
later profile is added through capability negotiation. A cleartext,
non-sensitive `ophelia.export.envelope` sits beside the ciphertext and records
the envelope version, algorithm profile, export id, recipient reference ids and
public fingerprints, ciphertext size and SHA-256 digest, creation time, and
inner-manifest kind. Plaintext item digests and application metadata stay
inside the encrypted payload. Private keys and raw recipient strings supplied
as secrets never enter either manifest.

Multiple recipients support staged key rotation. A confirmation-bound rewrap
or re-encrypt operation writes and verifies a new immutable object before the
old recipient or object becomes retirement-eligible. Readers select a local
private identity by opaque key reference, authenticate the envelope, verify the
outer ciphertext digest, decrypt into private managed staging, then verify the
inner checksum manifest before import.

Import writes `ophelia.import.candidate` metadata with `import_id`, source
export digest, target host, candidate paths, adapter versions, data-item states,
resolved secret-binding presence, verification status, and cleanup policy.
Plan and receipt envelopes retain `operation: app.export.*` and
`operation: app.import.*` for CLI compatibility.

### State And Data Model

| Entity | Important fields |
| --- | --- |
| `Export` | export id, pack digest, source revision, consistency state, status |
| `ExportItem` | data-item id, adapter, logical size, digest, encryption, status |
| `ProviderObject` | provider kind, opaque object ref, digest, retention metadata |
| `EncryptionEnvelope` | profile, recipient refs and fingerprints, ciphertext digest, inner manifest kind, rotation generation |
| `Import` | import id, export digest, target host, mode, state, operation id |
| `ImportItem` | source item, candidate target, adapter, restore cursor, verification |
| `ImportCandidate` | isolated runtime and data roots, revision ref, ready flag, expiry |

Suggested export states are `planned`, `quiescing`, `capturing`, `verifying`,
`publishing`, `complete`, `failed`, and `cleanup_required`. Suggested import
states are `planned`, `validating`, `staging`, `restoring`, `verifying`,
`candidate_ready`, `failed_compensated`, and `failed_uncompensated`.

### Safety And Redaction Invariants

- Validate archive member paths, types, symlinks, counts, expanded sizes,
  compression ratios, and digests before extraction.
- Extract relative to trusted directory descriptors and atomically promote only
  a fully verified candidate.
- Never follow an export bundle path into active runtime or a populated target.
- Resolve secret refs on the target and record names, presence, and freshness
  only.
- Quiesce and unquiesce hooks use the typed pack contract, bounded environment,
  deadlines, and captured redacted output.
- Database commands are fixed adapters or pack-approved argv, never a shell
  string from bundle metadata.
- Encryption keys, database contents, provider credentials, and raw hook output
  do not enter plans, journals, receipts, or UI summaries.
- Plaintext data exists only in a mode `0700` managed staging directory with
  mode `0600` files, preferably on a private memory-backed filesystem when size
  permits. It is never published off host, and cleanup is journaled and
  verified after encryption, decryption, cancellation, or failure.
- Cleartext envelope metadata exposes ciphertext identity and public recipient
  fingerprints only. Equality-sensitive plaintext digests remain encrypted.
- Export completeness is false when any required data item is missing, stale,
  unverifiable, or captured below its declared consistency requirement.

### Failure And Recovery Semantics

An export failure must attempt the declared unquiesce compensation and record
whether it succeeded. Partial bundles remain quarantined and cannot satisfy
backup freshness. Publication is atomic only after checksums and required item
verification succeed.

Import journals progress before each side effect. Restart resumes from verified
item checkpoints. Failure before candidate promotion removes or quarantines
only resources created by that import. Failure never edits active routes or
active revision state. If compensation cannot prove cleanup, the operation ends
`failed_uncompensated` with exact managed resource ids and a recovery plan.

Interrupted encryption or upload never publishes an incomplete final object.
The candidate ciphertext remains in managed staging until its authenticated
envelope and provider observation agree, then it is atomically published. On
restart, Ophelia resumes the same generation or securely cleans its registered
plaintext and ciphertext candidates. Rotation preserves the last verified
decryptable object until the replacement has been decrypted and verified with
the intended recovery identity.

### Dependencies

Feature 1 supplies the canonical data and adapter contract. Feature 5 supplies
candidate isolation. Feature 6 supplies host capability evidence, storage
roots, journaled execution, and recovery. Feature 3 consumes a candidate import
to prove recoverability, and feature 4 consumes a verified candidate for
cutover.

### Phased Implementation Slices

1. Characterize bundle v1 and split archive, export, import, provider, and
   receipt responsibilities behind compatibility exports.
2. Add canonical bundle v2 validation and deterministic export metadata.
3. Add consistency adapters and journaled export, initially for fixture files,
   Docker volumes, and synthetic PostgreSQL.
4. Add encryption and a fixture provider, then one approved off-site provider.
5. Add isolated candidate import with resumable item checkpoints.
6. Add host-to-host transfer and clean-target integration tests.

### Test And Acceptance Criteria

- Deterministic fixture exports have stable metadata and checksum manifests.
- Bundle v1 remains inspectable and rehearsal-safe; unsupported versions fail
  closed.
- Traversal, symlink escape, archive bombs, duplicate members, device nodes,
  digest mismatch, truncation, and wrong decryption identity are rejected.
- Ciphertext tampering, wrong recipient, unavailable key reference, interrupted
  encryption or upload, recipient rotation, and failed plaintext cleanup have
  deterministic failed or recovery-required outcomes.
- Kill after every export and import journal boundary, then resume without
  duplicate side effects.
- Seed known secrets in source env, Compose, hook output, provider responses,
  and database connection strings; prove they do not enter controlled evidence.
- Corrupt or incomplete required items never produce backup-eligible success.
- Candidate import changes no active app, Caddy, DNS, or provider state.
- A filesystem scan after success, cancellation, injected failure, and restart
  finds no unregistered plaintext export remnants.
- A second identical request returns the same operation and candidate; a
  different request using the same idempotency key fails.

### Definition Of Done

Feature 2 is complete when every declared critical fixture data item can be
captured consistently, encrypted when required, verified, transported, and
resumed into an isolated clean-host candidate with truthful receipts, while
active source and target runtime remain unchanged.

## 3. Restore Drill System

### Product Outcome

A restore drill proves that a specific export can reconstruct a useful
application environment without touching the live app. The result includes
data integrity, service behavior, cleanup outcome, measured recovery point and
time, and enough evidence for a reviewer to decide whether the backup is
actually recoverable.

### Current Foundation

- `ship app restore-drill plan|apply` is dry-run-first and confirmation-bound.
- `ship backup rehearse plan|apply` supports a concrete directory, `.tar`, or
  `.tar.zst` bundle and a manifest-facing verifier flow.
- Current apply safely extracts into an isolated drill directory, runs declared
  volume verifier commands, and writes a restore-drill receipt.
- `ship restore-drills list|show`, readiness, drift, backup status,
  observability, and the state index consume drill receipts.
- Current drills do not restore over production or start a full service stack.
- The product-operations recovery path already demonstrates stronger strict
  contracts, digest-bound datasets, isolated PostgreSQL targets, service boot,
  health verification, fencing, and cleanup. The generic pack drill should
  reuse those domain and execution lessons instead of creating a third
  incompatible recovery engine.
- Fixture live-drill profiles are read-only expected-state checks. They are a
  complementary regression harness, not the same thing as a data restore drill.

### Remaining Completion Scope

1. Unify app restore drill, backup rehearsal, and backup verification around one
   `RestoreDrill` state machine and receipt contract.
2. Allocate a quarantined drill namespace with isolated networks, databases,
   volumes, secrets, routes, resource limits, and expiry.
3. Import the selected export through Feature 2, then start only the workloads
   required by the drill contract.
4. Run data-item verifiers, workload readiness, app-owned health and release
   checks, bounded synthetic transactions, and optional internal route probes.
5. Measure source backup age, declared RPO result, restore duration, declared
   RTO result, logical item counts, and cleanup duration.
6. Preserve a bounded evidence set and tear down all drill resources unless a
   failure policy explicitly quarantines them for investigation.
7. Add scheduled drill requests through opheliad and project freshness into
   Lumen without making Lumen the scheduler of host-local recovery work.

### Non-Goals

- Restoring over active production data.
- Using public production DNS for a rehearsal.
- Declaring success from archive readability alone.
- Running undeclared migrations or synthetic writes against external services.
- Retaining unlimited drill databases, containers, logs, or extracted data.
- Replacing read-only live-drill profile testing.

### Contracts, CLI, API, And JSON Artifacts

Compatibility commands remain:

```bash
ship app restore-drill plan demo-service --environment staging --source <bundle> --json
ship app restore-drill apply demo-service --environment staging --source <bundle> --confirm <token> --json
ship backup rehearse plan <bundle> --manifest /srv/demo-service/.ophelia.yml --json
ship restore-drills list --app demo-service --json
ship restore-drills show <drill-id> --json
```

The completed plan adds an explicit execution profile:

```bash
ship app restore-drill plan demo-service \
  --environment staging \
  --source <bundle> \
  --profile isolated-services \
  --json
```

Scheduled execution uses a separate confirmation-bound contract:

```bash
ship app restore-drill schedule plan demo-service \
  --environment staging \
  --profile isolated-services \
  --cadence "0 3 * * 0" \
  --overlap forbid \
  --retain-count 6 \
  --json
ship app restore-drill schedule apply <plan-id> --confirm <token> --json
ship app restore-drill schedule status demo-service --environment staging --json
```

Schedule apply is a host-local opheliad mutation accepted through the same
`/v1/plans` and `/v1/operations` resources. It does not create a Lumen-owned
timer; Lumen projects the host schedule and its receipts.

`/v1/plans` and `/v1/operations` accept the same canonical drill request when
the host advertises `restore_drill` and all required data adapters.

The terminal receipt remains `ophelia.receipt` with
`operation: app.restore-drill.apply`. Its `restore_drill` block contains:

- drill id, pack digest, export id and digest, target host, profile
- isolated resource ids and route namespace
- per-data-item restore and verification outcomes
- workload revision, readiness, liveness, and release observations
- RPO and RTO objectives, measurements, and pass or fail
- cleanup status, retained evidence, and quarantine reason
- `effect: data_restored_isolated`

`ophelia.restore_drill.evidence` is the immutable drill-specific artifact.
`ophelia.restore_drills` remains the list envelope and
`ophelia.restore_drill` remains the single-record read envelope for
compatibility.

### State And Data Model

Suggested states:

```text
planned
  -> allocating
  -> importing
  -> starting
  -> verifying
  -> cleanup
  -> succeeded | failed_compensated | failed_uncompensated
```

The drill record binds `drill_id`, `operation_id`, `pack_digest`,
`export_digest`, target host, candidate revision, isolated resources,
verification assertions, timings, and terminal cleanup. The backup is
recoverable only when all required assertions and cleanup obligations satisfy
policy.

A persisted `RestoreDrillSchedule` binds app, environment, profile, cadence,
latest eligible export selection policy, overlap policy, missed-run policy,
retention count and age, next run, last accepted run, and scheduler fencing
generation. The default overlap policy is `forbid`; one delayed tick may be
coalesced after restart, but missed schedules never create an unbounded backlog.

### Safety And Redaction Invariants

- Drill resources use generated names scoped to the drill id and never reuse
  production database, volume, container, network, route, or secret targets.
- Rehearsal routes are private or host-local and cannot claim production
  domains.
- Secrets resolve from drill-scoped refs. Values never enter evidence.
- Verifiers are read-only by default. A bounded synthetic-write verifier must
  target only isolated drill data and declare cleanup.
- Resource quotas, deadlines, log limits, and retention limits are mandatory.
- Application logs are treated as sensitive. Receipts retain bounded assertion
  results and digests, not full logs.
- A failed assertion cannot be converted into a warning when the pack marks it
  required.

### Failure And Recovery Semantics

Every allocation is journaled before use and registered for compensation.
Failure stops further verification, preserves the first useful diagnostic,
then removes resources in reverse dependency order. If cleanup cannot be
verified, the drill ends `failed_uncompensated`, the host reports degraded
capacity, and the resources remain quarantined with an explicit cleanup plan.

A failed drill never invalidates the underlying export artifact automatically,
but it marks that export as not currently proven recoverable. A daemon restart
resumes the drill or its cleanup from the durable journal.

### Dependencies

Feature 1 supplies assertions and data ownership. Feature 2 supplies actual
candidate import. Feature 5 supplies isolation. Feature 6 supplies resource
inventory, scheduling, durable execution, and cleanup reconciliation. Feature
4 requires a recent successful drill for critical cutovers.

### Phased Implementation Slices

1. Normalize existing receipt readers and current extraction-only behavior.
2. Add the isolated drill allocator and cleanup registry.
3. Restore fixture volumes and PostgreSQL through Feature 2 adapters.
4. Start synthetic service workloads and run app-level assertions.
5. Add RPO, RTO, scheduling, retention, and Lumen event projection.
6. Prove the complete lane on a disposable clean host.

### Test And Acceptance Criteria

- A synthetic stateful app restores data and serves expected behavior on an
  isolated route without changing the active app.
- Wrong data counts, corrupt archives, missing secret refs, failed readiness,
  failed app assertions, and exceeded RTO produce failed receipts.
- Kill the daemon at every drill phase and recover without leaking or
  duplicating resources.
- Network inspection proves drill workloads cannot reach unrelated app-private
  networks.
- Cleanup removes every registered drill resource; injected cleanup failure is
  visible as uncompensated state.
- Schedules survive daemon restart, acquire one fenced run at a time, apply the
  declared missed-run policy, and never overlap when policy is `forbid`.
- Retention removes only terminal, expired drill resources and evidence beyond
  the declared count or age; active, quarantined, latest-success, and
  incident-referenced evidence remains protected.
- Drill list, readiness, drift, state index, observations, and UI projection
  agree on the latest successful drill and its age.
- Fixture evidence contains no secret values or private infrastructure data.

### Definition Of Done

Feature 3 is complete when a clean reviewer can select a fixture export, watch
Ophelia restore and run it in isolation, verify declared data and behavior,
measure RPO and RTO, confirm cleanup, and reconstruct the result entirely from
machine-readable evidence. A persisted schedule must also survive restart,
avoid forbidden overlap, recover missed ticks deterministically, and enforce
safe evidence and resource retention.

## 4. Cutover Orchestrator

### Product Outcome

Ophelia can move an application between independent hosts through one durable,
reviewable state machine: verify prerequisites, freeze or quiesce the source,
capture and import the final data delta, activate the target, move traffic,
verify external behavior, retain rollback state, and either commit or
compensate safely.

### Current Foundation

- `ship app cutover plan|apply` produces a confirmation-bound checkpoint and
  optional shared PostgreSQL evidence scaffold.
- Current cutover apply writes evidence only. It does not move Caddy, DNS, data,
  runtime, or source retention state.
- `ship app traffic plan|apply` supports target health checks, checkpoint-only
  apply by default, file-backed DNS and Caddy mutation, Cloudflare DNS patch or
  explicit creation, and receipt-backed rollback where previous state exists.
- Provider mutation requires an exact token, provider config,
  `--execute-provider-mutation`, and `allow_mutation: true`.
- The `move-app` workflow already orders readiness, placement, export, import,
  restore drill, and cutover-related nodes, but current node semantics stop
  short of a real move.
- The current workflow ends with traffic plan/apply rather than invoking
  `app.cutover.plan|apply`, and its `EXPORT_BUNDLE` input is a manual
  substitution rather than an automatically digest-bound upstream artifact.
- `opheliad` provides host-local journals, events, cancellation, leases,
  maintenance, drain, and disconnected completion.

### Remaining Completion Scope

1. Define one immutable cross-host cutover plan that binds source and target
   host identities, pack and export digests, observations, provider intent,
   rollback windows, and every host-local suboperation.
2. Require a target candidate imported and verified through Features 2 and 3.
3. Add source quiesce, final delta capture, target delta apply, target
   activation, and source unquiesce or retention as explicit phases.
4. Submit each local effect to that host's opheliad. The coordinator links
   receipts but never performs hidden SSH or Docker mutations.
5. Make Caddy and DNS provider changes transactional where the provider
   supports observation and restoration. Preserve explicit limitations when
   deletion rollback is unsafe.
6. Run target-origin checks before traffic, external checks after traffic, and
   source or target checks after compensation.
7. Define safe pause and recovery behavior for source disconnect, target
   disconnect, Lumen outage, provider timeout, and operator cancellation.
8. Retain source data and revision for the approved rollback window, then hand
   cleanup to a separate confirmation-bound operation.

For normal fleet operation, the durable coordinator lives in Lumen's Ophelia
DeployProvider because Lumen already owns supervisory Jobs, Decisions, and
cross-host progress. The coordinator state machine, canonical plan, phase
contracts, idempotency keys, and aggregate receipt remain Ophelia contracts.
For fixture work and separately authorized local recovery, `ship` may host the
same state machine in a local coordinator journal. That lane is identified as
`authorization_kind: local_break_glass`; it is not a second host executor and
still submits every local effect to the relevant opheliad.

### Non-Goals

- Universal zero-downtime movement for applications that cannot quiesce or
  replicate safely.
- Automatic migration of undeclared external services.
- Deleting a DNS record or provider object when prior state cannot be proven.
- Treating a traffic checkpoint as a completed cutover.
- Allowing the private UI or CI to run host commands directly.
- Combining post-cutover destructive cleanup into the initial approval.

### Contracts, CLI, API, And JSON Artifacts

Compatibility surfaces remain:

```bash
ship app cutover plan demo-service --from host_fixture_a --to host_fixture_b --json
ship app cutover apply demo-service --from host_fixture_a --to host_fixture_b --confirm <token> --json
ship app traffic plan demo-service --from host_fixture_a --to host_fixture_b --target-origin demo-target.example.com --json
ship app traffic rollback plan demo-service --receipt <receipt-id> --json
```

During compatibility, the plan declares `mode: checkpoint` or
`mode: orchestrated`. Orchestrated apply is refused until every advertised
host and provider capability is present. Existing checkpoint semantics do not
silently change.

Orchestrated mode also declares `coordinator: lumen` for normal fleet work or
`coordinator: local` for fixture and break-glass operation. The logical
coordinator API is `plan_cutover`, `start_cutover`, `get_cutover`,
`cancel_cutover`, and `resume_cutover`; its wire location belongs to the Lumen
DeployProvider or the local `ship` transport, not to a new public host port.

The coordinator artifact `ophelia.cutover.plan` contains:

- `cutover_id`, plan digest, app, environment, pack digest
- source and target host ids, observation cursors, revision ids
- base export and final delta requirements
- ordered phases, host-local operation requests, dependencies, deadlines
- target origin and external assertions
- DNS and Caddy provider intent plus observed prior state
- rollback window, compensation graph, cleanup exclusions
- exact authorization scope

Each host returns ordinary journaled operation events and receipts. The
terminal `ophelia.cutover.receipt` links their ids, the traffic receipt,
observed final state, effect, compensation, and any manual recovery action.
Lumen projects the same chain into one supervisory Job and related Incidents.

One settled Decision authorizes the immutable coordinator plan, not a blanket
host command. Immediately before each host phase, Lumen signs a short-lived
claim containing the coordinator plan digest, phase digest, host-specific
subplan digest, host audience, scope, actor, Decision id, expiry, nonce, and
phase idempotency key. The host verifies and journals the claim digest before
execution. An accepted operation may finish after claim expiry; an unaccepted
expired claim cannot be reused. Reissue requires unchanged Decision and plan
digests plus fresh required observations, otherwise a new plan and Decision.

### State And Data Model

Suggested coordinator states:

```text
planned
  -> awaiting_approval
  -> source_quiescing
  -> final_export
  -> target_import
  -> target_activating
  -> traffic_switching
  -> external_verifying
  -> committed
```

Any active state may transition to `pausing`, `compensating`,
`failed_compensated`, `failed_uncompensated`, or `recovery_required` according
to phase-specific rules. Host-local state remains authoritative for local
effects. The coordinator stores references, cursors, and digests rather than a
second copy of host truth.

Coordinator storage is transactional and durable. It enforces one fenced owner
per cutover, unique `(cutover_id, phase, host_id, attempt)` idempotency, monotonic
phase transitions, host event cursors, approval and subclaim digests, and the
aggregate receipt link. Coordinator failover acquires a higher fence, replays
host evidence, and resumes only from a state whose preconditions are still
fresh.

### Safety And Redaction Invariants

- One approval binds the complete coordinator plan and produces scoped,
  short-lived host claims. A host accepts only its exact subplan.
- Re-observation invalidates approval when source revision, target candidate,
  data digest, provider state, route ownership, or health evidence changed.
- Source freeze has a maximum duration and a tested unfreeze compensation.
- No traffic moves until target import, activation, and target-origin checks
  succeed.
- Provider credentials are resolved only inside the scoped provider adapter.
- Plans and receipts record record ids, zones, names, TTL, and prior or next
  metadata, never tokens.
- Cleanup and source deletion are excluded from the cutover approval.
- Every phase has a deadline, cancellation boundary, and declared compensation.

### Failure And Recovery Semantics

Before traffic changes, failure stops the target candidate as appropriate,
unfreezes the source, and leaves source traffic authoritative. After traffic
changes, failed external verification first attempts provider rollback and
verifies the restored source path. A provider response alone is not enough;
the orchestrator observes route and application behavior.

If the coordinator loses Lumen, already accepted host operations continue and
replay events. No new phase begins while its coordinator lease, subclaim, or
required observation cannot be proven. On reconnect, the coordinator consumes
terminal host receipts idempotently and either advances or compensates. If
hosts disagree about committed state, the workflow enters `recovery_required`
and never guesses. `failed_uncompensated` creates a critical incident with the
last observed source, target, and traffic states.

### Dependencies

Feature 2 supplies verified target import and final data movement. Feature 3
supplies recovery proof. Feature 5 supplies safe concurrent source, candidate,
and drill isolation. Feature 6 supplies enrolled host capability and durable
local execution. Feature 8 projects the workflow but does not own its logic.

### Phased Implementation Slices

1. Formalize checkpoint truthfulness, phase contracts, and aggregate plan only.
2. Orchestrate fixture host-local operations with no traffic provider mutation.
3. Add source quiesce, final delta, target activation, and compensation.
4. Integrate file DNS and Caddy providers with observed rollback.
5. Add Cloudflare within its existing patch and create safety limits.
6. Prove disconnect, restart, and recovery on two disposable hosts.

### Test And Acceptance Criteria

- Planning changes only staging and plan evidence on both hosts.
- A two-host synthetic stateful app moves with matching data, release, route,
  and external behavior evidence.
- Failure is injected at every phase, with old or new verified service retained
  according to the approved state machine.
- Freeze timeout always attempts unfreeze and records the result.
- Changed source data, candidate revision, route ownership, or provider prior
  state invalidates the approval.
- Replayed events, duplicate requests, coordinator restart, host disconnect,
  and Lumen disconnect do not duplicate effects.
- Coordinator lease failover and phase replay preserve monotonic state and one
  host-local effect per phase idempotency key.
- Expired, wrong-host, wrong-phase, replayed, or stale-observation subclaims are
  rejected; accepted work can finish while disconnected.
- Traffic rollback refuses deletion-only cases and never claims restoration it
  did not observe.
- Source cleanup cannot occur under a cutover token.

### Definition Of Done

Feature 4 is complete when a two-disposable-host fixture move can be planned
without live mutation, approved exactly, executed through both opheliad
authorities, externally verified, compensated at every injected failure point,
and reconstructed from linked receipts without consulting private logs.

## 5. Per-App Isolation Refactor

### Product Outcome

Each app receives only its declared network paths, data credentials, volumes,
secrets, devices, runtime privileges, and resources. A compromised workload
does not receive ambient connectivity or shared credentials merely because it
runs on the same host.

### Current Foundation

- Manifest v1 supports `networking.internal: per-app`; omitted manifests retain
  the shared `ophelia-internal` compatibility network.
- `ship app isolation plan` reports current and target topology without
  mutation. Apply occurs through the ordinary reviewed deploy flow. The current
  plan does not inspect Docker or other observed runtime state, and its reported
  target omits some addon attachments.
- The v1 renderer currently joins every service to edge, even when that service
  has no route. A per-app service using a shared addon also joins the shared
  internal network.
- Manifest v2 gives each app and environment a stable app network, uses
  revision-specific projects, and models app, edge, and data membership. The
  intended contract limits edge to routed web workloads and data access to
  explicit membership, but the current renderer still joins every web workload
  to edge and every container workload to the app network. Model, renderer,
  preflight, and observation therefore need one enforced interpretation.
- V2 also enforces resource limits, non-root assertions, read-only root by
  default, no-new-privileges, capability drops, and security-profile choices.
- Stable data aliases require recreate strategy, preventing overlapping
  ownership.
- V1 and v2 use different private-network names and lifecycles, with no
  journaled adoption contract between them. V2's declared network list also
  needs one consistent interpretation across model, renderer, preflight, and
  observation.
- The current architecture identifies shared Redis credentials and broad edge
  membership as known gaps.

### Remaining Completion Scope

1. Define a canonical `IsolationProfile` and observed isolation report for both
   v1 compatibility and v2 revisions.
2. Move retained v1 applications off shared internal networking through
   explicit, plan-visible migrations.
3. Ensure only routed workloads join edge, only declared clients join data,
   and app-private networks are unique, labeled, observed, and lifecycle-safe.
4. Replace shared Redis password and logical-index isolation with per-app ACL
   users and key prefixes or isolated instances.
5. Enforce per-app PostgreSQL database and role boundaries, connection refs,
   grants, and backup ownership even when the server is shared.
6. Bind volumes, file mounts, devices, secrets, security profiles, and resource
   limits to exact workloads. Block ambient mounts and unapproved privilege.
7. Reduce Caddy filesystem visibility to the edge configuration and material it
   actually needs. It must not gain read access to workload secret material
   merely because both live beneath the runtime root.
8. Add isolation observations and drift findings to opheliad, readiness,
   receipts, and UI projections.
9. Provide a safe compatibility exit for apps that require a temporary shared
   dependency, with explicit risk, owner, and expiry.

### Non-Goals

- Claiming hostile public multi-tenancy or a VM-strength boundary.
- Replacing Docker or Compose with a cluster scheduler.
- Removing the shared Caddy edge or shared data network when explicit use is
  appropriate.
- Automatically changing an application's network or data credentials without
  a reviewed deploy plan.
- Allowing network isolation to substitute for secret or runtime hardening.

### Contracts, CLI, API, And JSON Artifacts

The current plan remains:

```bash
ship app isolation plan demo-service --environment staging --json
```

Completion extends it with observed comparisons and an optional profile:

```bash
ship app isolation plan demo-service \
  --environment staging \
  --profile default \
  --json
```

There is no standalone isolation apply. V1 changes flow through
`ship deploy --plan|--apply`; v2 changes flow through
`ship manifest plan|apply`. This keeps network, secret, data, Caddy, and
revision changes inside one activation and compensation boundary.

The plan remains `ophelia.plan` with `operation: app.isolation.plan` and adds an
`isolation` block containing desired and observed networks, edge membership,
data principals, volume ownership, secret targets, runtime security, resource
limits, exceptions, and drift. Terminal deployment receipts reference
`ophelia.isolation.observation` and its digest.

### State And Data Model

| Entity | Important fields |
| --- | --- |
| `IsolationProfile` | version, network defaults, security defaults, resource policy |
| `IsolationBinding` | app, environment, workload, resource type, resource id, access |
| `IsolationException` | reason, owner, expiry, approved plan, observed use |
| `IsolationObservation` | revision, host, collected time, bindings, drift, digest |

Desired bindings come from the manifest and pack lock. Observations come from
Docker, managed data adapters, secret-binding metadata, and runtime files.
`state.db` indexes the comparison; opheliad receipts and observations remain
the evidence source.

### Safety And Redaction Invariants

- Network, volume, database, role, Redis principal, and secret target names are
  generated from canonical app, environment, and workload identities.
- Migration creates and verifies the target boundary before disconnecting the
  active workload from its previous dependency.
- Secret rotation uses opaque refs and never reports credential values.
- Only routed web workloads receive edge access.
- Data-network access is denied unless a workload declares it.
- An existing same-name Docker network is accepted only after ownership,
  driver, internal, attachable, and label observations match the approved
  managed resource.
- Privileged mode, added capabilities, device access, writable root, or host
  mounts require explicit typed capability and policy approval.
- Shared foundation services are protected managed resources and are never
  deleted by app cleanup.

### Failure And Recovery Semantics

Isolation migration is part of a revision activation. Failure before traffic
switch preserves the old revision and bindings. Failure after switch invokes
normal revision and traffic compensation. Network and data principals are
retained until no active or rollback-eligible revision references them.

A partial credential rotation never revokes the old credential until the new
revision proves connectivity. If rollback would require an expired exception or
revoked credential, planning blocks and requires an explicit recovery plan.

### Dependencies

This feature depends on the journaled revision kernel, strict manifest parsing,
runtime observation, and managed-resource handles. Feature 1 declares intended
bindings. Features 2 and 3 require isolated candidates. Feature 4 requires
safe source and target coexistence. Feature 6 reconciles host-wide foundations
without collapsing app boundaries.

### Phased Implementation Slices

1. Add desired-versus-observed isolation reports for fixtures.
2. Finish edge, app, and data network enforcement for v1 and v2.
3. Add PostgreSQL and Redis principal adapters plus secret-ref rotation.
4. Enforce volume, mount, device, security, and resource bindings.
5. Add compatibility exception expiry and drift remediation.
6. Prove migration and rollback with live Docker fixtures.

### Test And Acceptance Criteria

- Network inspection shows unrelated apps cannot connect over app-private
  networks and non-routed workloads do not join edge.
- PostgreSQL and Redis fixture credentials cannot access another app's data.
- Caddy cannot read revision-private workload secret material.
- Secret, volume, device, privilege, resource, and security-profile drift is
  detected and blocks where policy requires.
- V1 shared compatibility continues until explicit opt-in; v2 defaults remain
  strict.
- Failed migration leaves the previously verified revision usable and does not
  delete shared foundations.
- Concurrent revisions, restore drills, and import candidates use distinct
  managed resources.
- Seeded secrets are absent from plans, observations, receipts, and UI output.

### Definition Of Done

Feature 5 is complete when fixture and live-Docker inspection prove that every
app receives only declared network, data, secret, storage, device, privilege,
and resource access; compatibility exceptions are explicit and expiring; and
failed migrations deterministically preserve or restore a verified revision.

## 6. Host Bootstrap And Reconcile

### Product Outcome

A clean supported Linux host can be inspected, bootstrapped from an immutable
source, enrolled when separately authorized, restarted, upgraded, recovered,
and continuously reconciled to a declared Ophelia host profile. Repeated apply
is idempotent, drift is observable, and repair never hides destructive effects.

### Current Foundation

- `ship daemon install` plans and confirmation-gates an immutable versioned
  install under `/opt/ophelia/releases`, a stable launcher, strict config, a
  dedicated identity, systemd enablement, and a promoted-release health check.
  Staged self-upgrade has launcher rollback; initial-install failure still
  needs the stronger deterministic recovery semantics specified below.
- `opheliad` is the durable single-host authority with a Unix-socket API,
  journal, restart recovery, journaled manifest v2 revision execution, tasks,
  cron, observations, drain, maintenance, and host event cursors.
- Strict daemon config rejects unknown fields, unsafe ownership and modes,
  invalid paths, and invalid limits.
- Host inventory, readiness, placement, capabilities, observations, encrypted
  host-control backup, clean-host recovery, staged upgrade, enrollment, and
  outbound agent foundations are present.
- Current host inventory and placement are read-only. Full Docker, firewall,
  NTP, updates, disk, log, and shared edge foundation convergence is not yet a
  complete product workflow.
- The current daemon reconciliation thread heartbeats host state, verifies the
  operation journal periodically, and signals the systemd watchdog. It does not
  yet compare or repair desired Docker, Caddy, network, systemd, or active
  revision state.
- The legacy `ship bootstrap-host` path assumes SSH trust, a checkout, Python,
  and Docker already exist. It has no JSON plan, confirmation token, or receipt.
  Packaged edge bootstrap is a separate idempotent seam and is not yet governed
  by the host journal.

### Remaining Completion Scope

1. Define a versioned `HostProfile` covering supported OS, architecture,
   packages, Docker and Compose, system identity, directories, filesystem
   permissions, firewall posture, SSH assumptions, time sync, security updates,
   disk and log policy, edge runtime, backup destinations, and daemon config.
2. Add zero-mutation bootstrap inspection and a deterministic plan with source
   provenance for every desired value and observation, including a pinned SSH
   host fingerprint when bootstrap crosses that trust boundary.
3. Apply through typed, idempotent steps with per-step preconditions,
   postconditions, deadlines, rollback or repair semantics, and receipts.
4. Keep enrollment separately confirmation-bound. A bootstrap plan may prepare
   identity paths but may not consume a Lumen token implicitly.
5. Extend reconciliation from journal and runtime health to host-profile drift,
   with report-only default and narrowly scoped auto-repair classes.
6. Protect operator-managed files and unrelated services. Adopt only resources
   carrying Ophelia ownership evidence or an explicit adoption plan.
7. Add drain-aware upgrades, edge and data foundation health, disk pressure
   remediation, recovery verification, and post-reboot convergence.
8. Publish host capability and profile observations to Lumen through the
   existing outbound protocol once the matching server authority exists.
9. Add fail-closed authoritative-journal recovery: preserve a corrupt
   `operations.db`, stop mutation, reconstruct candidate truth from immutable
   revisions, receipts, exported events, active markers, Docker labels, and
   Caddy targets, then require an explicit reconciliation plan whenever those
   sources disagree.

### Non-Goals

- Becoming a general-purpose configuration-management system.
- Managing arbitrary operator packages, users, firewalls, or SSH keys.
- Opening a public inbound Ophelia API.
- Hiding reboots, firewall changes, package upgrades, or destructive repair
  inside automatic reconciliation.
- Restoring host identity from the host-control backup.
- Enrolling or mutating a production host under this implementation spec.

### Contracts, CLI, API, And JSON Artifacts

Existing foundation commands remain:

```bash
ship daemon install --source-root /srv/ophelia --json
ship daemon status --json
ship daemon capabilities --json
ship daemon events --cursor 0 --json
ship host inventory --json
ship host readiness host_fixture_a --json
```

Existing machine artifacts include `ophelia.daemon-install-plan`,
`ophelia.daemon-install-receipt`, `ophelia.enrollment-plan`,
`ophelia.enrollment-receipt`, `ophelia.host-backup-plan`,
`ophelia.host-backup-receipt`, `ophelia.clean-host-restore-plan`,
`ophelia.clean-host-restore-receipt`, `ophelia.host_inventory`,
`ophelia.host_readiness`, `ophelia.capabilities`, and
`ophelia.host-observation`. The completion work composes these seams without
renaming them into semantics they do not yet have.

Completion adds explicit host-profile commands:

```bash
sudo ship host bootstrap local plan --profile config/host-profile.fixture.yml --json
sudo ship host bootstrap local apply <plan-id> --confirm <token> --json
ship host bootstrap remote plan bootstrap@host-a.example.com \
  --ssh-port 22 \
  --ssh-host-key-sha256 <reviewed-fingerprint> \
  --profile config/host-profile.fixture.yml \
  --json
ship host bootstrap remote apply <plan-id> --confirm <token> --json
ship host reconcile plan --profile config/host-profile.fixture.yml --json
ship host reconcile apply --plan-id <plan-id> --confirm <token> --json
ship host drift --profile config/host-profile.fixture.yml --json
```

Local mode runs on the target host and can use the authoritative journal as
soon as opheliad is installed. Remote mode is a controller-side SSH bootstrap
used only before the daemon exists. It requires a previously reviewed host-key
fingerprint, never trust-on-first-use during apply. Plan performs read-only OS,
dependency, port, firewall, disk, and artifact observations over that pinned
connection and binds them into the plan digest.

Remote apply transfers only digest-verified artifacts into a plan-specific
staging root. Until opheliad is healthy, the trusted `ship` controller owns the
bootstrap operation and both sides retain a minimal append-only bootstrap
journal. The remote journal lives under a mode `0700` managed bootstrap root
and contains no credentials. Once the Unix socket and journal pass health,
opheliad records a `host.bootstrap.handoff` genesis event that binds the
bootstrap plan, pre-daemon journal digest, install receipt, and controller
identity. From that point, opheliad is the sole host mutation authority.
Partial pre-daemon failure is retried or compensated from the same bootstrap id;
it never falls through to an unjournaled shell script.

The host may expose equivalent authenticated `/v1/plans` and `/v1/operations`
operation classes plus read-only profile and drift resources. The capability
map advertises profile version, supported step types, operating systems,
reboot requirement handling, and auto-repair classes.

`ophelia.host.profile`, `ophelia.host.bootstrap.plan`,
`ophelia.host.reconcile.plan`, and `ophelia.host.observation` artifacts carry
canonical desired state, bounded observations, ownership, drift, and source
provenance. Terminal effects remain ordinary `ophelia.receipt` records linked
to the authoritative operation journal.

### State And Data Model

| Entity | Important fields |
| --- | --- |
| `HostProfile` | version, supported OS, desired foundations, policy, digest |
| `HostResource` | typed id, desired state, ownership, sensitivity, repair class |
| `HostObservation` | host id, profile digest, cursor, collected time, bounded state |
| `HostDrift` | resource id, desired, observed, severity, safe remediation |
| `BootstrapOperation` | plan, ordered steps, reboot boundary, terminal receipt |
| `ReconcileLease` | resource scope, owner, expiry, fencing token |
| `HostRecoveryState` | journal integrity, preserved corruption artifact, candidate evidence, recovery-required reason |

The host identity directory remains independent from the recoverable runtime
tree. `operations.db` remains authoritative for accepted work. `state.db` is a
rebuildable projection. Profile source and observations never contain private
keys or secret values.

### Safety And Redaction Invariants

- Planning makes no package, service, firewall, user, filesystem, Docker,
  Caddy, network, or enrollment change.
- Apply operates only on typed resources and validates ownership and parent
  safety before mutation.
- Package and source inputs are immutable and digest-verified.
- Configuration and identity files use strict ownership and mode; effective
  config output is redacted with provenance.
- Firewall changes prove the operator recovery path before commit and never
  expose a public opheliad port.
- Reboots, destructive storage repair, credential rotation, enrollment, and
  removal require separate plan visibility and approval.
- Reconciliation never adopts or deletes an unowned service by name alone.
- Ambiguous disagreement among journal, active markers, Docker labels, and
  Caddy targets stops mutation and emits an explicit reconciliation plan.
- Host events and observations retain error type and bounded diagnostics, not
  raw secret-bearing process output.

### Failure And Recovery Semantics

Bootstrap steps are journaled before side effects. Each step is idempotent and
has an observed postcondition. On failure, the engine either restores the last
verified configuration, leaves the previous daemon release active, or enters
`recovery_required` with exact repair instructions. It never marks a host ready
because files were merely written.

Reconciliation uses leases and fencing. Loss of a lease stops further commits.
Repeated drift does not cause an unsafe repair loop. Report-only drift remains
visible until approved; safe auto-repair is rate-limited and receipt-backed.
After reboot, opheliad recovers journal state, verifies foundations, and emits a
new observation before accepting normal workload changes.

If the authoritative journal fails integrity, Ophelia preserves the damaged
database as a read-only, digest-addressed recovery artifact and refuses every
mutation. Recovery builds a candidate from immutable revision locks, signed or
anchored receipts, event exports, active markers, Docker labels, and Caddy
targets. One internally consistent candidate can be proposed through a
confirmation-bound reconcile plan. Missing or conflicting evidence stores the
host lifecycle as `recovery_required`; Ophelia never silently creates a new
empty authority or guesses the active revision.

### Dependencies

This feature depends on the existing journaled kernel, secure filesystem
primitives, daemon installer, systemd service, observations, and encrypted
host-control recovery. Features 2 through 5 require its managed roots and host
capabilities. Feature 8 consumes its observations and events. The Lumen server
authority is a later integration dependency, not a prerequisite for local host
correctness.

### Phased Implementation Slices

1. Characterize daemon install, recovery, upgrade, inventory, and readiness.
2. Add the strict host profile and read-only inspection or drift plan.
3. Implement a minimal fixture bootstrap for directories, daemon, Docker
   validation, and edge readiness on a disposable host.
4. Add typed package, firewall, time, updates, disk, and log steps one at a time.
5. Add report-only continuous reconciliation, then narrowly scoped auto-repair.
6. Prove reboot, failed upgrade, disk pressure, corrupted config, and clean-host
   recovery lanes.

### Test And Acceptance Criteria

- Two consecutive applies to an already-correct disposable host produce no
  extra mutation.
- Planning leaves protected system and runtime hashes unchanged except for
  allowed staging and plan journal writes.
- Unsupported OS, wrong architecture, unsafe config ownership, missing recovery
  path, untrusted source digest, and unknown profile keys fail closed.
- Kill after every bootstrap and reconcile journal boundary, then recover to a
  deterministic host state.
- Failed daemon promotion restores the previous healthy release.
- Wrong SSH fingerprint, changed host observations, or expired approval fails
  before remote mutation.
- Reboot restores opheliad, active workloads, journal recovery, scheduler
  fencing, and observations.
- Drift against an unowned service is reported but never auto-adopted or
  deleted.
- Encrypted host-control recovery succeeds on a clean fixture host and does not
  restore host identity.
- A corrupt authoritative journal is preserved, all mutation stops, candidate
  truth is reconstructed from independent evidence, and disagreement persists
  as `recovery_required` until an explicit plan resolves it.
- Remote bootstrap rejects trust-on-first-use apply, transfers only plan-bound
  artifacts, records pre-daemon progress durably, and proves the authority
  handoff into opheliad.

### Definition Of Done

Feature 6 is complete when a disposable clean host can be inspected,
bootstrapped idempotently, restarted, drifted, reconciled, upgraded, and
recovered from public-safe fixtures, with every effect observed and
receipt-backed and no dependence on Lumen availability for local correctness.
Remote trust and pre-daemon authority handoff must be explicit, and corrupt
authoritative state must stop mutation and enter evidence-backed recovery
rather than being discarded or guessed.

## 8. Private Operator UI Adapter Surface

### Product Outcome

A private operator UI and its agents can render stable, bounded, redacted views
of apps, hosts, plans, approvals, operations, events, revisions, backups,
restore drills, cutovers, drift, and incidents without scraping CLI text or
reimplementing platform logic. Mutations still flow through Ophelia plans and
Lumen Decisions.

### Current Foundation

- `ship lumen capabilities`, `action-descriptors`, `dashboard-data`, and
  `console-data` expose read-only `ophelia.lumen.*` JSON contracts.
- The compatibility local API exposes `/lumen/capabilities`,
  `/lumen/action-descriptors`, `/lumen/dashboard-data`, `/lumen/console-data`,
  `/lumen/apps`, app readiness, and app timelines.
- Console data includes navigation, overview cards, bounded app rows, approval
  metadata, workflows, plugins, quick actions, source summaries, and an
  explicit no-execution safety block.
- The adapter reuses command catalog, readiness, receipts, conflicts, state,
  observability, traffic, host inventory, placement, workflows, and plugins.
  It does not own duplicate business logic.
- Per-app subreport failures degrade to structured warnings instead of crashing
  the whole dashboard, and a final deep-redaction sweep protects output.
- `opheliad` provides the authoritative versioned host API and replayable event
  cursor. The matching Lumen certificate authority, exchange, queue,
  acknowledgement projection, and DeployProvider are the current integration
  boundary.

### Remaining Completion Scope

1. Freeze a compatibility policy for existing `ophelia.lumen.*` v1 payloads,
   including additive fields, deprecation, pagination, and freshness.
2. Add opheliad-backed host, app, revision, operation, event, workload-run,
   backup, restore-drill, cutover, and drift projections.
3. Carry source kind, source cursor, observed time, freshness, partial-data
   warnings, and capability compatibility on every operator card.
4. Add bounded event pagination and replay cursors. High-volume logs use a
   separate scoped stream and never become ordinary dashboard fields.
5. Implement Lumen's Ophelia DeployProvider, signed Decision flow, command
   queue, acknowledgement, and projections into Jobs, Runs, receipts, Activity,
   Artifacts, and Incidents.
6. Expose action descriptors and plan previews to the UI, but keep approval and
   apply on the provider path. Console-data endpoints continue to reject tokens
   and execution requests.
7. Normalize status vocabulary so the UI distinguishes blocked, stale,
   disconnected, executing, compensating, failed compensated, failed
   uncompensated, recovery required, and succeeded with observed effect.
8. Keep the public repository limited to adapter contracts, fixtures, and
   public-safe examples. Private UI source and private deployment data remain
   outside this repository.

### Non-Goals

- Publishing or embedding the private UI in Ophelia.
- Letting the UI call Docker, Caddy, SSH, providers, or app hooks directly.
- Accepting a confirmation token on a read-only console endpoint.
- Duplicating readiness, placement, policy, workflow, or receipt logic in an
  adapter.
- Returning unbounded logs, raw findings, provider responses, or secret values.
- Inferring success from a CI label, missing event, or optimistic local cache.

### Contracts, CLI, API, And JSON Artifacts

Existing compatibility commands and kinds remain:

| Command | JSON kind |
| --- | --- |
| `ship lumen capabilities --json` | `ophelia.lumen.capabilities` |
| `ship lumen action-descriptors --json` | `ophelia.lumen.action_descriptors` |
| `ship lumen dashboard-data --json` | `ophelia.lumen.dashboard` |
| `ship lumen console-data --json` | `ophelia.lumen.console` |

The adapter consumes, rather than replaces, opheliad resources such as
`/v1/capabilities`, `/v1/hosts/self`, `/v1/apps`, `/v1/operations`,
`/v1/events`, `/v1/workload-runs`, and `/v1/observations/latest`.

Completed UI records share a bounded projection header:

```json
{
  "schema_version": 1,
  "source_kind": "ophelia.host.events",
  "source_id": "host_fixture_a",
  "source_cursor": 42,
  "observed_at": "2026-08-30T12:00:00Z",
  "freshness": "fresh",
  "partial": false,
  "values_redacted": true
}
```

Action descriptors identify plan and apply operation types, risk, required
Decision scope, capability prerequisites, and artifact kinds. They never carry
reusable approval credentials. UI plan previews reference immutable plan and
diff artifacts. Lumen records the Decision and sends the host a short-lived
signed claim only after settlement.

### State And Data Model

| Projection | Stable identity and content |
| --- | --- |
| Host card | host id, capability version, health, drain, maintenance, pressure, freshness |
| App card | app and environment, desired and active revision, readiness, drift, backup posture |
| Operation card | operation id, plan digest, state, current phase, effect, event cursor |
| Approval item | plan id and digest, risk, Decision state, expiry, no nonce |
| Recovery card | export, drill, cutover, compensation, cleanup, RPO and RTO summaries |
| Incident candidate | uncompensated or stale critical state plus evidence references |

Lumen owns user-facing persistence and relationships. Host journal and
immutable Ophelia evidence remain authoritative for runtime truth. Projection
deduplication keys are host id plus event id or cursor, provider operation id,
and receipt id as appropriate.

### Safety And Redaction Invariants

- Every aggregate ends with centralized deep redaction, but upstream contracts
  must also avoid collecting secret material.
- Cards contain counts, codes, statuses, digests, and bounded summaries, not raw
  env values, database URLs, tokens, keys, provider payloads, or full logs.
- Actor identity comes from authenticated Lumen and host boundaries, never UI
  form fields.
- Read-only endpoints accept no confirmation token and trigger no plugin,
  workflow, provider, probe, state refresh, or command execution.
- External links and artifact paths are allowlisted, scoped, and do not expose
  local private filesystem structure unnecessarily.
- Cache state always shows freshness and source connection posture.
- Unknown status or schema values render as unsupported, not success.

### Failure And Recovery Semantics

One failed app subreport produces a warning and a partial app card; it does not
remove sibling apps or crash the aggregate. A disconnected host retains the
last observation with `freshness: stale` and never appears healthy by omission.

Event delivery is at least once. Lumen deduplicates and advances cursors only
after durable projection. Host backpressure does not stop runtime
reconciliation. After reconnect, events and terminal results replay until
acknowledged. A projection conflict preserves both evidence references and
opens an incident instead of choosing optimistic state.

### Dependencies

Feature 6 supplies host authority, events, observations, and capability
negotiation. The read-only adapter can evolve in parallel with Features 1
through 5, but final recovery and movement views depend on their stable
artifacts. Lumen server authority and DeployProvider implementation are the
external integration dependency.

### Phased Implementation Slices

1. Snapshot-test current compatibility payloads and publish field stability
   rules.
2. Add freshness, partial-data, pagination, and opheliad host projections.
3. Add operation, revision, backup, drill, and cutover cards from fixture
   evidence.
4. Implement Lumen enrollment and exchange projection with cursor replay.
5. Implement plan preview, settled Decision, provider apply, and incident flow.
6. Run fixture, disconnect, replay, redaction, and private-UI contract tests.

### Test And Acceptance Criteria

- Existing fixture console expectations remain stable: bounded apps, blocked
  and warning counts, plugin inventory, approval metadata, and no fixture
  secret values.
- Golden JSON and JSON Schema tests cover every payload and additive migration.
- One corrupt app, receipt, plugin, or state record yields a localized warning.
- Stale and disconnected host data can never render as fresh or successful.
- Duplicate and replayed events produce one projection and monotonic cursor
  advancement.
- A UI mutation without a settled Decision, correct scope, exact plan digest,
  or supported host capability is rejected before host acceptance.
- Seeded tokens, database URLs, private keys, env values, raw approval nonces,
  and application log canaries never appear in adapter or Lumen projections.
- UI status agrees with terminal host receipt effect and observed state.

### Definition Of Done

Feature 8 is complete when a private UI can render the full fixture and
disposable-host lifecycle from versioned, bounded, redacted contracts; submit a
plan-bound action through a settled Lumen Decision; survive disconnect and
replay; and show the exact host-observed outcome without direct infrastructure
access or CLI text parsing.

## Dependency-Ordered Delivery Plan

The sequence below is dependency order, not a calendar estimate. Read-only UI
contract work may run in parallel, but completion gates remain ordered.

| Order | Delivery slice | Features | Exit gate |
| ---: | --- | --- | --- |
| 0 | Characterization and contract freeze | 1, 2, 3, 4, 5, 6, 8 | Current CLI JSON, fixture behavior, effect truthfulness, and v1/v2 boundaries have golden tests. |
| 1 | Canonical portability domain | 1 | Supported source manifests compile into a deterministic redaction-safe pack lock. |
| 2 | Isolation and host foundations | 5, 6 | Disposable hosts expose managed roots, strict app boundaries, journal recovery, and read-only drift. |
| 3 | Journaled export and candidate import | 2 | A stateful fixture moves into an isolated clean-host candidate with no activation. |
| 4 | Full restore proof | 3 | The candidate runs in quarantine, passes data and app assertions, records RPO and RTO, and cleans up. |
| 5 | End-to-end cutover | 4 | A two-host fixture move commits or compensates deterministically at every phase. |
| 6 | Operator and Lumen completion | 8 | Private UI projections, settled Decisions, provider operations, replay, and incidents agree with host evidence. |
| 7 | Disposable-host release gate | All | Security, recovery, fault injection, compatibility, docs, and public audit pass without production mutation. |

### Cross-Feature Release Gates

No feature advances beyond fixtures until these gates pass:

1. Plan purity hashes prove all protected live surfaces unchanged.
2. Approval tests prove exact content, actor, host, scope, expiry, and replay
   binding.
3. Fault injection at every journal boundary yields a deterministic recovered
   state.
4. Redaction tests seed registered secret canaries across manifests, runtime
   env, provider responses, hook output, logs, bundles, events, and receipts.
5. Compatibility tests cover manifest v1, manifest v2, bundle v1, bundle v2,
   API versions, operation schema migrations, and command aliases.
6. Live Docker tests inspect actual networks, containers, volumes, Caddy state,
   PostgreSQL grants, Redis access, and cleanup.
7. Disposable-host tests cover bootstrap, reboot, daemon termination, disk
   pressure, export, import, drill, cutover, rollback, upgrade, and recovery.
8. Documentation checks and strict open-source audit pass with synthetic data
   and `example.com` endpoints only.

## Program-Level Definition Of Done

The selected major-feature program is complete only when all of the following
are true:

- One canonical pack lock drives export, import, restore drill, cutover,
  isolation, host fit, and UI summaries for supported v1 and v2 sources.
- An encrypted, digest-verified export restores onto a clean disposable host
  without the original host or any secret value in the artifact metadata.
- A restore drill proves declared data and application behavior in isolation,
  measures RPO and RTO, and cleans up deterministically.
- A two-host cutover can commit or compensate at every injected failure point,
  while each opheliad remains the authority for its local effects.
- Runtime inspection proves declared per-app boundaries and reports every
  compatibility exception.
- Host bootstrap and reconcile are idempotent, reboot-safe, observable, and
  recoverable from public-safe fixtures.
- The private adapter and Lumen projection show exact plans, Decisions, events,
  effects, receipts, stale state, and incidents without direct runtime access.
- No plan, receipt, event, diagnostic, bundle metadata, adapter payload, or
  fixture intentionally contains secret material or private deployment data.
- No production mutation was needed to prove any acceptance criterion in this
  document.
