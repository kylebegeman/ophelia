# Ophelia 0.3.0 Upgrade Prompt

Copy and paste this prompt into an agent or hand it to an engineer who is
already operating an Ophelia checkout.

````text
You are upgrading an existing Ophelia installation or app repository to Ophelia 0.3.0.

Context:
- Repository: https://github.com/mrbagels/ophelia
- Distribution: GitHub only. Do not use PyPI.
- License: Apache-2.0 with DCO sign-off for contributions.
- Primary CLI: ship
- Runtime state must remain outside the source checkout, normally under ~/ophelia-runtime or the configured runtime root.

Objectives:
1. Update the Ophelia checkout to version 0.3.0.
2. Install the editable package with test extras.
3. Migrate any manifests or docs using old private profile names to the public console profile contract.
4. Verify manifests, rendered bundles, fixtures, docs, package metadata, and the strict open-source audit.
5. Do not print, request, commit, or infer secret values.

Required setup:
```bash
git fetch origin
git checkout next
git pull --ff-only
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[test]"
```

Confirm version and package metadata:
```bash
.venv/bin/python - <<'PY'
from importlib.metadata import metadata, version
print(version("ophelia"))
for value in metadata("ophelia").get_all("Project-URL") or []:
    print(value)
PY
```

Expected version output:
```text
0.3.0
Repository, https://github.com/mrbagels/ophelia
Issues, https://github.com/mrbagels/ophelia/issues
```

Manifest migration notes:
- Replace old private profile usage with `profile: console`.
- Replace the old profile config block with `console:`.
- Use `console.surface: console` for a `/console` surface.
- Use `console.surface: root` when the root URL and `/console` both need verification.
- Replace legacy console env names with `OPHELIA_CONSOLE_*`.
- Keep app-specific live values outside Git. Use env var names and provider references, not raw values.

Core verification gate:
```bash
make validate-examples
make validate-manifests
make validate-fixtures
make validate-adoption-fixtures
make validate-fixture-plugins
make render-examples
make render-manifests
make test
make compile
make docs-check
make open-source-audit-strict
```

Useful smoke commands:
```bash
./cli/ship self-test --json
./cli/ship schema manifest --json
./cli/ship commands catalog --json
./cli/ship actions --json
./cli/ship live-readiness run \
  --runtime-root fixtures/app-suite/runtime \
  --manifests-dir fixtures/app-suite/manifests \
  --host-config fixtures/app-suite/host-inventory.yml \
  --provider-config fixtures/app-suite/integrations.yml \
  --allow-blocked \
  --json
```

Acceptance criteria:
- Installed metadata reports `0.3.0`.
- `make open-source-audit-strict` reports zero blockers and zero warnings.
- All selected manifests validate.
- Rendered bundles are regenerated successfully.
- Tests and docs checks pass.
- Any remaining issues are reported with exact file paths, commands run, and redacted diagnostics only.

Output format:
- Summary of what changed.
- Commands run and whether they passed.
- Manifest migrations applied.
- Remaining blockers, if any.
- Explicit confirmation that no secret values were printed or committed.
````
