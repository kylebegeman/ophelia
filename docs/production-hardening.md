# Production Hardening Report

The production hardening report is a read-only go/no-go aggregate for moving
from fixture-backed contract work toward real staging and production use. It
does not execute workflow nodes, plugin code, provider mutations, shell
commands, HTTP probes, Docker probes, state refresh, or apply/create commands.

## Command

```bash
./cli/ship hardening production-readiness --json
```

Fixture smoke drill:

```bash
make production-hardening-fixtures
```

For repeatable multi-scenario fixture checks, use live drill profiles:

```bash
make live-drills-fixtures
```

Equivalent direct fixture command:

```bash
./cli/ship hardening production-readiness \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --plugins-dir fixtures/app-suite/plugins \
  --include-fixture-suite \
  --allow-blocked-live-readiness \
  --json
```

`--allow-blocked-live-readiness` is useful for expected-failure drills. The
fixture suite intentionally includes `fixture-incomplete-app`, so the child
live-readiness report is blocked while the hardening report returns `review`
instead of `no_go`.

## API

The local API exposes the same report:

```text
GET /hardening/production-readiness
```

The API route uses the configured runtime root and repo-local manifests and
plugins. It is read-only and suitable for private operator UI discovery.

## Payload

The report emits:

```json
{
  "kind": "ophelia.production_hardening_report",
  "operation": "production.hardening",
  "status": "ok | warning | blocked",
  "go_no_go": "go | review | no_go",
  "read_only": true,
  "mutates_state": false,
  "confirmation_required": false
}
```

The aggregate checks:

- live-readiness status
- operator-console payload shape
- plugin inventory validation
- state service status
- workflow template availability
- command catalog safety for mutating descriptors
- optional fixture app suite expected-state drill

All child reports are compacted before inclusion. The final payload is passed
through propagated `deep_redact`, so command output remains safe even when child
reports include runtime-derived data.

When a real live-readiness child report is blocked, use
`ship live-hydration report` for one focused app before enabling probes or
production rehearsals.

## Go/No-Go Semantics

- `go`: no blockers or warnings.
- `review`: no blockers, but at least one warning or explicitly allowed blocked
  child report exists.
- `no_go`: one or more blockers exist.

CLI exit codes follow `status`: blocked reports exit non-zero; `ok` and
`warning` reports exit zero.

## Safety Boundary

This report is a hardening gate, not a production migration executor. Live
provider mutations, authenticated external provider probes, real vault APIs,
and production apply/rehearsal workflows should be added behind the existing
plan, confirmation-token, and receipt contracts in later phases.
