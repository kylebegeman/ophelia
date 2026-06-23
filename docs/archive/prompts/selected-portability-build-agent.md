# Dedicated Agent Prompt: Selected Ophelia Portability Features

Use this prompt for an implementation agent that should work from the selected
feature scope.

```text
You are working in /path/to/ophelia.

Goal:
Implement the selected Ophelia portability foundation. Ophelia remains the VPS deploy/runtime substrate. Do not rename it. Do not mutate any VPS or clean up any production services. The selected scope is documented in docs/selected-portability-feature-roadmap.md.

Important reset:
This prompt supersedes docs/prompts/portable-runtime-build-agent.md and any earlier broad implementation prompt. Do not blindly continue the previous work plan. Start from the current worktree, audit what has already changed, and reconcile it with the selected scope below. Keep, modify, or replace existing in-progress implementation only when it serves the selected roadmap. Work that does not support the selected scope should be left alone unless it blocks the selected features or the user explicitly asks for cleanup.

Legacy platform note:
Private deployments on current hosts are not the public product model. Do not
design new Ophelia work around preserving one-off private layouts as target
foundations. Treat private host files, containers, and docs as legacy inventory
or future decommission targets only. Do not delete or modify them unless the
user explicitly approves a cleanup/decommission task.

Read first:
1. README.md
2. docs/ophelia-next-architecture.md
3. docs/portable-app-pack-spec.md
4. docs/stateful-app-migration-runbook.md
5. docs/selected-portability-feature-roadmap.md
6. docs/manifest-spec.md
7. docs/preflight-and-safety.md
8. docs/releases-and-rollback.md
9. docs/job-action-api.md

Before coding:
1. Run git status --short.
2. Review the current diff, including any in-progress files from prior agents.
3. Map existing changes to the selected features in docs/selected-portability-feature-roadmap.md.
4. Write a short implementation note for yourself that says which existing changes will be preserved, revised, or ignored.
5. Do not revert unrelated user or agent work without explicit permission.

Selected minor features:
- App Move Readiness Checklist
- Redacted Env Shape Diff
- Backup Freshness Receipt
- Route And Domain Conflict Scanner
- Portability Score
- Generated App Runbook
- Receipt Browser Commands
- Pack Init Scaffolder

Selected major features:
- Portable App Pack System
- Data Export And Import Pipeline
- Restore Drill System
- Cutover Orchestrator
- Per-App Isolation Refactor
- private operator UI Adapter Surface

Agent-friendly API requirements:
- Every command that may mutate state must have a dry-run/read-only plan form.
- Every command must support stable --json output before it is considered usable by LLM agents.
- Every plan should include schema_version, kind, operation, operation_id, blockers, warnings, checks, artifacts, confirmation_required, confirmation_token when needed, and exact_apply_input when applicable.
- Every receipt should include schema_version, kind, operation, operation_id, status, app, environment, timestamps, redaction status, artifacts, checks, and rollback notes.
- Never print raw env values, secrets, database URLs, tokens, private keys, or provider credentials.
- Prefer typed args over shell snippets. Hook execution must be allowlisted and bounded.

Implementation order:
1. Preserve backwards compatibility. Run tests before and after touching manifest parsing.
2. Implement or complete portable app pack model support for optional pack, host_requirements, data, and hooks sections.
3. Implement pack validate and pack explain with stable JSON output.
4. Implement redacted env shape diff as a reusable internal function, then expose it through a command or pack/readiness output.
5. Improve route/domain conflict scanning with JSON output that identifies owners and blockers.
6. Implement backup status/readiness using existing backup directories and manifests first. Do not run destructive restore.
7. Implement app readiness and portability score as aggregators over pack, env, routes, backups, releases, and restore drill receipts.
8. Implement receipt storage and browser commands.
9. Implement generated app runbook from the same data model used by readiness.
10. Implement pack init scaffolding in preview mode first, with --write as the explicit file-writing mode.
11. Implement export plan before export create. Start with metadata and runtime bundle planning, then add volume/archive and Postgres support behind confirmation tokens.
12. Keep import apply, restore drill apply, cutover apply, and per-app isolation changes behind explicit planning and tests. Do not rush mutating flows.
13. Expose action descriptors for private operator UI as stable JSON once commands exist.

Hard safety constraints:
- Do not SSH into source-host, target-host, or any other VPS for mutation.
- Do not delete containers, images, volumes, backups, apps, DNS records, env files, or runtime roots.
- Do not change production Caddy or DNS.
- Do not commit secrets.
- Do not weaken tests to make implementation pass.

Verification commands:
- PYTHON=python3 make docs-check
- PYTHON=python3 make compile
- PYTHON=python3 make test
- PYTHON=python3 make validate-examples
- git diff --check

Definition of done for an initial implementation pass:
- Existing manifests still validate and render.
- New pack/readiness/env/receipt APIs have stable JSON output.
- Mutating operations are either not implemented yet or require plan tokens.
- Tests cover backwards compatibility, selected feature contracts, redaction, blockers/warnings, and receipt shape.
- Final response lists changed files, tests run, and remaining risks.
```
