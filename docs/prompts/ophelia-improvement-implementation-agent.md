# Dedicated Agent Prompt: Ophelia Improvement Roadmap

Use this prompt for an implementation agent that should build the current
selected Ophelia product-improvement roadmap.

```text
You are working in /Users/kyle/Developer/platforms/ophelia.

Goal:
Implement the selected Ophelia strategic roadmap documented in docs/ophelia-strategic-implementation-roadmap.md. Ophelia remains the VPS deploy/runtime substrate. Do not rename it. Do not mutate production VPS state, delete services, change production DNS/Caddy, or print secrets.

Read first:
1. README.md
2. docs/product-improvement-findings.md
3. docs/ophelia-strategic-implementation-roadmap.md
4. docs/ophelia-improvement-execution-plan.md
5. docs/changelog/README.md
6. docs/changelog/TEMPLATE.md
7. docs/job-action-api.md
8. docs/portable-app-pack-spec.md
9. docs/preflight-and-safety.md
10. docs/lumen-vps-ophelia-2-handoff.md

Before coding:
1. Run git status --short --branch.
2. Review the current diff and preserve unrelated user or agent work.
3. Start at the next incomplete phase of docs/ophelia-strategic-implementation-roadmap.md and move in dependency order.
4. For each logical change, add docs/changelog/NNNN-short-slug.md and update docs/changelog/INDEX.md.

Implementation rules:
- Every agent-facing command must support stable --json output.
- Every mutating operation must have a dry-run/read-only plan form, confirmation token, exact apply input, and JSON receipt.
- Never print raw env values, secrets, database URLs, tokens, private keys, or provider credentials.
- Prefer shared models in src/ophelia/operation_schema.py, src/ophelia/actions.py, and src/ophelia/api.py over duplicate command-specific contracts.
- Keep Lumen integration as a thin adapter over Ophelia's command/action/state/receipt models.
- Run focused tests after each phase and the full verification set before handing back:
  PYTHON=python3 make docs-check
  PYTHON=python3 make compile
  PYTHON=python3 make test
  PYTHON=python3 make validate-examples
  git diff --check

Definition of done:
The selected phase has documented commands or API surfaces, stable JSON contracts, tests, safety gates, Lumen-consumable descriptors, and change records. Final response must list changed files, verification run, and remaining risks.
```
