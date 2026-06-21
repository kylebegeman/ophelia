# Selected Portability Implementation Note

Working decision after reading `docs/selected-portability-feature-roadmap.md`:

- Preserve the in-progress manifest model support for `pack`,
  `host_requirements`, `data`, and `hooks`; it matches the selected Portable App
  Pack System and preserves old manifests through inferred addon contracts.
- Preserve and revise `ship pack validate|explain`; keep the validation logic,
  but align JSON output with the selected versioned agent envelopes.
- Preserve and revise `ship app export plan` and `ship app import plan`; keep
  them read-only, but make plan output schema-stable before adding any create or
  apply behavior.
- Add the selected minor features before deeper mutation: redacted env shape
  diff, backup status, route conflict ownership, app readiness and score,
  receipt browser, generated runbook, and preview-first pack init.
- Ignore older broad prompt scope unless it supports the selected roadmap. Do
  not implement production import apply, restore drill apply, cutover apply, or
  per-app isolation mutation in this pass.
