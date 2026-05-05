# Operator Runbook

Preflight a manifest:

```bash
./cli/ship validate manifests/quark-ops-staging.ophelia.yml
./cli/ship explain manifests/quark-ops-staging.ophelia.yml
./cli/ship deploy manifests/quark-ops-staging.ophelia.yml --plan
./cli/ship diff manifests/quark-ops-staging.ophelia.yml
./cli/ship inspect conflicts
```

Inspect runtime:

```bash
./cli/ship status
./cli/ship doctor
./cli/ship drift all
./cli/ship releases quark-ops-staging
```

Deploy staging:

```bash
./cli/ship deploy manifests/quark-ops-staging.ophelia.yml --plan
./cli/ship deploy manifests/quark-ops-staging.ophelia.yml --apply
```

Deploy production:

```bash
./cli/ship deploy manifests/quark-ops.ophelia.yml --plan --json
./cli/ship deploy manifests/quark-ops.ophelia.yml --apply --confirm <token>
```

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
