# Job / Action API Notes

The local HTTP Job/Action API is not implemented yet. The current integration
surface is the structured `ship` command set with stable JSON output.

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

There is no arbitrary shell endpoint. A later API should wrap this command
catalog with typed schemas, idempotency keys, job state, audit records,
local-only binding, and optional callbacks.
