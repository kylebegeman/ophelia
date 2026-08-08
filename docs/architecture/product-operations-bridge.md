# Product Operations Bundle Bridge

Status: landed vertical slice

Ophelia can independently consume the brand-neutral `product.*` operations
bundle produced by Forge. Forge is a producer, not an Ophelia dependency. The
wire boundary consists of these versioned contracts:

- `product.runtime-requirements/v1`
- `product.release-manifest/v1`
- `product.recovery-contract/v1`
- `product.operations-bundle/v1`

Ophelia rejects duplicate JSON keys, unknown fields, unsafe paths, oversized
documents, inconsistent cross-document identities, invalid canonical contract
digests, and document byte-digest mismatches. Executable size, SHA-256 digest,
operating system, and architecture are checked before execution. Contract
parsing never loads secret values. Contracts and artifacts are read through
stable regular-file handles that reject symlinks. Ophelia validates both the
staged copy and the final immutable revision copy, so changing source bytes
between planning, staging, and materialization fails before a process starts.

## Operator workflow

Validate a bundle and, optionally, its artifact bytes:

```sh
ship product validate .product/operations \
  --artifact ./dist/product-server \
  --json
```

Plan and apply a release:

```sh
ship product release plan .product/operations \
  --artifact ./dist/product-server \
  --env-file /etc/ophelia/example.env \
  --evidence expand-contract-compatible=/srv/evidence/schema-review.json \
  --json

ship product release apply .product/operations \
  --artifact ./dist/product-server \
  --env-file /etc/ophelia/example.env \
  --confirm <token> \
  --json
```

The environment file must be a real regular file with mode `0600` or stricter.
It is opened through a no-follow file descriptor and revalidated after its
bounded read. Plans and receipts retain configuration names only. Values remain
in process memory and are passed directly to the product process. The
confirmation token is a host-keyed HMAC over the complete secret-bearing
configuration digest, active revision generation, artifact, contracts, and
reviewed evidence. Changing a value after planning invalidates the token without
placing that value or its raw digest in a plan, receipt, or runtime file.

Roll back by planning the retained target bundle and artifact against the
currently active revision:

```sh
ship product rollback plan .product/operations \
  --artifact ./dist/previous-product-server \
  --env-file /etc/ophelia/example.env \
  --json

ship product rollback apply .product/operations \
  --artifact ./dist/previous-product-server \
  --env-file /etc/ophelia/example.env \
  --confirm <token> \
  --json
```

The active release's rollback policy governs the operation. A target cannot
weaken a running release that requires coordinated data restoration, and a
rollback without an active predecessor is blocked. Re-deploying a release after
it was rolled back receives a new active-generation identity instead of
colliding with its earlier successful operation.

Create a recovery-contract backup. Each provider-selected dataset must be bound
to a local snapshot or mounted provider export for this v1 backend:

```sh
ship product backup plan .product/operations \
  --backup-id backup-2026-07-14 \
  --dataset storage.object.private=/srv/example/objects \
  --env-file /etc/ophelia/example.env \
  --json

ship product backup apply .product/operations \
  --backup-id backup-2026-07-14 \
  --dataset storage.object.private=/srv/example/objects \
  --env-file /etc/ophelia/example.env \
  --confirm <token> \
  --json
```

SQLite datasets use the SQLite backup API after the application process has
quiesced. Existing database files are opened read/write without create access
so SQLite can recover or checkpoint WAL sidecars before copying. Backup copies
receive the same existing-file access for their integrity check because a
WAL-mode database can require SQLite to initialize fresh sidecars on its first
open. Filesystem datasets reject symlinks and special files. The process set is
restored on its retained revision and must pass its public health probe before
backup success is recorded. Provider-snapshot datasets do not stop a healthy
application process.

Run an isolated restore drill:

```sh
ship product restore-drill plan .product/operations \
  --backup-id backup-2026-07-14 \
  --drill-id drill-2026-07-14 \
  --env-file /etc/ophelia/example.env \
  --json

ship product restore-drill apply .product/operations \
  --backup-id backup-2026-07-14 \
  --drill-id drill-2026-07-14 \
  --env-file /etc/ophelia/example.env \
  --confirm <token> \
  --json
```

The drill restores into separate data and runtime roots, verifies dataset
digests, runs SQLite integrity checks where declared, boots the exact retained
artifact, executes the declared health probe, stops the isolated process set,
and leaves the active runtime untouched. Filesystem and local object-storage
snapshots are rebound through an isolated local provider. A restore plan accepts
only a backup whose exact manifest and dataset bytes reconcile with a successful
kernel terminal receipt and correlated product receipt. Its confirmation binds
the exact backup manifest and every dataset digest, so replacing an otherwise
valid snapshot invalidates the approval. PostgreSQL uses
`pg_dump`, `pg_restore`, and `psql`; its target database name must end with
`_ophelia_drill_<drill_id>` so the production database cannot be selected by
mistake. Applied Goose migration state is verified before the application is
booted against the restored database.

## Execution model

The v1 process backend accepts one executable process declaration, one to 32
replicas, one HTTP port, and one or more HTTP health probes. Unsupported runtime
shapes fail closed during preflight. The process argv must contain the declared
port as a standalone token or host-and-port token so Ophelia can replace it
with a unique loopback candidate port for every replica.

Ophelia keeps the product's declared port stable by running a small TCP switch.
Each revision runs on an isolated set of loopback ports. Activation atomically
replaces an `active.json` upstream set read for every new connection. New
connections are distributed across the complete healthy replica set. This
provides the full kernel sequence:

1. validate and preflight exact bundle and artifact bytes;
2. materialize an immutable revision;
3. start the candidate on an isolated port;
4. verify candidate health;
5. atomically switch new connections;
6. verify health through the public port;
7. stop the predecessor;
8. commit active revision truth and a terminal receipt.

If verification fails after the switch, the executor restores the exact prior
pointer before removing the candidate. If a later commit failure occurs after
predecessor drain, compensation reconstructs and restarts the content-addressed
retained revision before restoring traffic. Plans require configuration for the
target and active predecessor so that recovery cannot discover a missing secret
after a side effect. A later rollback uses the same retained-revision path,
verifies it, and drains the displaced revision.

Forward-only-at-startup migrations are executed by the exact product artifact.
Ophelia binds their declared identities into release evidence, starts replicas
sequentially so the first healthy instance completes migrations before peers
join, and still requires the release's backup precondition for upgrades.
Initial releases do not require a backup of nonexistent state. Expand-contract
compatibility and any custom precondition must be supplied as a bounded regular
evidence file; its digest, never its contents or path, is bound into the plan.
An upgrade's `backup-complete` precondition is satisfied only by a backup of the
exact active revision whose required datasets, bytes, manifest, successful
terminal receipt, and correlated receipt all reconcile. A manifest alone is
never sufficient evidence.

Release, rollback, backup, and restore operations all acquire the same durable
host, app, and environment execution fence. Backup quiescence cannot race a
deployment or rollback. Long backups and restore drills heartbeat that lease.
An interrupted recovery command can be replayed with the same confirmation;
published manifests and reports are validated and reconciled before work is
repeated. Runtime traffic pointers must also reconcile with journal active
revision truth before a new plan is issued.

## Evidence and correlation

Product receipts bind all of the following:

- operations bundle digest;
- composition digest;
- runtime, release, and recovery canonical contract digests;
- exact readable document byte digests;
- release and artifact identity;
- explicit Ophelia request, plan, approval, operation, verification, and
  terminal receipt digests;
- backup manifest or restore report digest when applicable.

Receipt payloads contain no environment values or application output. Backend
verification prose is discarded before journal persistence.

## Linklet fixture

The integration test loads Forge's committed SQLite, PostgreSQL, and React
Linklet bundles directly from their `.product/operations` directories and
verifies them against `fixtures/compatibility/forge-products.json`, including
the reviewed Forge history boundary, release identities, composition digests,
and bundle digests. The shared checkout must descend from the reviewed commit,
while every operations bundle must still match its exact reviewed content
identity. This permits documentation-only and unrelated descendant commits
without weakening the wire-contract proof. Updating that compatibility lock is
an explicit interoperability review, not a side effect of running tests. This
checks facets, shared-provider profiles, multi-replica declarations, and the
shared wire contract without importing Forge packages. Executable lifecycle
coverage uses WAL-mode synthetic SQLite artifacts for replica balancing,
startup migrations, release evidence, local object recovery, PostgreSQL
provider commands, upgrade, isolated restore, and rollback. When a shared Forge
artifact is present, the compatibility test also verifies its bytes and plans
any host-compatible profile. Forge's own clean-host lane remains the
authoritative build-and-execute proof for the produced artifact.

## Current v1 boundaries

- Multi-process, worker, cron, TCP-only, and HTTPS-terminated runtime shapes
  remain blocked until their backend protocols land.
- Provider-selected backup inputs outside PostgreSQL must be exposed as local
  files or directories. The current local adapter treats all object-provider
  dataset identities as views of one aggregate provider snapshot, requires
  every binding and captured digest to identify that same snapshot, and rebinds
  the aggregate through one isolated local root. Independent per-dataset
  providers and remote object-store snapshot APIs remain provider-adapter work.
- Unknown release preconditions require explicit reviewed evidence. Ophelia
  does not infer compatibility from migration names.
- The TCP switch is host-local. Distributed routing and multi-host placement
  remain separate control-plane work.
- Restore-required rollback still requires an explicit coordinated restore;
  artifact-only rollback remains the directly executable rollback profile.
