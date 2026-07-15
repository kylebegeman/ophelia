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
parsing never loads secret values.

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
  --json

ship product release apply .product/operations \
  --artifact ./dist/product-server \
  --env-file /etc/ophelia/example.env \
  --confirm <token> \
  --json
```

The environment file must be a real regular file with mode `0600` or stricter.
Plans and receipts retain configuration names only. Values remain in process
memory and are passed directly to the product process.

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
quiesced. Filesystem datasets reject symlinks and special files. The process is
restarted on the same isolated candidate port and must pass its public health
probe before backup success is recorded.

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

For stack-owned datasets, the drill restores into a separate data and runtime
root, verifies dataset digests, runs SQLite integrity checks where declared,
boots the exact retained artifact, executes the declared health probe, stops
the isolated process, and leaves the active runtime untouched. A drill with a
provider-selected dataset remains blocked because copying provider bytes does
not prove that the application was rebound to an isolated provider. That path
requires a native provider restore adapter.

## Execution model

The v1 process backend accepts one executable process, one replica, one HTTP
port, and one or more HTTP health probes. Unsupported runtime shapes fail
closed during preflight. The process argv must contain the declared port as a
standalone token or host-and-port token so Ophelia can replace it with a unique
loopback candidate port.

Ophelia keeps the product's declared port stable by running a small TCP switch.
Each revision runs on an isolated loopback port. Activation atomically replaces
an `active.json` pointer read for every new connection. This provides the full
kernel sequence:

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
- Ophelia plan, approval, operation, verification, and terminal receipt digest;
- backup manifest or restore report digest when applicable.

Receipt payloads contain no environment values or application output. Backend
verification prose is discarded before journal persistence.

## Linklet fixture

The integration test loads Forge's committed Linklet bundle directly from
`examples/linklet/.product/operations` and verifies its current bundle digest.
This checks the shared wire contract without importing Forge packages. The
committed Linklet artifact is Linux amd64, so Ophelia validates its bytes on a
Darwin arm64 development host but correctly blocks local execution. Executable
lifecycle coverage uses a synthetic executable fixture with SQLite, HTTP
health, upgrade, backup, isolated restore, and rollback.

## Current v1 boundaries

- Multi-process, worker, cron, TCP-only, HTTPS-terminated, and multi-replica
  runtime shapes remain blocked until their backend protocols land.
- Provider-selected backup inputs must currently be exposed as local files or
  directories. Provider-selected restore drills fail closed until native
  snapshot restore and isolated application-binding adapters land.
- Release preconditions outside artifact digest verification, configuration
  validation, and health probing remain blocked because this backend cannot
  produce truthful evidence for them.
- The TCP switch is host-local. Distributed routing and multi-host placement
  remain separate control-plane work.
- Forward-only migrations are represented in the release contract but are not
  executed by this backend. A release containing migrations should remain
  blocked until migration execution and restore-required rollback are added.
