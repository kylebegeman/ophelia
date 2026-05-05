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
