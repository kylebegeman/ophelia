# Releases And Rollback

Every `ship deploy` writes:

- `apps/<app>/release.json` as the current release pointer
- `apps/<app>/releases/<release-id>.json` as immutable release history
- `apps/<app>/release-bundles/<release-id>/` with generated Caddy, Compose,
  env template, and manifest lock files

Release records include manifest hash, rendered bundle hash, git SHA when
available, image references, image digests when present in the image reference,
runtime path, environment, source, deployed actor, apply result, verification
result, and the previous release id.

Apply and verification are tracked separately:

- `applied`: true only after the runtime bundle is activated, containers are
  started or confirmed healthy, and Caddy validation/reload succeeds when shared
  Caddy is present
- `verified`: true only after configured or inferred external verification
  checks pass
- `apply.phase`: the failed apply phase when apply cannot complete, such as
  `image_pull`, `container_health`, `caddy_validate`, or `caddy_reload`
- `verification.phase`: `certificate_obtain` when HTTPS/TLS is not ready, or
  `external_route_verify` when the public HTTP check fails after TLS is ready

If a release is applied but not verified, rerun the check with:

```bash
./cli/ship verify <app>
```

Commands:

```bash
./cli/ship releases <app>
./cli/ship release show <app> <release-id>
./cli/ship rollback plan <app> <release-id>
./cli/ship rollback apply <app> <release-id> --confirm <token>
```

Rollback is file-level today. It restores allowlisted generated files from the
target release bundle and updates shared Caddy snippets when present. It never
deletes current runtime state, env files, volumes, backups, or static assets.
Each apply writes a rollback report under `apps/<app>/rollback-reports/`.

Rollback plans now include:

- `post_apply_verification`: verification checks read from the target
  `manifest.lock.json`, plus the recommended `ship verify` command
- `traffic_switching`: explicit metadata that rollback is a generated-file
  restore, not a traffic-aware rollout
- `changes`: generated files that will be restored and whether each currently
  differs

Rollback apply reports preserve those fields and mark verification as
`not_run`; Ophelia reports the check list but does not perform live HTTP
verification unless the operator runs `ship verify`.

Backup and restore preview reports include:

- `coverage`: app env, rendered config, release metadata, static assets, and
  Postgres metadata intent
- `restore_preview_supported: true`
- `destructive_restore_supported: false`
- `active_runtime_modified_on_apply: false` for restore previews

Secret values are copied into backup artifacts when backing up runtime `env`,
but report JSON never prints the secret values.
