# Reviewed Hydration Evidence: fixture-postgres-api/staging

This directory is a committed, synthetic reviewed evidence kit for exercising
the live hydration validation, promotion-plan, and probe-gate flow without
touching live runtime state.

All values are fake. Do not copy these files into a real runtime root.

Contents:

- `env.required.template`: reviewed env shape only, no values.
- `github-secret-observation.template.json`: observed secret names only.
- `release-metadata.template.json`: synthetic active release metadata.
- `host-capabilities.template.yml`: synthetic host capability facts.

Expected behavior:

- `ship live-hydration validate-evidence` returns `ok`.
- `ship live-hydration promotion-plan` returns `warning` because the focused
  fixture hydration report still has drift review work.
- `ship live-hydration probe-gate` returns `review` and emits explicit probe
  commands without running them.
