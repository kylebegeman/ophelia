# Preflight And Safety

Ophelia mutating operations are allowlisted and deterministic. The platform
does not provide arbitrary shell execution.

Dry-run-first flows:

- `ship deploy --plan` before production `--apply`
- `ship rollback plan` before rollback apply
- `ship backup plan` before backup create
- `ship restore plan` before restore preview apply
- `ship operations run <name> --dry-run` before confirming an operation template
- `ship gc plan` before runtime cleanup apply

Confirmation tokens are derived from the planned action and relevant inputs.
Changing the manifest, target release, backup id, or runtime files that affect
the plan changes the token.

Safety gates currently implemented:

- production deploy apply requires a matching token
- placeholder env values block apply
- image pulls fail the deploy instead of silently continuing
- Caddy validation runs before reload when an existing shared Caddy container
  is available
- restore apply writes a preview/report and does not overwrite active state
- runtime GC never deletes current releases, env/secrets, backups, or the
  configured rollback window
- operation templates list deterministic steps and persist an artifact report,
  but do not run autonomous workflows
- status and doctor report Docker warnings without mutating host state

Known limitations:

- live Postgres dump/restore execution is not enabled by default
- rollback is file-level and not traffic-aware yet
- callbacks are not enabled by default
