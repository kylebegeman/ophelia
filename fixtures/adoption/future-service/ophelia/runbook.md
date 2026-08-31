# Future Service Runbook

Use `ship app adoption plan future-service --repo-path fixtures/adoption/future-service --environment staging --json` before runtime or provider setup.

## Checks

- Validate the pack with `ship pack validate .ophelia.yml --json`.
- Run readiness only after truthful runtime state exists.
