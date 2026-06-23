# Dedicated Agent Prompt: Ophelia Portable Runtime Build

Use this prompt for a dedicated implementation agent.

```text
You are working in /path/to/ophelia.

Goal:
Build the next Ophelia portability foundation. Ophelia remains the deploy/runtime substrate for Kyle's VPS platform. Do not rename it. Do not mutate any VPS or delete/clean up any services. The immediate implementation target is to make app movement between hosts a first-class, dry-run-first Ophelia workflow with explicit data contracts, export/import planning, and receipts.

Read these files first, in this order:
1. README.md
2. docs/architecture.md
3. docs/manifest-spec.md
4. docs/releases-and-rollback.md
5. docs/preflight-and-safety.md
6. docs/host-contract.md
7. docs/ophelia-next-architecture.md
8. docs/portable-app-pack-spec.md
9. docs/stateful-app-migration-runbook.md

Core constraints:
- Do not touch production VPS state.
- Do not SSH into a host for mutation.
- Do not delete containers, images, volumes, backups, apps, DNS records, or env files.
- Do not print or commit secrets.
- Preserve backwards compatibility with existing manifests.
- Keep all mutating operations dry-run-first with confirmation tokens.
- Prefer JSON receipts for anything that private operator UI may consume later.
- Add focused tests for manifest parsing, validation, plan generation, and receipt shape.

Implementation order:
1. Add manifest model support for optional pack, host_requirements, data, and hooks sections. Existing manifests must parse exactly as before.
2. Add pack validation/explain commands, for example:
   - ./cli/ship pack validate <manifest>
   - ./cli/ship pack explain <manifest> --json
3. Infer a default data contract from addons.postgres and addons.redis for backwards compatibility, but warn that inferred contracts are not sufficient for critical production movement.
4. Add read-only export planning:
   - ./cli/ship app export plan <app> [--environment production] [--json]
   The plan should inspect runtime files only. It should not run pg_dump yet unless explicitly implemented as a confirmed create command.
5. Add export receipt types and bundle shape constants matching docs/portable-app-pack-spec.md.
6. Add import planning from an export bundle or exported metadata. Keep import apply out of scope until plan and validation are solid.
7. Add docs updates and examples, especially an example Demo Service pack shape.

Suggested first milestone:
- pack fields parse and round-trip into manifest.lock.json
- pack validate/explain works for existing examples
- Demo Service example can declare critical data without breaking render/deploy
- docs-check, compile, and tests pass

Verification commands:
- PYTHON=python3 make docs-check
- PYTHON=python3 make compile
- PYTHON=python3 make test
- PYTHON=python3 make validate-examples

Definition of done for this agent run:
- No VPS state was mutated.
- No cleanup was performed.
- Existing example manifests still validate.
- New manifest fields are documented and tested.
- New commands are dry-run/read-only unless a confirmation token is explicitly required.
- Final response lists changed files, tests run, and any remaining risks.
```
