# Operator Console Adapter

The operator-console adapter is a read-only JSON contract for private or
internal operator UIs. Ophelia remains the deterministic executor: console data
never runs commands, plugins, workflows, provider calls, or mutating operations.

The command group and local API paths are historically named `lumen` for
compatibility. That name does not refer to a public dependency and no private UI
repository is required to use Ophelia.

## Command

```bash
./cli/ship lumen console-data --json
```

Fixture smoke:

```bash
make lumen-console-fixtures
```

Equivalent direct command:

```bash
./cli/ship lumen console-data \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --plugins-dir fixtures/app-suite/plugins \
  --json
```

## API

```text
GET /lumen/console-data
```

## Payload Shape

`kind: "ophelia.lumen.console"` includes:

- `navigation`: stable view ids and labels
- `overview.cards`: app, readiness, approval, plugin, and workflow summaries
- `apps`: bounded app rows with readiness, score, blocker codes, backup
  freshness, traffic status, observability status, and read-only primary actions
- `approval_queue`: metadata for confirmation-required commands and paused
  workflow nodes
- `workflows`: workflow templates and stored workflow summaries
- `plugins`: compact plugin inventory
- `quick_actions`: high-value read-only commands with examples
- `sources`: source report kinds and counts
- `safety`: non-mutation and redaction contract

## Safety Contract

Console data is read-only:

- no command execution
- no workflow execution
- no plugin execution
- no provider mutation
- no confirmation token accepted
- no raw finding bodies when compact summaries are enough
- no raw env, database URL, token, password, private key, or provider value

Approval queue entries are metadata only. Operators still approve by using the
underlying command's own plan/confirm/apply flow.

## Fixture Expectations

The fixture console run should report:

- eight apps
- one blocked app (`fixture-incomplete-app`)
- seven warning apps
- one fixture plugin
- a non-empty approval queue from mutating command descriptors
- no fake fixture secret values in JSON output
