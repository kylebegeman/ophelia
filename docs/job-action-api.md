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

Mutating commands require confirmation tokens from their matching plans:

- `ship deploy --apply --confirm <token>` for production manifests
- `ship rollback apply ... --confirm <token>`
- `ship backup create ... --confirm <token>`
- `ship restore apply ... --confirm <token>`

API endpoints:

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
