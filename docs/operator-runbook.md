# Operator Runbook

Preflight a manifest:

```bash
MANIFEST=path/to/app.ophelia.yml
APP=<app-name>

./cli/ship validate "$MANIFEST"
./cli/ship explain "$MANIFEST"
./cli/ship deploy "$MANIFEST" --plan
./cli/ship diff "$MANIFEST"
./cli/ship inspect conflicts
```

Inspect runtime:

```bash
./cli/ship status
./cli/ship doctor
./cli/ship drift all
./cli/ship releases "$APP"
```

Deploy staging:

```bash
./cli/ship deploy "$MANIFEST" --plan
./cli/ship deploy "$MANIFEST" --apply
```

Deploy production:

```bash
./cli/ship deploy "$MANIFEST" --plan --json
./cli/ship deploy "$MANIFEST" --apply --confirm <token>
```

Product-specific deploy wrappers belong in separately approved adoption or
migration docs. The generic `ship` flow is the Ophelia contract.

Backup and restore preview:

```bash
./cli/ship backup plan <app>
./cli/ship backup create <app> --confirm <token>
./cli/ship restore plan <app> <backup-id>
./cli/ship restore apply <app> <backup-id> --confirm <token>
```

Operation templates and cleanup:

```bash
./cli/ship operations list
./cli/ship operations run deploy-with-preflight --dry-run
./cli/ship operations run deploy-with-preflight --confirm <token>
./cli/ship gc plan
./cli/ship gc apply --confirm <token>
```

Operator notes:

```bash
./cli/ship notes add --release <release-id> "rolled back because health check failed"
./cli/ship notes list --release <release-id>
./cli/ship notes add --job <job-id> "manual env fix applied"
```

Local API smoke:

```bash
./cli/ship api serve --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/actions
```

For mutating API jobs, run the job once with `dry_run: true`, then submit the
returned `exact_apply_input` only after the operator explicitly confirms.
