# Adoption Fixture Repos

These directories model future app repositories that already conform to the
Ophelia source-of-truth contract. They are synthetic fixtures, not production
apps, and are safe to use in tests and local validation.

Run:

```bash
make validate-adoption-fixtures
```

Each fixture keeps its `.ophelia.yml` in the repo root and includes the
recommended repo-local Ophelia artifacts under `ophelia/`.
