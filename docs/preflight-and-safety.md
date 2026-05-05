# Preflight And Safety

Ophelia mutating operations are allowlisted and deterministic. The platform
does not provide arbitrary shell execution.

Dry-run-first flows:

- `ship deploy --plan` before production `--apply`
- `ship rollback plan` before rollback apply
- `ship backup plan` before backup create
- `ship restore plan` before restore preview apply

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
- status and doctor report Docker warnings without mutating host state

Known limitations:

- preflight is still composed from individual commands, not a single command
- live Postgres dump/restore execution is not enabled by default
- rollback is file-level and not traffic-aware yet
