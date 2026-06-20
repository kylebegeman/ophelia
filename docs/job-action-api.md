# Job / Action API Notes

The local HTTP Job/Action API foundation is implemented with `ship api serve`.
It binds to `127.0.0.1` by default and exposes JSON endpoints for actions,
jobs, job events, host inventory, and registries.

Quark can call these read-only commands today:

- `ship validate <manifest> --json`
- `ship explain <manifest> --json`
- `ship diff <manifest> --json`
- `ship deploy <manifest> --plan --json`
- `ship status --json`
- `ship doctor --json`
- `ship inspect conflicts --json`
- `ship drift <manifest> --json`
- `ship releases <app> --json`
- `ship pack validate <manifest> --json`
- `ship pack explain <manifest> --json`
- `ship pack init --app <app> --json` for preview-only scaffolding
- `ship env diff <app> --environment <env> --json`
- `ship backup status <app> --environment <env> --json`
- `ship app readiness <app> --environment <env> --json`
- `ship app runbook <app> --environment <env> --json`
- `ship app export plan <app> --environment <env> --json`
- `ship app import plan <bundle-or-metadata> --json`
- `ship app restore-drill plan <app> --environment <env> --json`
- `ship app cutover plan <app> --from <source> --to <target> --json`
- `ship app isolation plan <app> --environment <env> --json`
- `ship receipts list --json`
- `ship receipts show <receipt-id> --json`

Mutating commands require confirmation tokens from their matching plans:

- `ship deploy --apply --confirm <token>` for production manifests
- `ship rollback apply ... --confirm <token>`
- `ship backup create ... --confirm <token>`
- `ship restore apply ... --confirm <token>`

Export create, import apply, restore drill apply, cutover apply, and isolation
apply are not exposed as mutating action descriptors in this planning pass.

API endpoints:

- `GET /health`
- `GET /actions`
- `POST /jobs`
- `GET /jobs/<job_id>`
- `GET /jobs/<job_id>/events`
- `POST /jobs/<job_id>/cancel`
- `GET /host/inventory`
- `GET /registry/manifests`
- `GET /registry/releases`
- `GET /operations`
- `POST /operations/run`

There is no arbitrary shell endpoint. Jobs use typed schemas, idempotency keys,
job state, audit records, local-only binding, and optional artifact link fields.
Operation templates return explicit step lists and result artifacts; they do
not execute autonomous deploy workflows.

Mutating API jobs are stricter than direct staging CLI applies: `deploy.apply`,
`deploy.rollback.apply`, `backup.create`, and `restore.apply` must first run
with `dry_run: true`. The dry-run stores a confirmation record under the runtime
root, returns `required_confirmation_token`, `confirmation_expires_at`, and
`exact_apply_input`, and the apply job must submit the matching token before it
expires. Successful apply consumes the token.

Completion callbacks are disabled by default. When enabled in
`config/ophelia-actions.json`, Ophelia posts the completed job JSON to
`completion_callback_url` and signs the payload with `X-Ophelia-Signature:
sha256=<hmac>` when `OPHELIA_CALLBACK_SECRET` is set.
