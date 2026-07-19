# Ophelia Host Daemon

Status: landed on `next`, pre-release

`opheliad` is Ophelia's durable single-host authority. It owns the operation
journal, runtime reconciliation, manifest v2 execution, scheduled workloads,
one-shot tasks, host controls, and the authenticated local API. Applications
continue serving and accepted operations remain recoverable when Lumen or the
operator CLI is unavailable.

The daemon is the default transport for manifest v2 planning and apply. Direct
CLI execution remains available only as an explicit local recovery path.

## Install And Start

On a Linux host with Python, Docker, and systemd, calculate the exact install
plan from a trusted Ophelia checkout:

```bash
ship daemon install --source-root /srv/ophelia --json
```

Review the source, unit, configuration, dependency observations, and
confirmation token. Apply the same plan as root:

```bash
sudo ship daemon install \
  --source-root /srv/ophelia \
  --apply \
  --confirm <confirmation-token> \
  --json
```

The installer creates a dedicated `ophelia` identity, installs an immutable
versioned Python environment beneath `/opt/ophelia/releases`, writes the
systemd unit and initial strict configuration, promotes the `current` link,
enables the service, and requires a successful systemd health check. An
incomplete version directory is quarantined before a replacement is staged and
atomically published.

The generated local socket is mode `0600` and owned by the daemon identity.
The initial configuration authorizes UID 0, so host operator commands use
`sudo`. Add another trusted UID explicitly only when that account should hold
root-equivalent Ophelia runtime authority.

## Configuration

The default file is `/etc/ophelia/agent.toml`:

```toml
runtime_root = "/var/lib/ophelia"
socket_path = "/run/ophelia/opheliad.sock"
allowed_uids = [0]
allowed_manifest_roots = ["/srv"]
max_request_bytes = 1048576
max_workers = 4
operation_poll_seconds = 0.5
reconciliation_seconds = 15
integrity_check_seconds = 300
scheduler_seconds = 15
require_edge_runtime = true
local_planning_enabled = true
local_apply_enabled = true
```

Unknown fields, unsafe ownership, group or world writable configuration,
invalid paths, invalid types, and out-of-range limits fail closed. Inspect the
effective redacted configuration without starting the service:

```bash
opheliad --config /etc/ophelia/agent.toml --check-config
```

## Normal Operator Flow

Plan through the daemon:

```bash
sudo ship manifest plan /srv/example/.ophelia.yml --json
```

Apply the exact reviewed plan. Acceptance is journaled before the API returns;
execution continues asynchronously:

```bash
sudo ship manifest apply <plan-id> --confirm <confirmation-token> --json
```

Inspect host state and events:

```bash
sudo ship daemon status --json
sudo ship daemon capabilities --json
sudo ship daemon events --cursor 0 --json
```

Run a task declared by the active revision:

```bash
sudo ship daemon run-task example production maintenance --json
```

Drain or place the host in maintenance mode:

```bash
sudo ship daemon drain --enable --json
sudo ship daemon maintenance --enable --json
```

Drain and maintenance refuse new deployments and task runs. Existing accepted
work remains durable and recoverable.

## Local Recovery

When the daemon is unavailable, an operator may use the same kernel directly:

```bash
sudo ship manifest plan /srv/example/.ophelia.yml \
  --direct \
  --runtime-root /var/lib/ophelia \
  --json

sudo ship manifest apply <plan-id> \
  --direct \
  --runtime-root /var/lib/ophelia \
  --confirm <confirmation-token> \
  --json
```

This is a recovery and compatibility lane, not a second executor. It uses the
same plan contract, operation journal, leases, backend, compensation, and
receipt logic as the daemon.

## Local API Contract

The daemon serves HTTP/JSON over its Unix socket. Every request requires:

- `X-Ophelia-Protocol-Version: 1`
- a bounded `X-Request-ID`
- authenticated Unix peer credentials
- an `Idempotency-Key` for every mutation

Primary resources are:

| Method | Resource | Purpose |
| --- | --- | --- |
| `GET` | `/v1/health` | Loop health and daemon identity |
| `GET` | `/v1/capabilities` | Protocol, workload, runtime, and limit negotiation |
| `GET` | `/v1/hosts/self` | Durable host state and controls |
| `GET` | `/v1/apps` | Active application revisions |
| `GET` | `/v1/apps/{app}/{environment}` | Active revision and recent task runs |
| `POST` | `/v1/plans` | Create an idempotent manifest v2 plan |
| `POST` | `/v1/operations` | Durably accept an approved plan |
| `GET` | `/v1/operations` | List journaled operations |
| `GET` | `/v1/operations/{id}` | Operation, events, and terminal receipt |
| `POST` | `/v1/operations/{id}/cancel` | Request durable cancellation |
| `GET` | `/v1/events` | Read the globally ordered host event stream |
| `POST` | `/v1/events/acknowledge` | Advance a consumer cursor monotonically |
| `POST` | `/v1/workload-runs` | Accept an active-revision task run |
| `GET` | `/v1/workload-runs` | List task and cron runs |
| `POST` | `/v1/workload-runs/{id}/cancel` | Request task cancellation |
| `POST` | `/v1/host/drain` | Change drain state |
| `POST` | `/v1/host/maintenance` | Change maintenance state |

Actor identity is always derived from the Unix peer UID. Caller-supplied actor
labels are not accepted. Events have one monotonic host cursor and retain their
operation or workload event digest for replay and integrity verification.

## Recovery And Service Health

At startup, daemon workers acquire fresh monotonic fences and resume every
nonterminal operation through the canonical executor. Workload runs are bound
to the exact retained active revision. Long-running runs renew their lease;
workers that lose a fence stop participating without overwriting the new
owner's result.

The reconciliation loop heartbeats host state, checks journal and event-chain
integrity, and signals the systemd watchdog. Loop failures make `/v1/health`
degraded while retaining only the error type and observation time. Raw process
output and exception text are not persisted in host health.

## Current Boundary

The Unix API is the completed single-host transport. Outbound enrollment,
mutual TLS, certificate rotation and revocation, disconnected command replay,
and staged self-upgrade are the next fleet milestone. Until that lands, Lumen
can use the local or bootstrap transport without changing the operation,
event, or receipt contracts.
