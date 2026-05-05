# Releases And Rollback

Every `ship deploy` writes:

- `apps/<app>/release.json` as the current release pointer
- `apps/<app>/releases/<release-id>.json` as immutable release history
- `apps/<app>/release-bundles/<release-id>/` with generated Caddy, Compose,
  env template, and manifest lock files

Release records include manifest hash, rendered bundle hash, git SHA when
available, image references, image digests when present in the image reference,
runtime path, environment, source, deployed actor, verification result, and the
previous release id.

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
