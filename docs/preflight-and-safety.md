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

For Job/Action API applies, the token is also persisted as an expiring
confirmation record tied to action id plus input hash. API applies fail when the
token is missing, wrong, expired, already consumed, or not issued by a prior
dry-run job.

Safety gates currently implemented:

- production deploy apply requires a matching token
- API mutating applies require a prior dry-run token for staging and production
- placeholder env values block apply
- image pulls fail the deploy instead of silently continuing
- Caddy validation runs before reload when an existing shared Caddy container
  is available
- restore apply writes a preview/report and does not overwrite active state
- runtime GC never deletes current releases, env/secrets, backups, or the
  configured rollback window
- traffic apply records provider intent and checkpoint receipts by default
- target health verification is read-only, requires an explicit
  `--target-health-url` and `--run-target-health`, rejects URLs with
  credentials, query strings, or fragments, and is covered by the matching
  confirmation token
- provider-backed traffic writes require the matching plan token,
  `--execute-provider-mutation`, `--provider-config`, non-manual providers, and
  provider config entries with `allow_mutation: true`
- implemented provider backends are file-backed DNS/Caddy staging and
  Cloudflare DNS
- Cloudflare DNS uses `api_token_env`; token values are not stored in plans or
  receipts, apply never deletes DNS records, and TTL must be automatic `1` or
  between `30` and `86400` seconds
- live Caddy reload through the file provider requires `reload: true`,
  `allow_reload: true`, `sites_dir` matching `<runtime_root>/caddy/sites.d`,
  the matching plan token, and the normal provider execution gates
- traffic rollback apply requires a rollback plan token and restores only
  previous provider state captured by a traffic apply receipt; it blocks
  rollback cases that would require deleting previously absent records/files
- operation templates list deterministic steps and persist an artifact report,
  but do not run autonomous workflows
- status and doctor report Docker warnings without mutating host state

Known limitations:

- live Postgres dump/restore execution is not enabled by default
- rollback is file-level for releases and receipt-backed for traffic providers;
  Cloudflare rollback restores previous records with PATCH and blocks
  deletion-only rollback cases
- callbacks are not enabled by default
