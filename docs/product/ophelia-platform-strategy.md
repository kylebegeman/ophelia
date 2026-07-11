# Ophelia Platform Strategy And Technical Architecture

Status: proposal for discussion

Date: 2026-07-11

Audience: product owner, Ophelia implementers, Lumen architects, operators, and
implementation agents

<div class="artifact-actions">
  <a href="ophelia-platform-strategy.md">View Markdown source</a>
  <a href="ophelia-platform-strategy.md" download>Export Markdown source</a>
</div>

## Executive Conclusion

Ophelia is already the nucleus of a real platform. It is more than a deployment
script collection: it has the contract, planning, evidence, safety, and
machine-readable surfaces of a small platform as a service. Its strongest layer
is the declared control plane. Its weakest layer is execution truthfulness at
the live runtime boundary.

The recommended product direction is:

> Ophelia becomes a sovereign, agent-native personal cloud for a trusted
> operator managing many VPS-hosted applications. A durable Ophelia agent on
> each host owns runtime reconciliation, while Lumen provides the human and AI
> control plane across the fleet.

The near-term goal is not Kubernetes parity, public multi-tenancy, or another
application framework. It is a reliable and ergonomic single-operator platform
for hosting static sites, APIs, SaaS products, background services, databases,
and AI workers across independent VPSs.

The defining semantic rule should be:

> An Ophelia operation succeeds only when observed and verified runtime state
> matches the approved desired state.

This rule has important consequences. A copied bundle is not a deployment. A
changed pointer is not a rollback. A completed archive is not a recoverable
backup. A recorded checkpoint is not a cutover. A policy report is not a safety
gate unless every mutation path enforces it.

Ophelia should first earn trust on one host, then make that trust repeatable
across a fleet, then expose the complete experience through Lumen. Breadth such
as source builds, preview environments, templates, and AI workload profiles
should follow the trusted execution kernel rather than precede it.

## Document Status And Decision Boundary

This document consolidates the repository scan, executable checks, security and
reliability audit, Lumen integration review, competitive research, target
architecture, and implementation roadmap performed against Ophelia 0.4.4 at
commit `e576d7d`.

It is a proposal for discussion. It does not authorize production mutation,
host migration, data movement, DNS changes, or deletion. The active
[Roadmap](../ROADMAP.md) remains the current delivery record until the product
direction in this document is accepted and converted into tracked work.

The document uses these labels consistently:

| Label | Meaning |
| --- | --- |
| Current | Verified in source, tests, or executable local behavior. |
| Partial | Performs part of the named operation but lacks important runtime semantics. |
| Preview-only | Produces plans, rehearsals, evidence, or checkpoints without performing the live operation. |
| Metadata-only | Describes capabilities or desired state without executing them. |
| Proposed | Recommended implementation that does not exist yet. |
| Optional later | A useful extension after prerequisite invariants are proven. |

### Initial Trust Model

The initial product is a trusted single-operator system, but it must treat these
inputs as potentially compromised:

- application repositories and their manifests
- CI identities and generated artifacts
- AI-generated plans or operation requests
- imported app packs and backup archives
- application containers
- network responses used by verification and observability
- provider responses and webhooks

This distinction matters. There may be only one human operator, but a compromised
repository, dependency, container, CI token, or AI-generated manifest must not
automatically become arbitrary host access.

### Near-Term Non-Goals

- Public customer tenancy and billing.
- An Ophelia-specific container scheduler.
- Kubernetes API compatibility.
- A general-purpose application framework embedded in the runtime core.
- Automatic cross-host rescheduling before independent hosts are reliable.
- Replacing Lumen's AI task scheduler with another scheduler inside Ophelia.
- Hiding risky host operations behind opaque convenience commands.

## Audit Baseline

### Repository Inventory

| Measure | Verified value |
| --- | ---: |
| Tracked files | 419 |
| Python lines, including tests | 59,515 |
| Python lines under `src/ophelia` | 44,070 |
| Python source files under `src/ophelia` | 111 |
| Python test files | 62 |
| Documentation files | 120 |
| Cataloged commands | 111 |
| Typed action descriptors | 39 |
| Largest source module | `portability.py`, 7,435 lines |

The repository is substantial enough that future work should be treated as
platform engineering rather than script maintenance.

### Verification Performed

The scan verified:

- `./cli/ship self-test --json`
- 507 unit tests
- Python compilation
- documentation link validation
- example, platform manifest, fixture, adoption, and plugin validation
- fixture live-drill profiles
- production-hardening fixture behavior
- adversarial in-memory and temporary-directory probes for manifest paths,
  Caddy rendering, remote planning order, rollback behavior, and policy
  enforcement

The Git worktree was clean before this strategy document was created.

### Verification Limitations

The audit did not have a live Docker daemon or disposable VPS environment.
Therefore it did not perform:

- a real Docker, Compose, and Caddy deployment
- a live SSH deployment
- a real Cloudflare or GitHub provider mutation
- a destructive PostgreSQL or Docker-volume restore
- crash injection during live activation
- a power-loss recovery test
- a concurrent multi-host rollout

The repository currently has no configured formatting gate, linter, static type
check, coverage threshold, dependency vulnerability scan, secret scan, or
scheduled disposable-VPS test. CI runs Python 3.12 rather than the complete
declared Python support range.

The strict open-source audit also has one pre-existing blocker in
[the 0.4.3 change record](../changelog/0076-release-043.md), plus retained-name
warnings. That baseline should be repaired independently of this strategy.

## Current Product Architecture

### Existing Ownership Boundary

The current [Architecture](../architecture.md) establishes a sound separation:

- Ophelia owns manifests, generated runtime files, host operations, safety
  gates, receipts, and machine-readable contracts.
- Application repositories own source code, Dockerfiles, build pipelines, and
  `.ophelia.yml`.
- The VPS owns secrets, generated runtime state, images, volumes, logs, and
  backups.

That boundary should remain. The main architectural change is to make the VPS
side an independent, durable reconciler instead of a collection of synchronous
CLI operations.

### Current Components

| Component | Current responsibility | Assessment |
| --- | --- | --- |
| `ship` CLI | Human, CI, and agent entrypoint for validation, planning, mutation, inspection, and reports | Broad and machine-readable, but mutations do not share one executor |
| Manifest parser | YAML parsing into typed Python models | Extensive contract, insufficient grammar and containment enforcement |
| Renderer | Compose, Caddy, env, lock, support file, and static artifact generation | Deterministic foundation worth preserving |
| Deployment runtime | Local bundle write, addons, Docker, Caddy, static publish, release records | Operational but nontransactional |
| Remote transport | `rsync` plus remote `ship` over SSH | Useful bootstrap path, unsafe plan staging and no protocol handshake |
| Shared foundation | Caddy, PostgreSQL, Redis, Docker networks | Convenient, but isolation and supply-chain defaults are weak |
| State database | Rebuildable SQLite read model | Correctly avoids becoming a hidden second authority |
| Local API | Loopback HTTP discovery, jobs, state, and action execution | Prototype control transport, not a durable authenticated daemon |
| Workflow engine | Multi-node plans, confirmations, receipts, and resumability | Strong modeling, inherits preview-only node limitations |
| Lumen adapter | Read-only dashboard, capabilities, actions, readiness, timelines, and plugins | Valuable integration substrate, not a deployment provider |

### Capability Matrix

| Area | Status | Current behavior |
| --- | --- | --- |
| Manifest validation and rendering | Current | Parses manifests and deterministically renders runtime artifacts |
| Local deployment | Current, partial | Pulls images, starts Compose, reloads Caddy, publishes static files |
| Remote deployment | Current, partial | Stages through SSH and runs target checkout commands |
| Static publishing | Current | Uses immutable static releases and a `current` pointer |
| PostgreSQL addon | Current | Creates per-app database and role in a shared service |
| Redis addon | Current, weak isolation | Allocates logical databases under one shared password |
| Release history | Current | Keeps immutable rendered bundles and active/latest metadata |
| Runtime rollback | Partial | Restores files and pointers without live reactivation or verification |
| File backup | Current, partial | Copies configuration and metadata, not necessarily recoverable data |
| Portable export | Current, partial | Can include volume archives and optional `pg_dump` |
| Restore apply | Preview-only | Produces preview/report without restoring active runtime state |
| Import | Preview-only | Creates a rehearsal directory; cutover import is unsupported |
| Cutover | Preview-only | Records evidence/checkpoints without moving runtime, Caddy, or DNS |
| DNS traffic changes | Current | File-backed or Cloudflare mutations behind explicit gates |
| Host inventory | Metadata-only | Reads declared inventory and local observations |
| Placement | Metadata-only | Scores candidates without reserving or reconciling hosts |
| Secrets providers | Metadata-only | Reports name, presence, or freshness without fetching or injecting material |
| Observability | Partial | Summaries and point probes without ingestion, retention, or alerts |
| Plugins | Metadata-only | Validates and reports plugin contracts without execution |
| Lumen integration | Partial | Exposes read-only JSON surfaces, not live provider execution |

### Strengths To Preserve

1. **Machine-readable discovery.** The command catalog, action descriptors,
   JSON schemas, operation digests, receipts, and Lumen surfaces are unusually
   well suited to automation and AI operators.
2. **Minimal dependency surface.** Python and PyYAML keep installation and host
   recovery understandable.
3. **Deterministic rendering.** Desired runtime artifacts can be inspected and
   hashed before activation.
4. **Fixture-first validation.** Synthetic applications model important shapes
   without requiring private production data.
5. **Evidence discipline.** Plans, confirmation tokens, redaction, immutable
   release bundles, and receipts are the correct product vocabulary.
6. **Static sites as first-class workloads.** Static deployment is simple,
   fast, and already demonstrates atomic revision switching.
7. **Rebuildable read model.** Files remain authoritative while SQLite provides
   query ergonomics.
8. **Typed process execution.** Ordinary subprocesses avoid `shell=True`, and
   several path and archive surfaces already have meaningful defenses.

### Maintainability Hotspots

`src/ophelia/portability.py` is 7,435 lines and owns too many unrelated
responsibilities: export, import, rehearsal, cutover, provider interaction,
fresh-install cleanup, archive handling, verification, and receipt generation.
It is now a change-risk hotspot.

The desired decomposition is incremental:

```text
src/ophelia/portability/
  models.py
  archive.py
  export.py
  import_plan.py
  import_apply.py
  restore.py
  cutover.py
  cleanup.py
  verification.py
  providers.py
  receipts.py
```

Behavior should first be covered with characterization tests. Functions can then
move behind compatibility exports without a big-bang rewrite.

## Product Opportunity

### Product Thesis

Ophelia should become the dependable runtime substrate for everything the operator wants
to run on VPS infrastructure:

- static and marketing sites
- APIs and web applications
- revenue-producing SaaS products
- background workers and scheduled jobs
- PostgreSQL, Redis, and application-owned data services
- internal tools and private services
- Lumen authority and execution services
- AI workers, tool services, queues, and future GPU workloads

The product promise should be:

> Ophelia safely operates every application and service across my VPS fleet,
> and Lumen gives humans and AI agents one trustworthy place to inspect,
> approve, deploy, recover, and understand them.

### Operator Journeys

The finished platform should make these paths routine:

1. **Bootstrap a host.** Buy or rebuild a VPS, install Ophelia, enroll it, apply
   a hardening profile, and see it become ready in Lumen.
2. **Deploy an application.** Publish an immutable image, validate a manifest,
   inspect a plan, approve it, activate a revision, verify it, and retain a
   truthful receipt.
3. **Operate an application.** See desired and observed state, logs, health,
   resource pressure, certificates, drift, releases, and backup posture.
4. **Recover safely.** Roll back a runtime revision or restore application data
   onto a clean host with checksums and verification.
5. **Manage a fleet.** Enroll, drain, upgrade, place, and roll out across hosts
   without hand-maintained SSH knowledge.
6. **Delegate to agents.** Let a Lumen agent inspect and propose operations, but
   bind every mutation to explicit policy, scope, evidence, and approval.

### Comparable Platforms And Lessons

| Reference | What it demonstrates | What Ophelia should take |
| --- | --- | --- |
| [Lakebed](https://docs.lakebed.dev/) | Constrained agent-native application capsules, one-command deploy, inspection, scoped automation credentials | A bounded contract, one golden path, inspection before guessing, private defaults |
| [Coolify](https://coolify.io/documentation) | Expected self-hosted PaaS breadth: Git, builds, previews, certificates, services, backups, monitoring | Later-stage ergonomics and service catalog expectations |
| [Kamal](https://kamal-deploy.org/docs/commands/deploy/) | Immutable image deployment, readiness, traffic switch, old-container drain | Execution rigor without requiring a cluster scheduler |
| [Dokku](https://dokku.com/docs/deployment/zero-downtime-deploys/) | Simple application lifecycle and zero-downtime expectations | Clear operator workflows and release semantics |
| [Docker Swarm](https://docs.docker.com/engine/swarm/) | Desired state, secure membership, rolling updates, rollback, multi-host networking | A possible future backend when cross-host scheduling is truly required |
| [Docker rootless mode](https://docs.docker.com/engine/security/rootless/) | Reduced daemon and runtime privilege | An optional host hardening profile after compatibility testing |

Lakebed is a different layer. It is a constrained TypeScript application runtime;
Ophelia is a host and infrastructure runtime. Ophelia should borrow Lakebed's
discipline, not embed its framework. A Lakebed-like capsule profile could later
run on top of Ophelia.

The clearest positioning is:

> Kamal-level deployment correctness, Coolify-like operator ergonomics, and a
> Lumen-native evidence and approval model, optimized for a sovereign personal
> VPS fleet.

## Threat Model

### Trust Boundaries

```text
application repositories / CI / imported bundles / AI proposals
                              |
                              v
                 Lumen intent and Decision boundary
                              |
                    scoped provider identity
                              |
                              v
                 Ophelia control transport boundary
                              |
                              v
                  opheliad on each enrolled VPS
                    |         |          |
                    v         v          v
                 Docker     Caddy     addons and data
                    |         |          |
                    +---------+----------+
                              |
                     public and private traffic

External trust boundaries also exist at DNS providers, image registries,
backup storage, notification providers, and host enrollment.
```

### Relevant Actors

- the human operator
- an authorized Lumen AI agent
- an application CI identity
- a compromised project repository
- a compromised or malicious application container
- a network attacker
- a compromised VPS
- a malicious or corrupted imported artifact
- a compromised registry, dependency, or provider response

### Protected Assets

- the host filesystem and Docker socket
- host enrollment and signing keys
- DNS and traffic-provider credentials
- TLS private keys
- application secrets and database credentials
- databases, uploads, and volume data
- backup encryption keys and off-site archives
- deployment history, plans, receipts, and approval evidence
- Lumen identities, Decisions, Jobs, and correlation records

### Accepted Initial Assumptions

- The platform begins as single-operator, not hostile multi-tenant
  infrastructure.
- Rootful Docker may remain supported initially, but access to its socket is
  equivalent to host-root authority.
- Application manifests are infrastructure code and may request powerful
  behavior only through explicit capabilities.
- A fully compromised host cannot be made trustworthy by the control plane.
  The system must instead support revocation, rebuild, and restore.
- Lumen and Ophelia remain separate failure domains. Either can be temporarily
  unavailable without corrupting accepted host operations.

## Confirmed Risk Register

| ID | Severity | Confirmed behavior | Required invariant |
| --- | --- | --- | --- |
| R-01 | Critical | App identifiers can traverse runtime paths; route values can inject Caddy syntax | Every identifier, domain, route, URL, and path is parsed by a strict grammar before rendering or filesystem use |
| R-02 | Critical | Support sources and destination-like values can escape intended roots | All resolved paths remain inside configured roots after symlink resolution |
| R-03 | High | Remote `plan` rsyncs into the live app directory before planning | Planning performs zero live runtime mutation |
| R-04 | High | Rollback restores files and pointers without runtime activation | Rollback reactivates, moves traffic, verifies, and records observed state |
| R-05 | High | Deploy applies live mutations across phases without compensation | Activation is transactional or deterministically compensating |
| R-06 | High | Policy enforcement differs between CLI, API, and traffic paths | Every mutation uses one canonical policy-enforced executor |
| R-07 | High | Secret-bearing files can be `0644`; credentials are duplicated into addon metadata and backups | Secret material is minimal, `0600`, encrypted off host, and absent from evidence surfaces |
| R-08 | High | Backup success may cover metadata without application data; restore apply is preview-only | Backup success means a verified restore contract exists for declared data |
| R-09 | High | Redis uses one password across logical databases; all services join the edge network | Workloads and data services are isolated by default |
| R-10 | High | Local API lacks durable execution, authoritative identity, real cancellation, and transactional idempotency | Accepted work is authenticated, journaled, bounded, cancellable, and recoverable |
| R-11 | High | Mutable image pull failure may fall back to a stale local tag | Production activation records and enforces the exact image digest used |
| R-12 | High | Fresh-install cleanup can recursively remove an uncontained configured path after overrides | Destructive cleanup operates only on typed resources inside managed roots |
| R-13 | Medium | Docker, SSH, rsync, addon, and provider subprocesses can hang without deadlines | Every external call has timeout, cancellation, bounded output, and structured failure |
| R-14 | Medium | Static garbage collection scans `versions` while runtime writes `releases` | Retention scans the authoritative release layout and is integration tested |
| R-15 | Medium | Dependency, action, and base-image versions are not fully locked or scanned | Build and runtime inputs are reproducible, attributable, and policy checked |

### Evidence Anchors

- Manifest parsing and version handling:
  [manifest.py](../../src/ophelia/manifest.py)
- Runtime app path derivation:
  [runtime.py](../../src/ophelia/runtime.py)
- Direct Caddy rendering:
  [templates.py](../../src/ophelia/templates.py)
- Remote staging and rsync:
  [remote.py](../../src/ophelia/remote.py)
- Deployment phases:
  [runtime.py](../../src/ophelia/runtime.py)
- Rollback behavior:
  [rollback.py](../../src/ophelia/rollback.py)
- Policy selection and reporting:
  [policy.py](../../src/ophelia/policy.py)
- Secret-bearing addon metadata:
  [addons.py](../../src/ophelia/addons.py)
- Backup and preview restore:
  [backup.py](../../src/ophelia/backup.py)
- API and action execution:
  [api.py](../../src/ophelia/api.py) and
  [actions.py](../../src/ophelia/actions.py)
- Shared Caddy, PostgreSQL, Redis, and networks:
  [compose.yml](../../platform/shared/compose.yml)

## Required P0 Remediation

### Strict Input Boundaries

Implement a validation layer that produces canonical domain objects before any
renderer, path join, shell argument, provider call, or runtime mutation.

Recommended identifier grammar:

```text
^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$
```

Recommended limits:

- application and environment slugs: 1 to 63 characters
- service, network, volume, and route names: 1 to 63 characters
- operation identifiers: generated by Ophelia, never accepted as filesystem
  components without parsing
- no control characters anywhere in manifest string fields

Domain parsing must:

- normalize IDNA consistently
- reject embedded ports unless the field explicitly permits them
- reject whitespace and control characters
- validate wildcard placement
- enforce DNS label and total-length limits
- emit a canonical ASCII representation
- never pass raw domain text into a Caddy template

Path handling must use one primitive with this contract:

```python
def safe_join(root: Path, *parts: str, allow_missing: bool = False) -> Path:
    """Resolve a candidate and prove it remains contained by root."""
```

Containment must be checked after symlink resolution. Ordinary source paths must
remain inside the manifest repository root. Exceptional host mounts require a
separate typed capability and an explicit configured allowlist.

`Path.resolve()` and a containment comparison are necessary but not sufficient
for privileged writes because an attacker can swap a symlink after validation.
Mutation, extraction, and destructive cleanup code must operate relative to an
already opened trusted directory descriptor. On Linux, prefer `openat2` with
`RESOLVE_BENEATH` and `RESOLVE_NO_SYMLINKS`; otherwise use carefully composed
`openat` calls, `O_NOFOLLOW`, exclusive creation where appropriate, inode
revalidation, and `renameat`-style atomic promotion. Candidate trees must be
owned by the Ophelia user and have no user-writable parent. Destructive
operations should accept a typed managed-resource handle rather than reopen an
arbitrary path string.

Unknown manifest keys should first become warnings during a short compatibility
window, then hard errors. Unsupported manifest versions must fail before
semantic parsing. Extension keys, if needed, should live under a single explicit
`extensions` namespace.

Tests must include traversal, absolute paths, symlink escapes, newline and Caddy
directive injection, invalid IDNA, wildcard misuse, unsupported versions, and
unknown fields.

### Truly Read-Only Planning

Remote planning must stage into an immutable operation directory:

```text
/var/lib/ophelia/staging/<operation-id>/candidate/
```

The plan compares that candidate with active desired and observed state. It must
not write into an application directory, `sites.d`, Compose state, Docker,
addons, DNS, or the active SQLite projection.

"Read-only" means no change to protected desired, active, observed, provider, or
runtime surfaces. Planning may write only to its operation-specific staging
tree and an append-only plan journal. Tests should hash protected application,
active Caddy, addon, and runtime paths before and after planning, then assert
that every permitted filesystem delta is confined to staging or plan evidence.
Mocks should also prove that no Docker, Caddy, DNS, provider mutation, active
state transition, or live-directory rsync occurs.

### One Canonical Mutation Engine

CLI, local API, remote SSH, the future daemon, and Lumen must call the same
operation engine. Entry points may differ in transport and presentation, but
they must not implement independent policy, locking, confirmation, or receipt
rules.

The engine should accept a server-derived `Actor`, a parsed `OperationRequest`,
and an immutable `ApprovedPlanRef`. It should return an asynchronous
`OperationRef` and append events to a durable journal before acknowledging work.

All blocking policy results must block. `production_apply_enabled`, allowed
roots, concurrency limits, environment recognition, and image policies must be
implemented once in this engine.

### Secure Runtime Files

Add central primitives:

```python
secure_write(path, bytes, mode=0o600, owner=None)
secure_mkdir(path, mode=0o700, owner=None)
atomic_replace(candidate, target)
```

They must use temporary files in the destination filesystem, `fsync`, explicit
modes, and atomic rename. Startup should audit and optionally repair known
runtime permissions without following unsafe symlinks.

Raw PostgreSQL and Redis passwords should not be duplicated into `addons.json`.
That file should contain secret references and non-secret connection metadata.
Generated secret files must be ignored by Git even if bootstrap runs inside a
source checkout.

### Truthful Recovery Semantics

Rename incomplete operations where necessary while the replacement is built:

- current rollback behavior: `bundle restore`
- current restore apply: `restore preview`
- current cutover apply: `cutover checkpoint`

The public command names may remain temporarily for compatibility, but JSON
receipts must clearly identify `effect: preview`, `effect: files_only`, or
`effect: runtime_activated` until full semantics exist.

## Target Platform Architecture

### Architecture Principles

1. **Independent hosts.** Every enrolled VPS keeps serving and reconciling when
   Lumen is unavailable.
2. **Immutable desired state.** Plans and activations reference content-addressed
   manifests, artifacts, and revisions.
3. **Observed-state truth.** Success is based on runtime verification, not only
   written files.
4. **One executor.** All transports share authorization, policy, locking,
   idempotency, state transitions, and receipts.
5. **Least privilege.** Applications receive only declared networks, volumes,
   secrets, and host capabilities.
6. **Recovery is a product feature.** Backup, restore, rollback, and host rebuild
   are normal workflows with objective evidence.
7. **Agent-native, not agent-trusting.** Agents receive rich inspection and
   planning surfaces while mutations remain scoped and evidence-bound.
8. **Backend seam, not premature scheduler.** Compose remains the first runtime;
   a future orchestrator is an adapter behind the same revision contract.

### Component Topology

```text
                      +----------------------------+
                      |           Lumen            |
                      | projects, Decisions, Jobs, |
                      | Activity, Incidents, UI    |
                      +-------------+--------------+
                                    |
                         DeployProvider contract
                                    |
                   +----------------+----------------+
                   |                                 |
          CLI transport, first              agent protocol, target
                   |                                 |
             +-----v------+                    +-----v------+
             |    ship    |                    |  opheliad  |
             | bootstrap  |                    | per VPS    |
             | CI, break  |                    | reconcile  |
             | glass      |                    | journal    |
             +-----+------+                    +-----+------+
                   |                                 |
                   +----------------+----------------+
                                    |
                      runtime backend interface
                                    |
           +------------------------+------------------------+
           |                        |                        |
        Compose                   Caddy               addons/data
     revisions and health     traffic and TLS     volumes, DB, Redis
```

### Component Responsibilities

| Component | Owns | Must not own |
| --- | --- | --- |
| Application repository | Source, Dockerfile, tests, `.ophelia.yml`, build metadata | Host secrets, live runtime state, fleet credentials |
| CI | Build, test, publish immutable artifact, request deployment | Direct long-lived host authority |
| `ship` | Local validation, planning client, bootstrap, CI transport, break-glass operations | Independent long-running host reconciliation |
| `opheliad` | Runtime reconciliation, revisions, traffic, addons, backups, logs, metrics, durable host journal | Product projects, human approvals, AI scheduling |
| Lumen | Projects, targets, Decisions, policy context, supervisory Jobs, Activity, Incidents, UI, secret references | Docker commands, Caddy files, low-level runtime mutation |
| Runtime backend | Container or process activation for a revision | Cross-product authorization and approval decisions |
| DNS provider adapter | Scoped traffic records and observations | Application runtime state |
| Backup provider adapter | Encrypted object upload, retrieval, retention observations | Decrypted runtime secrets outside restore execution |

### Proposed Host Layout

The first daemon version should use a conventional system layout while allowing
an explicit development root:

```text
/etc/ophelia/
  agent.toml
  trust/
    host.crt
    host.key
    control-plane-ca.pem

/var/lib/ophelia/
  operations.db
  state.db
  desired/
  operations/<operation-id>/
    request.json
    plan.json
    events.jsonl
    receipt.json
  staging/<operation-id>/
  apps/<app>/<environment>/
    desired.json
    active.json
    observed.json
    revisions/<revision-id>/
      manifest.lock.json
      compose.yml
      caddy/
      support/
      artifact-lock.json
      revision.json
  static/<app>/<environment>/revisions/<revision-id>/
  backups/
  cache/

/var/log/ophelia/
  agent.jsonl
```

Secrets should preferably be materialized into an in-memory filesystem or a
root-only runtime directory outside immutable revisions. Secret references may
be recorded; secret values may not.

### Authority Matrix

| Entity | Authoritative store | Projection or cache |
| --- | --- | --- |
| Approved desired revision | Immutable revision files plus content digest | Lumen DeploymentReceipt and state queries |
| Host operation | `operations.db` WAL journal | JSON receipt and Lumen Job projection |
| Operation event | Append-only `operations.db` event journal | Lumen Activity, Job timeline, and Incident |
| Active revision | Transactional commit in `operations.db`, signed receipt, and immutable revision files | `active.json`, observed Docker/Caddy state, Lumen target state |
| Observed runtime | Runtime inspection snapshots with freshness | Lumen dashboards and readiness |
| Secret identity | Lumen `SecretRef` plus host secret binding | Presence and freshness metadata only |
| Backup object | Encrypted provider object plus signed backup manifest | Local cache and Lumen backup posture |

The existing `state.db` remains a disposable read projection and may be rebuilt
from files and observations. Proposed `operations.db` is different: it contains
accepted operation truth, idempotency, leases, event ordering, and active-state
commits, so it is not casually rebuildable. It must be backed up, integrity
checked, and continuously exported or acknowledged into Lumen.

If `operations.db` is corrupt, Ophelia stops mutation, preserves the damaged
database for recovery, and reconstructs a candidate state from immutable
revisions, signed receipts, event exports, active markers, Docker labels, and
Caddy targets. If these sources disagree, the host enters `recovery_required`
and requires an explicit reconciliation plan. It must not guess which revision
is active. Large artifacts and secret material remain outside both databases.

## Execution Kernel

### Operation Model

Every mutation should be represented by these related identifiers:

| Identifier | Purpose |
| --- | --- |
| `request_id` | Correlates the caller's attempt and transport retry |
| `plan_id` | Stable identifier for a calculated plan |
| `plan_digest` | Hash of normalized desired state, observations, policies, and ordered steps |
| `decision_id` | Lumen approval or equivalent server-side authorization evidence |
| `operation_id` | Durable asynchronous execution identity |
| `revision_id` | Immutable application revision activated by the operation |
| `event_id` | Unique event identity for replay and deduplication |
| `receipt_id` | Immutable terminal evidence artifact |

An approval must bind to exact content. It should not be a reusable text token.
The bound claims should include:

```json
{
  "schema_version": 1,
  "plan_id": "plan_...",
  "plan_digest": "sha256:...",
  "manifest_digest": "sha256:...",
  "artifact_digests": ["sha256:..."],
  "host_id": "host_example_1",
  "app": "demo-service",
  "environment": "production",
  "revision_id": "rev_...",
  "actor_id": "actor_...",
  "decision_id": "decision_...",
  "authorization_kind": "lumen_decision",
  "issuer": "lumen-control-plane",
  "audience": "host_example_1",
  "expires_at": "2026-07-11T18:00:00Z",
  "nonce": "one-time opaque value"
}
```

The nonce may be ephemeral. Durable receipts should retain the content digest
and decision reference, not a reusable credential.

For normal operation, Lumen should issue a short-lived signed `ApprovalClaim`
only after the Decision settles. The host verifies signature, trusted key ID,
issuer, audience, actor, operation scope, plan and artifact digests, Decision
reference, expiry, and nonce replay before accepting work. The claim can use a
standard signed envelope such as JWT, PASETO, or COSE, but the schema and
verification rules belong to the Ophelia protocol rather than transport
headers.

Break-glass operation uses a distinct `LocalApprovalEvidence` type derived from
authenticated Unix peer credentials and an interactive high-risk ceremony. It
is permitted only when host policy enables it, never masquerades as a Lumen
Decision, and is later replayed to Lumen as an exceptional audit event.

At the domain boundary:

```text
AuthorizationEvidence = LumenApprovalClaim | LocalApprovalEvidence
```

Every mutating operation requires one valid variant. A plain reference or
caller-supplied actor label is never sufficient.

### Canonical Execution Pipeline

```text
validate
  -> observe
  -> plan
  -> authorize
  -> stage
  -> preflight
  -> start candidate
  -> readiness verify
  -> switch traffic
  -> external verify
  -> drain previous
  -> commit active state
  -> emit receipt
```

The engine should express every phase with explicit preconditions,
postconditions, timeout, emitted events, and compensation.

| Phase | Success condition | Failure behavior |
| --- | --- | --- |
| Validate | Manifest, paths, policies, secrets metadata, and artifact references are valid | No runtime write |
| Observe | Fresh runtime, traffic, addon, disk, and dependency state recorded | Fail closed if required observations are unavailable |
| Plan | Deterministic ordered changes and blockers calculated | No runtime write |
| Authorize | Approval matches plan digest, scope, actor, host, and expiry | Reject without staging |
| Stage | Immutable revision and candidate edge config written outside active paths | Delete or quarantine incomplete candidate |
| Preflight | Images, env bindings, networks, storage, migrations, Caddy config, and capacity are ready | Preserve prior active revision |
| Start candidate | Uniquely named revision is running without traffic | Stop and remove candidate only |
| Readiness verify | Candidate startup and readiness checks pass within deadline | Stop candidate and preserve prior traffic |
| Switch traffic | Validated candidate Caddy config is atomically active | Restore previous edge config and verify it |
| External verify | Public or declared end-to-end checks pass | Switch traffic back, then stop candidate |
| Drain previous | In-flight work receives declared grace period | Keep old revision available and report incomplete drain |
| Commit | Active state and terminal receipt are committed transactionally | Recover from journal on restart |

No phase may claim success because a subprocess returned zero if the declared
runtime outcome has not been observed.

### Revision State Machine

```text
created
  -> staged
  -> preflight_passed
  -> starting
  -> ready
  -> traffic_candidate
  -> active
  -> draining
  -> inactive
  -> garbage_collectable

Any nonterminal state can transition to failed. A previously active revision
can transition from inactive to rollback_starting, then ready, then active.
```

Suggested operation states:

```text
accepted -> planning -> awaiting_approval -> queued -> executing
executing -> succeeded
executing -> compensating -> failed_compensated
executing -> failed_uncompensated
queued or executing -> cancelling -> cancelled
```

`failed_uncompensated` is a critical incident because desired and observed state
may differ. It must never be flattened into ordinary failure.

### Compose Revision Strategy

Containerized revisions must be able to overlap during verification. Use a
unique Compose project name such as:

```text
ophelia-<app>-<environment>-<revision-short-id>
```

Only routed web workloads join the shared edge network. A revision-specific
alias lets Caddy target the candidate without colliding with the active
revision. Internal workers and jobs remain on per-app networks.

Blue-green overlap applies directly to web and declared overlap-safe internal
services, not to every workload in one Compose project. The executor needs
workload-specific activation rules:

- **Web:** old and candidate revisions may overlap through readiness and traffic
  switching.
- **Worker:** default to `overlap: forbid`. Quiesce or fence the old worker,
  transfer queue ownership or lease generation, then start the new worker.
  Explicit overlap requires an application contract proving duplicate-safe work.
- **Cron:** one fenced scheduler owner per app/environment. A revision change
  transfers the fencing token; two revisions must not schedule the same tick.
- **Task:** start only for a separately accepted task operation with a
  transactional idempotency key.
- **Migration:** execute exactly once for the operation after declared backup
  and compatibility gates. It is not started as an ordinary long-running
  Compose service.
- **Static:** publish the complete immutable revision, then atomically switch
  the content pointer.

Shutdown ordering is explicit: stop new web traffic, drain web requests, fence
or hand off background consumers, preserve failed-task evidence, then remove
inactive containers. A single `docker compose up` for every workload is not a
sufficient activation primitive.

The runtime backend interface should expose at least:

```python
class RuntimeBackend(Protocol):
    def preflight(self, revision: Revision) -> PreflightResult: ...
    def start(self, revision: Revision) -> RuntimeHandle: ...
    def inspect(self, handle: RuntimeHandle) -> ObservedRevision: ...
    def stop(self, handle: RuntimeHandle, grace_seconds: int) -> StopResult: ...
    def remove(self, handle: RuntimeHandle) -> RemoveResult: ...
    def logs(self, handle: RuntimeHandle, cursor: str | None) -> LogBatch: ...
```

Compose is the first backend. Swarm, Nomad, or k3s can be considered later
without changing the application revision or Lumen provider model.

### Caddy Traffic Activation

Traffic activation should be its own transactional subsystem:

1. Render a complete candidate Caddy configuration from canonical route models.
2. Validate the complete candidate, including first deployment.
3. Write it to a versioned candidate directory.
4. Atomically replace the active include or symlink.
5. Reload Caddy with a deadline.
6. Inspect Caddy state and run declared external verification.
7. On failure, atomically restore the previous include and verify the rollback.

Caddy should not receive read access to the entire Ophelia runtime root. Mount
only required configuration, certificate, and static content paths with the
narrowest possible permissions.

### Genuine Rollback

Rollback is an activation operation whose desired revision happens to be an
older immutable revision. It must:

1. Confirm that required artifacts and secrets bindings still exist.
2. Re-run preflight against current host and data state.
3. Start the target revision under its original immutable artifact digest.
4. Pass readiness.
5. Move traffic back.
6. Run external verification.
7. Drain the replaced revision.
8. Record a rollback receipt linking both revisions and the triggering failure.

Database rollback is distinct. Ophelia should assume schema migrations are
forward-compatible unless a separately approved data-restoration plan exists.
Irreversible migrations must appear as high-risk plan blockers or explicit
manual gates.

### Concurrency And Leases

File locks with process lifetime are insufficient for a durable daemon. Use
transactional leases containing:

- resource key
- owner operation
- acquisition time
- expiry
- heartbeat
- fencing token

Recommended lock scopes and order:

1. host maintenance or upgrade
2. shared traffic configuration
3. shared addon allocator
4. application/environment
5. backup or restore resource

Operations must acquire locks in a stable order to prevent deadlock. A fencing
token prevents a recovered stale worker from committing after a newer owner has
taken the lease.

### Idempotency

Idempotency belongs in SQLite with a unique constraint over caller identity,
operation class, and idempotency key. The stored record includes the normalized
request digest.

- Repeating the same key and digest returns the original operation.
- Reusing the key with different content fails.
- A transaction creates the idempotency record and accepted operation together.
- Transport retries never execute a second mutation.

### External Process Discipline

Every Docker, Caddy, SSH, rsync, database, hook, provider, and archive subprocess
needs:

- phase-specific timeout
- process-group cancellation
- bounded stdout and stderr capture
- incremental redaction
- structured exit reason
- tool version in the receipt
- retry classification

Retries should be explicit and safe for the phase. Starting a container or
changing DNS cannot be retried using the same rules as a read-only inspection.

### Crash Recovery

The daemon must journal intent before each side effect and record observation
after it. On startup it scans nonterminal operations and decides whether to:

- continue from an idempotent phase
- re-observe before continuing
- compensate back to the prior active revision
- stop and create a manual-intervention incident

Fault-injection tests should terminate the daemon after every journal boundary.
The recovered state must be either the previous verified revision or the new
verified revision, never an unreported mixture.

### Receipt Contract

A terminal deployment receipt should contain:

```json
{
  "schema_version": 1,
  "kind": "ophelia.deployment.receipt",
  "receipt_id": "receipt_...",
  "operation_id": "operation_...",
  "plan_id": "plan_...",
  "plan_digest": "sha256:...",
  "decision_id": "decision_...",
  "host_id": "host_example_1",
  "app": "demo-service",
  "environment": "production",
  "previous_revision_id": "rev_previous",
  "active_revision_id": "rev_current",
  "artifact_digests": ["sha256:..."],
  "verification": {
    "status": "passed",
    "checks": []
  },
  "compensation": {
    "attempted": false,
    "status": "not_needed"
  },
  "observed_at": "2026-07-11T18:00:00Z",
  "outcome": "succeeded"
}
```

Secret values, confirmation nonces, bearer tokens, raw provider payloads, and
unbounded command output are forbidden.

## Manifest V2 And Workload Model

### Versioning Strategy

Manifest v2 should be introduced without silently reinterpreting v1:

1. Add `ship manifest check` with strict v1 diagnostics.
2. Run existing manifests in warning mode and enumerate incompatibilities.
3. Add `ship manifest migrate --to 2` that writes a candidate diff.
4. Require explicit `version: 2` for new workload features.
5. Continue reading valid v1 manifests through a canonical v1-to-domain adapter.
6. Set a documented date or release milestone for strict unknown-key rejection.

Equivalent manifests must normalize into the same canonical domain model and
produce the same semantic digest.

### Workload Kinds

Manifest v2 should model workloads independently of routes:

| Kind | Lifecycle | Route requirement |
| --- | --- | --- |
| `web` | Long-running, readiness-gated | Optional, usually one or more |
| `worker` | Long-running, no ingress | None |
| `cron` | Scheduled execution | None |
| `task` | Operator or automation-triggered one-shot | None |
| `migration` | Release-coupled one-shot with ordering and risk | None |
| `internal` | Long-running service reachable only on app networks | None |
| `static` | Immutable content revision served by edge | One or more routes |

Redirects and tunnels can remain specialized route targets rather than forcing
every deployment shape into a container service.

### Example Shape

```yaml
version: 2
app: demo-service
environment: production

artifacts:
  app_image:
    image: ghcr.io/example/demo-service@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

workloads:
  web:
    kind: web
    artifact: app_image
    command: ["./bin/server"]
    port: 8080
    replicas: 1
    startup:
      http:
        path: /startup
        port: 8080
      timeout_seconds: 60
    readiness:
      http:
        path: /ready
        port: 8080
      interval_seconds: 2
      timeout_seconds: 90
    liveness:
      http:
        path: /health
        port: 8080
      interval_seconds: 30
    resources:
      memory: 512Mi
      cpu: "1.0"
      pids: 256
    security:
      run_as_non_root: true
      read_only_root: true
      no_new_privileges: true
      drop_capabilities: ["ALL"]
    shutdown_grace_seconds: 30

  jobs:
    kind: worker
    artifact: app_image
    command: ["./bin/worker"]
    networks: [app]
    update:
      overlap: forbid

  cleanup:
    kind: cron
    artifact: app_image
    command: ["./bin/cleanup"]
    schedule: "17 3 * * *"
    concurrency_policy: forbid

migrations:
  pre_traffic:
    workload:
      kind: migration
      artifact: app_image
      command: ["./bin/migrate"]
    compatibility: backward_compatible
    timeout_seconds: 300

routes:
  - name: public
    domain: demo-service.example.com
    target:
      workload: web
      port: 8080

update:
  strategy: blue_green
  auto_rollback: true
  drain_seconds: 30

secrets:
  - name: DATABASE_URL
    ref: secret://demo-service/production/database-url
```

### Probe Semantics

- **Startup** answers whether the process has initialized enough for further
  checks. Its deadline can be comparatively long.
- **Readiness** answers whether the candidate may receive traffic or work.
- **Liveness** answers whether an already running workload should be restarted.
- **External verification** answers whether the complete path, including edge
  and dependencies, works after activation.

These checks must not be collapsed into a single optional health command.

### Update And Migration Semantics

Supported initial strategies:

- `recreate`: stop old, start new, appropriate only when downtime is accepted
- `blue_green`: overlap revisions and switch traffic atomically
- `static_atomic`: publish immutable content and switch the `current` pointer

Rolling replicas can come later when a runtime backend supports them correctly.

Pre-traffic database migrations should be compatible with both old and new
application revisions. Expand-and-contract migrations are the default. An
irreversible migration requires explicit policy, backup evidence, and a plan
that explains why runtime rollback alone cannot restore the prior state.

### Canonical Generation

The normalized manifest, host profile, artifact lock, secret-reference set, and
renderer version should produce a deterministic revision digest. Host-specific
secret values and runtime observations do not belong in that digest.

Generated artifacts should include:

- normalized manifest lock
- artifact lock with exact digests
- Compose or backend specification
- Caddy candidate model
- required secret references
- support-file hashes
- renderer and protocol versions
- migration and verification contract

## Security Architecture

### Host Enrollment And Identity

Bootstrap remains an explicitly trusted SSH operation. It should:

1. Verify and pin the host SSH fingerprint.
2. Install a dedicated `ophelia` system user and required runtime dependencies.
3. Install and start `opheliad` under systemd.
4. Generate a non-exportable host key where practical.
5. Exchange a short-lived one-time enrollment token for a host certificate.
6. Register host ID, architecture, region, capabilities, and protocol version.
7. Delete enrollment material.

Normal control traffic should use mutual TLS or an equivalent
proof-of-possession device identity. Certificates need expiry, rotation,
revocation, replay defense, and bounded clock-skew handling. Host identity must
be derived by the server from the authenticated credential, not from a request
body.

### Authorization

Authorization scopes should be composable across:

- workspace or operator account
- project
- environment
- host or host group
- application
- operation class
- risk level

Example operation scopes:

```text
ophelia:inventory:read
ophelia:plan:create
ophelia:deploy:apply
ophelia:rollback:apply
ophelia:traffic:write
ophelia:backup:create
ophelia:restore:apply
ophelia:host:drain
ophelia:host:upgrade
```

The server derives authoritative `actor_id`, `host_id`, and authorization scope.
Client-supplied `requested_by`, `source`, or similar labels may be retained as
untrusted display metadata only.

### Local API

Prefer a Unix domain socket owned by the Ophelia user with mode `0600`. If
loopback TCP remains available, it still requires authentication, request-size
limits, deadlines, concurrency limits, and origin-independent authorization.

The local API must never become an unauthenticated root-equivalent bridge merely
because it binds to `127.0.0.1`.

### Secret Flow

```text
Lumen SecretRef
  -> scoped host binding request
  -> host secret provider or encrypted host vault
  -> short-lived materialization for the target revision
  -> container secret file or tightly scoped environment injection
```

Rules:

- manifests contain secret references, never values
- plans and receipts contain names, references, presence, and freshness only
- secret files use `0600` beneath `0700` directories
- runtime material is removed when no revision needs it
- logs, events, command output, and error text pass through structured redaction
- opaque bearer tokens must be redacted, not only URL-shaped credentials
- addon state stores references, not duplicate passwords
- host bootstrap secrets required to start Lumen remain available independently
  of Lumen itself

### Container And Network Defaults

The secure default profile should be:

- only routed web services join the edge network
- each app/environment receives an internal network
- no host ports unless explicitly declared
- loopback binding for approved host-published ports
- `cap_drop: [ALL]`
- `security_opt: [no-new-privileges:true]`
- non-root user required when the image supports it
- read-only root filesystem when compatible
- explicit writable tmpfs and volumes
- memory, CPU, PID, and optional IO limits
- bounded log driver configuration and rotation
- restart policy appropriate to workload kind
- default seccomp profile, with deviations called out in the plan

PostgreSQL may remain shared with per-app roles and databases if privilege tests
prove isolation. Redis should use per-app ACL users with key prefixes or
separate instances. Shared-password logical databases are not a tenant boundary.

### Hooks, Probes, And Imported Artifacts

Manifests and restore artifacts are powerful infrastructure input. Host-side
hooks should run in a locked-down utility container with:

- no Docker socket
- no host root mount
- declared read-only and read-write paths
- restricted network or no network
- bounded CPU, memory, PIDs, output, and time
- explicit executable capability

HTTP probes need SSRF controls. By default they should reject link-local cloud
metadata addresses, unauthorized loopback targets, unexpected redirects, and
unbounded response bodies. Private-network probes require an explicit target
capability.

Archive imports require traversal and symlink defenses plus maximum member
count, total decompressed size, compression ratio, per-file size, and declared
content-type checks.

### Software Supply Chain

Production policy should require exact image digests. A failed pull may use a
local image only if the requested digest exists and its `RepoDigest` matches.
Mutable tags must never silently activate a stale local image.

Later policy levels can add:

- signature verification with Cosign or Notation
- provenance attestations
- SBOM attachment
- vulnerability severity thresholds with explicit waivers
- digest-pinned shared Caddy, PostgreSQL, and Redis images
- locked Python dependencies with hashes
- commit-pinned GitHub Actions

Receipts should record the actual runtime image ID and digest observed after
container creation.

### Rootful And Rootless Docker

Rootless Docker should be evaluated as a supported hardening profile, not made a
blind immediate requirement. Compatibility tests must cover low ports through
Caddy, volume ownership, network behavior, resource limits, and host upgrades.

If rootful Docker remains the default, the document and implementation must
state clearly that `opheliad` access to the Docker socket is host-root
equivalent. The daemon's network exposure and authorization then become a
critical host boundary.

### Audit Integrity

Operation history should be append-only at the API level and exportable off
host. Each event can include the prior event hash to make local tampering
detectable. High-risk receipts should be signed by the host key and ingested by
Lumen or off-site storage.

A host signature proves that the enrolled host key produced a receipt; it does
not prove the host was uncompromised. A compromised host holding its key can
create a different valid-looking chain until revocation. Tamper evidence becomes
meaningful only when journal heads and high-risk receipts are anchored promptly
in Lumen or immutable off-site storage. Recovery reviews compare the host chain
with those independently retained anchors.

Audit identity comes from authenticated credentials. Display strings supplied
by clients cannot be treated as proof of who authorized or executed an action.

## Reliability, Backup, And Observability

### Reliability Objectives

Initial service objectives should be explicit and adjustable by environment.
Suggested defaults for production are:

| Objective | Proposed target |
| --- | --- |
| Accepted-operation durability | Journaled before successful API acknowledgement |
| Plan latency | p95 under 5 seconds, excluding external artifact inspection |
| Local event visibility | p95 under 5 seconds after journal commit |
| Lumen event visibility | p95 under 30 seconds while connected |
| Failed readiness outcome | Prior verified revision remains active |
| Backup freshness, standard | Successful encrypted backup within 24 hours |
| Backup freshness, critical data | Successful encrypted backup within 1 hour where supported |
| Restore drill freshness | At least one verified clean-host drill every 30 days for critical apps |
| Default RPO | 24 hours for standard apps, 1 hour for critical data classes |
| Default RTO | 4 hours for standard apps, 1 hour for critical services after host availability |

These are initial product targets, not current guarantees. Each application may
declare stricter or looser objectives, and the plan must explain when host or
provider capability cannot satisfy them.

### Unified Backup Contract

Configuration snapshots, portability exports, database dumps, volume archives,
and restore verification should become one backup domain.

A backup manifest should include:

```json
{
  "schema_version": 1,
  "kind": "ophelia.backup.manifest",
  "backup_id": "backup_...",
  "app": "demo-service",
  "environment": "production",
  "revision_id": "rev_...",
  "created_at": "2026-07-11T03:17:00Z",
  "consistency": "application_consistent",
  "resources": [
    {
      "kind": "postgres",
      "logical_name": "primary",
      "object": "objects/postgres.dump.zst.age",
      "sha256": "..."
    },
    {
      "kind": "volume",
      "logical_name": "uploads",
      "object": "objects/uploads.tar.zst.age",
      "sha256": "..."
    }
  ],
  "encryption": {
    "scheme": "age",
    "recipient_ids": ["recovery-key-1"]
  },
  "tool_versions": {},
  "verification": {}
}
```

Application data declarations should state:

- data class and criticality
- consistency method
- quiesce or pre-backup hook
- dump or snapshot mechanism
- expected maximum size and duration
- RPO and RTO
- retention class
- restore verification command or assertion

Consistency is an evidence-backed level, not a free-form success label:

| Level | Required evidence |
| --- | --- |
| `crash_consistent` | Objects were captured without a coordinated write barrier; restore may require application recovery |
| `database_consistent` | Database-native dump, snapshot, or recovery point completed successfully |
| `application_consistent` | A declared barrier quiesced relevant writers, captured all related stores, and resumed them successfully |
| `point_in_time` | Base backup or snapshot plus continuous log/WAL coverage proves recovery to a bounded time |

An application-consistent operation follows a declared barrier: acquire a
backup lease, run the pre-backup hook, pause or fence relevant workers and
writers, record the barrier time, capture the database and coupled volumes,
verify every object, resume writers in reverse order, and run a post-backup
check. If capture fails, the executor resumes the application before reporting
failure and records which objects are unusable. Restore reverses dependency
order: provision storage, restore databases and volumes, run recovery hooks,
then activate workloads and verify cross-store invariants.

PostgreSQL backups need a consistent `pg_dump` or physical-backup policy with
the exact server and client versions recorded. Redis workloads must declare
whether Redis is a disposable cache or persistent data, and if persistent,
which RDB or AOF guarantees are required. Filesystem volumes may require
application quiescing before archive or snapshot.

Hourly RPO cannot be claimed merely because an hourly dump is scheduled. If
dump duration, upload duration, or provider reliability cannot keep the recovery
window below one hour, the critical profile requires PostgreSQL WAL archiving or
provider point-in-time recovery, plus equivalent volume or object-store
snapshots where coupled data exists.

### Backup Storage And Key Separation

Backups should be encrypted before leaving the host and uploaded to an
S3-compatible provider or another versioned off-site backend. The host may hold
an encryption public key, while the recovery private key remains separate from
the VPS and normal deployment credentials.

Retention must be declarative and enforced only after:

- a newer backup is verified
- provider listing is fresh
- retention policy is calculated deterministically
- delete candidates are included in a plan
- protected recovery points are excluded

A host compromise should not allow an attacker to decrypt old backups or
silently erase every recovery point. Object lock, provider versioning, a
separate deletion credential, or an offline copy should be available for
critical data.

### Destructive Restore State Machine

```text
requested
  -> inspecting
  -> awaiting_approval
  -> downloading
  -> decrypting
  -> validating
  -> provisioning_target
  -> restoring_data
  -> activating_runtime
  -> verifying
  -> succeeded
```

Failure before target mutation leaves the target untouched. Failure after data
mutation isolates the incomplete target and creates an incident. The previous
production environment is not modified unless the approved plan explicitly
targets it.

Restore verification must check:

- manifest and backup schema compatibility
- object checksums before extraction
- archive quotas and safe paths
- database connectivity and declared invariants
- volume content assertions
- application startup and readiness
- external behavior on a non-production rehearsal route

The platform should run scheduled restore drills onto disposable or quarantined
targets and surface the last successful drill, duration, recovered revision,
and verification results in Lumen.

### Continuous Observability

Ophelia should collect low-level runtime observations and emit normalized
events. It does not need to become a full metrics database.

Minimum host and workload signals:

- daemon and runtime-backend health
- container state, restart count, exit code, and OOM status
- readiness and liveness state
- HTTP latency and status for declared probes
- CPU, memory, PID, network, and filesystem use
- disk free space and inode pressure
- Caddy reload status and certificate expiry
- backup age, size, upload result, and restore-drill age
- database reachability and storage pressure
- desired-versus-observed drift
- agent and protocol version

Ophelia can expose Prometheus-compatible metrics and structured event/log
streams. Lumen should own the operator-facing incident and notification
experience.

### Logs

Log handling needs:

- structured metadata for host, app, environment, workload, revision, and stream
- bounded local retention and rotation
- cursor-based streaming
- server-side access control
- encryption at rest or through the selected log backend
- registered-value redaction before off-host delivery
- maximum query ranges and download sizes
- explicit treatment of multiline and binary output

Containers must not be able to fill the host disk through unbounded default
Docker logging. Application output is always potentially sensitive because a
workload can print an unknown secret that Ophelia cannot recognize. Access,
retention, export, and incident response must assume redaction is incomplete.

### Alert And Incident Conditions

At minimum, emit actionable events for:

- deployment failure or uncompensated state
- readiness or health degradation
- restart loop or OOM kill
- certificate nearing expiry
- disk, inode, memory, or PID pressure
- backup failure or stale backup
- failed or stale restore drill
- configuration drift
- missing secret binding
- agent offline or version incompatible
- DNS or edge disagreement
- suspected credential replay or authorization failure spike

Alerts should carry remediation context and relevant operation, revision, host,
and application references without leaking secrets.

## Fleet Model

### Independent Hosts First

The initial fleet is a set of independently managed hosts, not one distributed
cluster. This reduces failure coupling and lets the platform establish reliable
host lifecycle, inventory, deployment, recovery, and upgrades before attempting
automatic rescheduling.

Applications can still use multiple hosts through explicit deployment targets
and controlled rollout groups. Cross-host traffic and data replication remain
application or provider concerns until a scheduler backend is intentionally
selected.

### Host Record

A host should advertise:

- stable host ID and enrollment identity
- provider, region, zone, and labels
- CPU architecture and operating system
- total and available CPU, memory, disk, and inodes
- Docker or runtime-backend version
- Caddy and addon capabilities
- Ophelia agent and protocol versions
- supported workload and backup features
- app and revision inventory
- maintenance, drain, and health state
- last heartbeat and observation freshness

Lumen may specify placement constraints, but Ophelia must verify them against
fresh host observations before activation.

### Host Lifecycle

```text
unregistered
  -> enrolling
  -> ready
  -> degraded
  -> draining
  -> maintenance
  -> ready
  -> revoked or decommissioned
```

Drain prevents new placements and moves or stops declared workloads through
separately approved operations. Maintenance permits agent and foundation work
without pretending application availability is normal.

### Placement

Initial placement should be recommendation plus explicit target selection. A
host is eligible only when it satisfies:

- environment and region constraints
- architecture and runtime compatibility
- declared capabilities
- disk and memory headroom
- network and provider requirements
- secret binding availability
- data locality and backup posture
- not drained, revoked, degraded beyond policy, or version incompatible

The placement record must explain why each candidate was accepted or rejected.
Reservation and automatic placement can come after race-safe capacity accounting
exists.

### Rollouts

Fleet rollouts should support:

- canary host or percentage
- fixed batches
- maximum unavailable
- health observation window
- pause and resume
- automatic stop on failure threshold
- explicit rollback plan

Each host executes locally and emits its own receipt. A fleet operation is an
aggregate that cannot overwrite the truth of individual host outcomes.

### Offline Semantics

If Lumen is offline, an already accepted host operation finishes locally,
journals events, and replays them later.

If a host is offline before acceptance, the default is explicit failure rather
than an invisible unbounded queue. An optional queued intent must have an expiry,
visible pending state, cancellation, and a re-plan requirement if observations
are stale when the host reconnects.

### Scheduler Backend Seam

Do not recreate a scheduler through incremental placement features. Revisit an
orchestrator only when requirements include several of:

- automatic rescheduling after host failure
- service replicas spanning hosts
- rolling updates across replicas
- overlay networking
- service discovery across hosts
- placement by resource availability
- cluster-managed secrets

At that point, compare Docker Swarm, Nomad, and k3s against the same Ophelia
`Revision`, `RuntimeBackend`, event, and receipt contracts.

## Lumen Integration

### Current Integration State

Lumen already contains appropriate control-plane primitives for projects,
platform targets, deployment receipts, Jobs, Runs, Decisions, Activity,
Incidents, monitoring, Connections, authentication, and secret references.

It also already deploys through Ophelia in CI and exposes application-owned
health and release endpoints. However, that operational CI path is separate from
Lumen's in-application deploy operator. The current in-application plan is
descriptive rather than a provider-calculated Ophelia plan, verification steps
can be marked successful without real provider verification, and deployment
work is not yet a durable asynchronous provider operation. The current Ophelia
application manifest covers the Lumen web authority but omits its worker, so the
integration has not yet proven background-workload lifecycle semantics.

The integration goal is one deployment system, not two adjacent systems.

### Ownership Mapping

| Lumen concept | Ophelia concept | Boundary |
| --- | --- | --- |
| Project | Owning application or product | Lumen owns product organization |
| `PlatformTarget` | Application, environment, and target-host policy | Lumen owns intent; Ophelia verifies host suitability |
| `Decision` | Authorization to perform a risky operation | Lumen owns approval evidence |
| Provider confirmation | Exact approved-plan binding | Ophelia owns plan integrity and replay defense |
| `Job` and Run | Supervisory projection of an asynchronous operation | Ophelia owns host execution journal |
| `DeploymentReceipt` | User-facing projection of plan, operation, revision, verification, and artifacts | Both reference one correlation chain |
| Activity | Projection of Ophelia events and other Lumen records | Lumen owns user-facing activity |
| Incident | Actionable failed or degraded runtime state | Lumen owns lifecycle; Ophelia supplies evidence |
| `Connection` | Provider or host access posture | Lumen stores references and scopes, not runtime secret values |
| `SecretRef` | Named runtime secret binding | Ophelia materializes only on the target host |

A VPS host and a Lumen execution node are not the same entity. One VPS may host
zero, one, or several Lumen services. Deployment placement and AI-execution-node
affinity must remain separate concepts.

### DeployProvider Contract

Lumen should integrate through a typed provider rather than a generic arbitrary
command adapter:

```typescript
interface DeployProvider {
  capabilities(profileId: string): Effect<DeployProviderCapabilities>
  inventory(profileId: string): Effect<HostAndAppInventory>
  validate(request: ValidateDeploymentRequest): Effect<ValidatedDeployment>
  plan(request: PlanDeploymentRequest): Effect<ProviderDeployPlan>
  apply(request: ApplyDeploymentRequest): Effect<ProviderOperationRef>
  getOperation(ref: ProviderOperationRef): Effect<ProviderOperation>
  cancel(ref: ProviderOperationRef): Effect<ProviderOperation>
  rollback(request: RollbackDeploymentRequest): Effect<ProviderOperationRef>
  getRevision(ref: ProviderRevisionRef): Effect<ProviderRevision>
  operationEvents(
    ref: ProviderOperationRef,
    cursor?: string
  ): Stream<ProviderOperationEvent>
  getLogs(request: ProviderLogRequest): Stream<ProviderLogEvent>
  readiness(request: ProviderReadinessRequest): Effect<ProviderReadiness>
  backupStatus(request: ProviderBackupRequest): Effect<ProviderBackupStatus>
  runRestoreDrill(request: RestoreDrillRequest): Effect<ProviderOperationRef>
}
```

First implement `OpheliaCliClient` using the versioned `ship --json` surface.
Then add `OpheliaHostClient` over the authenticated agent protocol. Lumen domain
logic should not change when the transport changes.

### Approval And Apply Flow

```text
1. Lumen resolves Project, PlatformTarget, actor, and policy context.
2. DeployProvider validates and returns a real Ophelia plan.
3. Lumen stores the plan and diff as immutable Artifacts.
4. Lumen creates or resolves a Decision for the exact plan digest.
5. A server-side mutation handler proves the Decision is settled.
6. Lumen signs a short-lived `ApprovalClaim` containing the Decision reference,
   plan and artifact digests, actor, scope, audience host, expiry, and nonce.
7. Ophelia authenticates the provider, verifies the claim and replay state,
   journals an operation, and returns immediately.
8. Ophelia executes locally and emits replayable events.
9. Lumen projects events into Job, Run, DeploymentReceipt, Activity, and Incident.
10. Terminal provider evidence closes the supervisory Job.
```

Direct WebSocket or HTTP mutation handlers must not bypass the settled Decision
check. A Decision ID alone is not authorization proof. A raw confirmation nonce
must not be persisted or returned through ordinary read-scoped receipt APIs.

### Correlation Chain

One correlation chain must connect:

```text
Project
  -> PlatformTarget
  -> Decision
  -> plan and immutable artifacts
  -> Ophelia operation
  -> host revision
  -> verification
  -> receipt
  -> rollback, if any
  -> Incident, if actionable
```

This is more valuable than copying complete provider payloads into several
Lumen tables.

### Event Contract

Initial event types:

- `deployment.planned`
- `deployment.started`
- `deployment.step`
- `deployment.succeeded`
- `deployment.failed`
- `deployment.compensating`
- `deployment.rolled_back`
- `release.traffic_changed`
- `drift.detected`
- `app.health_changed`
- `backup.completed`
- `backup.failed`
- `restore_drill.completed`
- `secret.binding_missing`
- `host.resource_pressure`
- `host.agent_state_changed`

Each event needs:

- immutable event ID and schema version
- host, app, environment, operation, and revision references where applicable
- monotonically increasing per-host sequence or journal cursor
- server-derived actor and provider identity where applicable
- bounded redacted payload
- timestamp plus host clock-quality metadata

Delivery is at least once. Lumen deduplicates by provider event ID, preserves
ordering within one host journal, stores a cursor, and can replay after
disconnection. Backpressure must not block host reconciliation, but the journal
also cannot grow without bound.

Events need priority and retention classes:

- security events, authorization evidence, state transitions, terminal
  receipts, compensation, and data-recovery evidence are durable until anchored
  off host
- progress and metric events may be coalesced into bounded summaries
- debug and high-volume log events may expire or be dropped according to an
  explicit policy

The journal enforces byte and age quotas, reserves capacity for terminal and
security evidence, and can archive signed segments to off-site object storage.
At a critical disk watermark, the daemon stops accepting new mutating operations
before it would lose required evidence. It continues health reporting and safe
recovery using reserved space.

### Lumen UI Requirements

The cockpit should expose:

- desired, active, and observed revision
- manifest and plan diff
- policy and Decision evidence
- operation timeline and live step state
- active and previous revisions
- readiness, liveness, and external verification
- drift and uncompensated failures
- host capacity and pressure
- scoped log streaming
- backup freshness, retention, and restore-drill evidence
- rollback and recovery actions
- provider and agent version compatibility

The UI should never need to parse human CLI output or infer success from a CI
job label.

### AI Harness Boundary

Ophelia may deploy and supervise Lumen workers, queue consumers, tool servers,
and future GPU workloads. Those workers still lease AI jobs from Lumen. Ophelia
owns workload lifecycle and resource isolation; Lumen owns model selection,
budgets, agent runs, tools, and task scheduling.

This avoids two competing schedulers and lets either subsystem evolve without
confusing deployment work with AI work.

### Migration From Current Lumen Deployment Paths

1. Fix Lumen deploy-operator approval and verification correctness.
2. Implement `OpheliaCliClient` and use real Ophelia plans in the Lumen UI.
3. Ingest existing CI deployment reports as provider artifacts and events.
4. Make apply asynchronous and idempotent through the provider contract.
5. Introduce `opheliad` and `OpheliaHostClient` on one canary host.
6. Move Caddy, tunnel, and host lifecycle details out of application CI.
7. Translate or dual-write legacy deployment records during a bounded migration
   window.
8. Declare the provider operation and Ophelia host journal authoritative, then
   remove the duplicate descriptive execution path.

## Later Product Ergonomics

The trusted kernel should enable, but not be delayed by, a richer PaaS
experience.

### Source And Git Deployments

Optional later source builds can support BuildKit, Nixpacks, or buildpacks. The
build service should still produce an immutable OCI image, SBOM, provenance,
logs, and digest before handing control to the ordinary revision pipeline.

Expected workflows:

- connect a Git repository
- select branch and environment
- auto-deploy after CI success
- create pull-request preview environments
- promote the same artifact between environments
- retain build and deploy as separate receipts

Artifact-first deployment remains the near-term default because it keeps
Ophelia's runtime smaller and makes rollback deterministic.

### Templates And Services

A template catalog can provide reviewed manifests for:

- static sites
- common web stacks
- PostgreSQL and Redis profiles
- object storage gateways
- analytics and monitoring tools
- queues and background-worker patterns
- private tools behind access control

Templates must generate ordinary manifests and plans. They must not create a
second hidden deployment system.

### Daily Operator Ergonomics

High-value commands and Lumen surfaces include:

- deploy, promote, rollback, restart, stop, and scale
- log tail and bounded historical search
- one-shot task execution
- shell or exec through an explicitly high-risk, audited capability
- secret binding and rotation status
- database connection or console through short-lived scoped credentials
- generated development and preview domains
- maintenance windows and host drain
- resource and estimated-cost summaries
- backup-now and restore-drill actions

### Optional AI Workload Profile

After worker, task, resource, and network contracts are stable, an AI profile
can add:

- queue and lease configuration
- GPU and accelerator labels
- model cache volumes
- outbound egress policy
- tool-server allowlists
- trace and cost metadata
- token and runtime budgets
- checkpoint and replay support
- ephemeral workspaces
- high-volume structured event export

These are workload capabilities, not a special AI scheduler inside Ophelia.

## Technical Implementation Map

### Recommended Module Boundaries

New behavior should be introduced behind narrow modules rather than extending
the largest existing files indefinitely.

```text
src/ophelia/
  domain/
    identifiers.py
    manifests.py
    operations.py
    revisions.py
    hosts.py
    backups.py
    events.py
  validation/
    paths.py
    domains.py
    routes.py
    artifacts.py
  execution/
    engine.py
    phases.py
    policy.py
    leases.py
    idempotency.py
    subprocesses.py
    recovery.py
  runtime_backends/
    base.py
    compose.py
  traffic/
    models.py
    caddy.py
    dns.py
  agent/
    service.py
    protocol.py
    enrollment.py
    auth.py
    upgrades.py
  secrets/
    models.py
    materialize.py
    permissions.py
  backups/
    models.py
    create.py
    restore.py
    retention.py
    providers.py
  observability/
    events.py
    metrics.py
    logs.py
```

This is a target boundary, not a requirement to move every existing function at
once. Introduce domain models and the executor first, then migrate one operation
at a time behind compatibility wrappers.

### Operation And Projection Databases

Suggested authoritative `operations.db` tables:

| Table | Purpose |
| --- | --- |
| `operations` | Request, normalized digest, state, actor, scope, timestamps, terminal outcome |
| `operation_events` | Ordered append-only per-operation and per-host event stream |
| `idempotency_keys` | Caller, operation class, key, request digest, operation ID |
| `leases` | Resource lease, owner, expiry, heartbeat, fencing token |
| `revisions` | App, environment, digest, artifact lock, lifecycle state |
| `active_revisions` | Transactional current active revision per app/environment |
| `observations` | Fresh desired-versus-observed runtime snapshots |
| `hosts` | Identity, capabilities, state, heartbeat, version |
| `secret_bindings` | Reference, target scope, provider, presence, freshness, never material |
| `backups` | Backup manifest, provider object, verification, retention state |
| `event_delivery` | Control-plane cursor and acknowledgement state |

Use foreign keys, unique constraints, explicit transactions, WAL, and a schema
migration mechanism. Keep large logs, archives, rendered artifacts, and secret
material outside SQLite.

The existing rebuildable `state.db` should continue to hold query-oriented
projections such as app inventory, receipt summaries, routes, readiness, drift,
and observed provider state. Projection rebuild must never overwrite or invent
operation-journal truth. A periodic integrity check, encrypted database backup,
signed event export, and Lumen acknowledgement cursor protect the irrecoverable
parts of `operations.db`.

### Agent Protocol And Network Topology

The recommended remote topology is an outbound persistent HTTP/2 or WebSocket
stream from each host agent to a Lumen control endpoint on port 443, authenticated
with mutual TLS. No public inbound Ophelia port is opened by default. The local
`ship` client uses the authenticated Unix socket.

On connection, the agent sends host identity, capabilities, protocol versions,
and last acknowledged event cursor. Lumen delivers signed command envelopes on
the stream. The agent journals and acknowledges acceptance before execution,
then batches events until Lumen advances the cursor. Disconnect prevents new
remote commands but does not interrupt accepted local work. Reconnect resumes
from cursors without repeating mutation effects. Certificate rotation occurs
over an authenticated live channel before expiry; revocation prevents a new
connection immediately. Direct inbound HTTPS may be an optional WireGuard-only
profile later, not the default fleet topology.

The logical API is versioned from the start. Its methods are available as local
Unix-socket endpoints and equivalent remote stream messages:

```text
GET  /v1/capabilities
GET  /v1/hosts/self
GET  /v1/apps
GET  /v1/apps/{app}/{environment}
POST /v1/plans
POST /v1/operations
GET  /v1/operations/{id}
POST /v1/operations/{id}/cancel
GET  /v1/events?cursor=...
GET  /v1/logs?app=...&revision=...&cursor=...
POST /v1/rollbacks
POST /v1/backups
POST /v1/restores
POST /v1/host/drain
POST /v1/host/maintenance
```

Requests and responses include a protocol version, schema version, request ID,
idempotency key for mutations, bounded body size, and structured errors.

The daemon advertises a capability map so an older Lumen provider can avoid
calling unsupported operations. Incompatible versions fail safely with an
actionable upgrade requirement.

### systemd Service

`opheliad` should ship with:

- a dedicated system user and group
- restrictive `UMask=0077`
- explicit runtime, state, and configuration directories
- restart-on-failure with bounded backoff
- readiness notification and watchdog
- journald or structured file logging with rotation
- hardened systemd sandboxing compatible with the chosen Docker access model
- an idempotent installer and uninstaller
- staged agent self-upgrade with health check and rollback

Host bootstrap should also cover Docker installation or validation, firewall
posture, SSH assumptions, unattended security updates, disk/log policies, NTP,
and recovery instructions.

### Configuration

Current settings such as allowed manifest roots, allowed runtime roots, maximum
concurrent jobs, and production apply enablement must become enforced typed
configuration. Configuration loading should fail on unknown keys, insecure file
modes, invalid paths, or contradictory settings.

Effective configuration should be inspectable in redacted JSON with source
provenance for every value.

## Implementation Roadmap

The phases below are dependency order, not calendar estimates. Each phase should
have a single accountable implementation owner and an explicit exit review.

### P0: Trust The Kernel

Goal: make plans read-only and mutation outcomes truthful.

Work:

1. Add strict identifiers, domains, routes, version handling, unknown-key
   diagnostics, path containment, and archive quotas.
2. Add secure atomic file and directory primitives, permission audit and repair,
   and generated-secret ignore rules.
3. Move remote planning into immutable operation staging.
4. Fix policy selection and route CLI, API, and remote apply through one
   canonical executor.
5. Add the minimal `operations.db` journal, transactional idempotency, durable
   leases, startup recovery, deadlines, cancellation, and structured subprocess
   results. The journal begins inside the CLI executor before it becomes a
   daemon in P1.
6. Introduce immutable container revisions with unique Compose project names
   and workload-specific web, worker, cron, task, and migration activation.
7. Implement candidate readiness, atomic Caddy switch, external verification,
   compensation, and genuine rollback.
8. Make receipts reflect observed runtime and exact artifact digests.
9. Fix static release garbage collection against the authoritative layout.
10. Add adversarial, property, fault-injection, concurrency, and live Docker
    integration tests.

Definition of done:

- Local and remote plans produce zero live mutation.
- Traversal and configuration injection are rejected before rendering.
- A blocked policy refuses the same request through every entrypoint.
- Fault at every activation phase leaves either the old or new verified revision
  active.
- Rollback visibly restores previous application behavior.
- Worker and cron activation cannot create duplicate consumers or schedulers
  unless an explicit duplicate-safe overlap contract is present.
- Ophelia-controlled plans, receipts, events, and diagnostics never
  intentionally emit registered secret material; seeded known secrets are
  redacted. Application logs are treated as potentially sensitive because
  arbitrary workloads can print unknown values.

### P1: Production-Grade Single Host

Goal: make one VPS independently operable and recoverable.

Work:

1. Promote the P0 journaled executor and recovery engine into `opheliad`; add
   the Unix-socket local API, long-running reconciliation, and systemd service.
2. Implement idempotent host bootstrap, hardening checks, agent upgrades, and
   break-glass recovery.
3. Add workload security contexts, per-app networks, routed-edge membership,
   Redis ACL or instance isolation, and resource limits.
4. Enforce artifact digests and add optional signature, provenance, SBOM, and
   vulnerability policies.
5. Unify backup/export, add encryption and off-site storage, and implement real
   restore onto a clean host.
6. Add continuous health, metrics, logs, disk pressure, certificate, backup, and
   restore-drill signals.
7. Add manifest v2 for web, worker, cron, task, migration, internal, and static
   workloads.

Definition of done:

- A fresh VPS can be bootstrapped idempotently and survives reboot.
- An application deploys, rolls back, backs up, and restores onto a clean second
  host within declared objectives.
- Runtime inspection confirms network and container security defaults.
- Disk-full, failed pull, failed readiness, failed Caddy validation, Docker
  restart, and daemon termination have deterministic outcomes.

### P2: Fleet Runtime

Goal: manage many independent hosts through authenticated agents.

Work:

1. Implement enrollment, mutual authentication, rotation, revocation, and
   protocol negotiation.
2. Add host heartbeats, capability inventory, maintenance, drain, and upgrade
   rings.
3. Implement explanatory placement, reservation, and capacity accounting.
4. Add replayable host events, cursors, acknowledgement, and disconnected
   recovery.
5. Add canary and batched fleet rollouts with aggregate status.
6. Complete actual app import and cutover workflows between hosts.

Definition of done:

- Three disposable VPSs enroll, report capabilities, drain, upgrade, and receive
  a controlled rollout.
- Revoked, expired, replayed, and wrong-scope credentials are rejected.
- Disconnected hosts replay events without duplicate effects.
- Placement excludes drained, incompatible, and insufficient-capacity hosts.

### P3: Lumen Cockpit

Goal: make Lumen the authoritative human and AI operator experience over
Ophelia's runtime truth.

Work:

1. Correct settled-Decision enforcement and remove durable raw confirmation
   nonces from user-facing records.
2. Implement the CLI-backed `DeployProvider` using real Ophelia plans and
   verification.
3. Project asynchronous operations into Jobs, Runs, DeploymentReceipts,
   Activity, Artifacts, and Incidents.
4. Replace the CLI transport with the authenticated host-agent transport.
5. Add desired-versus-observed state, operation timeline, revision, drift,
   logs, backup, restore, and host-health UI.
6. Consolidate the existing CI-only and descriptive deploy paths.

Definition of done:

- Every normal Lumen mutation proves a settled Decision through a signed,
  plan-bound claim. Exceptional local recovery proves separately typed
  break-glass evidence and is replayed into Lumen.
- One correlation chain spans Lumen and Ophelia.
- Lumen downtime does not corrupt an accepted host operation.
- Duplicate and replayed events are harmless.
- UI status is derived from provider evidence, not inferred success.

### P4: PaaS Ergonomics

Goal: make common application hosting dramatically faster without weakening the
kernel.

Work:

- optional source builds with immutable OCI output
- Git-triggered deployment
- pull-request preview environments
- generated domains and certificate posture
- environment promotion using the same artifact digest
- reviewed templates and one-click services
- secrets workflow and rotation status
- restart, task, exec, scale, and log-tail ergonomics
- resource and cost summaries

Definition of done:

- A new synthetic SaaS fixture can go from repository to verified preview and
  production deployment without host-specific manual configuration.
- Every convenience workflow still resolves to ordinary plans, policies,
  revisions, events, and receipts.

### P5: AI Workload Profile

Goal: host Lumen and other AI workloads as first-class, isolated applications.

Work:

- worker and queue templates
- accelerator capability and placement labels
- egress and tool-server policy
- model-cache and ephemeral-workspace volumes
- trace, token, cost, and checkpoint metadata
- high-volume event and log handling

Definition of done:

- Lumen deploys and supervises its workers through Ophelia while retaining
  ownership of AI scheduling, leases, runs, and budgets.

## First Implementation Program: Two-VPS Lumen Vertical Slice

This is the highest-value integration program after P0 and the required P1
single-host backup, restore, and daemon foundations. It uses the first
CLI-backed slice of P3. Full P2 fleet enrollment is optional for the proof;
hosts can be registered through the bootstrap transport while the agent
protocol matures.

### Objective

Prove that Ophelia can safely deploy, observe, roll back, back up, and recover a
real Lumen environment while Lumen presents the complete evidence chain.

### Environment

- one disposable VPS A as the active staging target
- one clean disposable VPS B as the recovery target
- an immutable Lumen image digest
- a sanitized Lumen manifest containing both its web authority and worker,
  using the proposed workload model
- encrypted off-site object storage
- Lumen provider integration using CLI transport first

### Scenario

1. Bootstrap and register both hosts through the available trusted transport.
2. Register capabilities and verify hardening posture.
3. Plan Lumen revision A for host A with zero runtime mutation.
4. Approve and activate revision A.
5. Plan and activate revision B using health-gated traffic switching.
6. Prove the web blue-green path and the worker's fenced, non-overlapping lease
   handoff independently.
7. Force a declared verification failure and prove automatic compensation.
8. Perform an explicit rollback to revision A and prove web and worker behavior.
9. Create an encrypted database and volume backup.
10. Destroy or quarantine the host-B target state.
11. Restore the backup on host B.
12. Activate the recovered application on a rehearsal route.
13. Verify application, worker, database, volumes, release metadata, and backup
    receipt.
14. Confirm every plan, Decision, Job, event, revision, verification, receipt,
    failure, and recovery step appears in Lumen.

### Required Evidence

- before-and-after hashes for protected desired, active, addon, Caddy, and
  runtime paths, plus an allowlist of staging and plan-journal writes
- immutable plan and artifact digests
- host-signed operation receipts
- Compose and Caddy observed state
- external verification responses using bounded assertions
- compensation and rollback timelines
- encrypted backup manifest and provider object references
- checksum and restore verification results
- measured RPO and RTO
- Lumen correlation chain and event replay after a simulated disconnect

### Exit Criteria

The program succeeds only if a clean reviewer can answer all of these from
machine-readable evidence:

- What was approved?
- Which exact artifact ran?
- Which host and revision were active at each point?
- What failed, and what compensation occurred?
- Did rollback change the real runtime and public behavior?
- Which data was backed up?
- Could it be restored without the original host?
- Did Lumen reconstruct the same truth after reconnecting?

## Test And Release Strategy

### Test Layers

| Layer | Purpose |
| --- | --- |
| Unit | Pure parsers, canonicalization, policies, state transitions, redaction, digests |
| Property and fuzz | Paths, domains, archive members, manifest structures, event decoding |
| Contract | CLI JSON, API schemas, agent protocol, events, receipts, Lumen provider |
| Component | SQLite leases/idempotency, Compose backend, Caddy switcher, backup provider |
| Integration | Real Docker, Caddy, PostgreSQL, Redis, networks, probes, logs |
| Recovery | Process kill, Docker restart, power-loss simulation, stale lease, corrupt state |
| Security | Traversal, injection, SSRF, replay, wrong scope, archive bomb, secret leakage |
| Live host | Disposable VPS bootstrap, deploy, rollback, backup, restore, upgrade |
| Compatibility | v1/v2 manifests, CLI/agent protocol versions, state schema migrations |

### P0 Acceptance Tests

- Adversarial manifests with traversal, newlines, invalid IDNA, unknown keys,
  unsupported versions, absolute paths, and symlink escapes fail before any
  renderer or filesystem mutation.
- Race tests swap symlinks between validation and mutation and prove
  descriptor-relative writes, extraction, and cleanup cannot escape managed
  roots.
- Local and remote plan leave protected desired, active, Caddy, addon, and
  runtime hashes unchanged; all allowed writes stay inside staging or the
  append-only plan journal, and no mutating backend is called.
- Every entrypoint refuses the same blocked policy.
- Deploy A, activate B, roll back to A, and verify A's externally visible
  behavior.
- Kill the executor after every phase boundary and recover to one verified
  active revision.
- Repeat an idempotency key with the same content and receive the same operation;
  reuse it with different content and receive an error.
- Run concurrent app, traffic, addon, and backup operations without duplicate
  allocation or corrupt state.
- Seed registered canary secrets and prove that Ophelia-controlled plans,
  receipts, events, diagnostics, and state redact them. Treat arbitrary
  application logs as sensitive even after best-effort registered-value
  redaction.

### Live Failure Matrix

The disposable-host lane should inject:

- image pull denied and network interrupted
- stale mutable tag
- startup timeout
- readiness failure
- Caddy candidate validation failure
- Caddy reload failure
- external verification failure
- Docker daemon restart
- `opheliad` termination
- disk full and inode exhaustion
- PostgreSQL unavailable
- Redis unavailable
- backup upload interruption
- corrupt backup object
- expired and revoked host certificate
- Lumen disconnect and event replay

Each case needs a deterministic expected state and an actionable diagnostic.

### CI Quality Gates

Add:

- all declared supported Python versions
- formatter and linter
- static type checking at typed boundaries
- coverage reporting and a ratcheted threshold
- dependency vulnerability audit
- secret scanning
- CodeQL or equivalent static analysis
- image and SBOM scanning for shared services
- docs and open-source audit
- real Docker integration lane
- scheduled disposable-VPS and fresh-host restore lane

### Performance Budgets

Track at minimum:

- plan latency by manifest size
- daemon idle and active memory
- operation queue and concurrency behavior
- state rebuild duration
- event delivery lag and replay throughput
- log throughput and query limits
- backup throughput and restore duration
- Caddy traffic-switch duration

Performance optimizations must not weaken plan binding, verification, or audit
truthfulness.

## Migration And Backward Compatibility

Avoid a big-bang rewrite.

### Recommended Sequence

1. Introduce strict validation in audit mode and enumerate current violations.
2. Add safe path, domain, identifier, and secure-write primitives.
3. Move remote planning to operation staging.
4. Create the canonical executor and shadow-plan existing fixture operations.
5. Route one low-risk mutation through the executor, then deploy, rollback,
   traffic, backup, and restore.
6. Add the revision layout alongside the current layout. Import the existing
   active release as a synthetic `legacy` revision.
7. Prove real rollback before migrating active production layouts.
8. Implement permissions migration that is idempotent, preserves ownership,
   rejects unsafe symlinks, and stops on ambiguity.
9. Introduce `opheliad` on a disposable host, then staging, then canary fleet
   batches.
10. Add the CLI-backed Lumen provider before changing transport.
11. Dual-read or translate existing Lumen deployment records during a bounded
    migration window.
12. Switch authority to provider operations and remove duplicate execution only
    after parity evidence exists.

### Break-Glass Path

`ship` must remain able to inspect local immutable revisions, state, operation
events, and receipts when Lumen is unavailable. A documented local recovery mode
may activate a previously verified revision after explicit high-risk approval.

Break-glass use must create a host-signed receipt and replay it to Lumen later.
It must not silently bypass policy or erase audit evidence.

The receipt records `authorization_kind: local_break_glass`, authenticated Unix
peer identity, host policy version, interactive ceremony evidence, reason, and
the exact plan digest. It never invents or reuses a Lumen Decision ID. Host
policy should restrict this path to recovery operations and previously verified
revisions unless a separate emergency capability is enabled.

### Ophelia Self-Upgrade

Agent upgrades need:

- signed or digest-locked artifact
- compatibility preflight
- state-schema backup
- staged binary or environment
- restart and health deadline
- automatic rollback to previous agent version
- protocol capability report after recovery

An agent unable to complete migration must continue serving the prior runtime
and expose a clear degraded state.

## Strategic Decisions

These defaults are recommended for the first implementation. Each should become
an architecture decision record when accepted.

| Decision | Recommended default | Revisit trigger |
| --- | --- | --- |
| Product trust model | Trusted single operator, defensive against compromised inputs | Customer-controlled workloads or delegated organizations become a real requirement |
| Fleet semantics | Independently managed hosts | Automatic rescheduling and cross-host replicas become required |
| Artifact model | CI-built immutable OCI images | Integrated builds create enough repeated value to justify a build service |
| Runtime backend | Docker Compose per host | Replica scheduling, overlay networking, or cluster service discovery is required |
| Docker privilege | Rootful supported with explicit risk; evaluate rootless profile | Rootless compatibility is proven across target hosts and workloads |
| Agent transport | Outbound persistent mutual-TLS stream to Lumen, plus a local Unix socket | A constrained network requires brokered polling or an approved WireGuard-only inbound profile |
| Host state | SQLite WAL for operation truth plus immutable revision files | Multi-writer host authority becomes necessary, which is not currently recommended |
| Event guarantee | At-least-once delivery with deduplication and per-host ordering | A real consumer requires stronger global ordering, which should be challenged |
| Secret ownership | Lumen references plus host-side provider or vault binding | A specific external vault becomes the organization-wide authority |
| Backup backend | Encrypted S3-compatible object storage with separate recovery key | Data locality, compliance, or provider constraints require another backend |
| DNS ownership | One declared owner per record or zone | Multi-provider traffic control becomes an explicit product requirement |
| Lumen relationship | Lumen is flagship cockpit; Ophelia core remains independently usable | Ophelia becomes a separately distributed product with other control planes |

The three decisions that most affect scope are:

1. whether Ophelia remains a personal trusted fleet or later hosts
   customer-supplied workloads
2. whether independent hosts are sufficient or cross-host scheduling is a real
   product requirement
3. whether application CI continues to build artifacts or Ophelia eventually
   owns source builds

The recommended answers are: personal fleet first, independent hosts first, and
artifact-first deployments.

## Success Metrics

### Runtime Trust

- percentage of deployments with mandatory external verification
- compensation success rate
- rollback success rate based on observed behavior
- number and age of uncompensated operations
- drift detection and resolution time
- percentage of production revisions using immutable digests

### Recovery

- backup success and freshness by data class
- restore-drill success and age
- measured RPO and RTO
- number of applications without a complete declared data contract
- backup corruption detected before target mutation

### Security

- secret-file permission compliance
- workloads using secure container and network defaults
- rejected traversal, replay, wrong-scope, and mutable-artifact attempts
- host certificate rotation and revocation health
- critical vulnerability and unsupported-version exposure

### Ergonomics

- time from new repository to first verified staging deployment
- number of manual host-specific steps per deployment
- percentage of operations available through typed JSON and Lumen
- time to identify active revision and last verified backup
- operator actions requiring raw SSH or unstructured shell use

### Fleet

- host enrollment and upgrade success
- heartbeat and event-delivery lag
- percentage of hosts with current capability and hardening reports
- rollout pause and rollback behavior under injected failure

## Immediate Backlog

If implementation begins now, the first ordered backlog should be:

1. Add strict slug, domain, route, version, unknown-key, and root-containment
   validation with adversarial tests.
2. Add secure atomic writes, permissions audit and migration, and secret-file
   ignore coverage.
3. Make remote planning use immutable staging and prove zero live mutation.
4. Fix runtime policy selection and make policy blockers block every mutation.
5. Define canonical `Operation`, `Plan`, `Revision`, `Event`, and `Receipt`
   domain models.
6. Introduce SQLite transactional idempotency, leases, and operation journal.
7. Build unique Compose revisions and candidate readiness.
8. Build atomic Caddy traffic switching and deterministic compensation.
9. Replace file-only rollback with live reactivation and verification.
10. Unify backup contracts and prove an encrypted restore onto a clean host.

Do not start fleet automation, a build service, or broad UI work before items 1
through 9 are proven through a real Docker integration lane.

## Evidence And References

### Active Ophelia Documentation

- [Documentation Index](../README.md)
- [Current Roadmap](../ROADMAP.md)
- [Architecture](../architecture.md)
- [Platform Handbook](../platform-handbook.md)
- [Manifest Specification](../manifest-spec.md)
- [Preflight And Safety](../preflight-and-safety.md)
- [Releases And Rollback](../releases-and-rollback.md)
- [Host Contract](../host-contract.md)
- [Host Inventory And Placement](../host-inventory-and-placement.md)
- [Job And Action API](../job-action-api.md)
- [Operator Console Adapter](../operator-console-adapter.md)
- [Production Hardening](../production-hardening.md)
- [Stateful App Migration Runbook](../stateful-app-migration-runbook.md)

### Prior Planning Context

These archived documents contain useful history but must not override verified
current source behavior:

- [Ophelia Next Architecture](../archive/planning/ophelia-next-architecture.md)
- [Strategic Implementation Roadmap](../archive/planning/ophelia-strategic-implementation-roadmap.md)
- [Product Improvement Findings](../archive/planning/product-improvement-findings.md)

### External Research

Accessed 2026-07-10 and 2026-07-11:

- [Lakebed documentation](https://docs.lakebed.dev/)
- [Lakebed npm package](https://www.npmjs.com/package/lakebed)
- [Coolify documentation](https://coolify.io/documentation)
- [Kamal deployment lifecycle](https://kamal-deploy.org/docs/commands/deploy/)
- [Dokku zero-downtime deploys](https://dokku.com/docs/deployment/zero-downtime-deploys/)
- [Docker Swarm mode](https://docs.docker.com/engine/swarm/)
- [Docker rootless mode](https://docs.docker.com/engine/security/rootless/)
- [Docker build best practices](https://docs.docker.com/build/building/best-practices/)

## Final Recommendation

Ophelia should stop growing sideways for one cycle and finish the vertical
execution path it already describes so well.

Build the trusted kernel first: strict boundaries, immutable revisions, one
executor, real readiness, atomic traffic, genuine rollback, durable operations,
and verified recovery. Then install that kernel as an independent agent on each
VPS. Then make Lumen the cohesive cockpit over the resulting runtime truth.

That creates a platform with a differentiated and valuable identity:

- sovereign because the operator owns the hosts and data
- dependable because success is proven at the runtime boundary
- ergonomic because applications share one contract and one golden path
- agent-native because inspection, plans, approvals, events, and receipts are
  structured from the beginning
- extensible because the runtime backend, provider, backup, and build layers
  have explicit seams

The two-VPS Lumen deployment and recovery program is the correct first proof. If
Ophelia can deploy a real Lumen revision, move traffic safely, roll it back,
restore its data onto a clean host, and reconstruct every step in Lumen, the
larger platform is no longer hypothetical. Its core has been demonstrated.
