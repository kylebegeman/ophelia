# App Adoption Planning

Status: active contract

`ship app adoption plan` is the read-only entrypoint for bringing an app
repository into the Ophelia contract. It is meant for future app repos first,
and for retained products only when an explicit adoption or deployment phase
starts.

## Command

```bash
./cli/ship app adoption plan demo-app \
  --repo-path ../demo-app \
  --environment staging \
  --json
```

Optional inputs:

- `--manifest path/to/.ophelia.yml`: use a non-default manifest path.
- `--runtime-root ~/ophelia-runtime`: show the readiness command that will be
  used later against truthful runtime state.

The default manifest path is `<repo-path>/.ophelia.yml`.

## What It Checks

The plan checks:

- the app repository path exists
- `.ophelia.yml` exists and parses with the Ophelia manifest parser
- the manifest `app` matches the requested app
- the manifest passes `ship pack validate`
- recommended repo-local Ophelia artifacts are present:
  - `ophelia/runbook.md`
  - `ophelia/agent.md`
  - `ophelia/checks/data-verify.sh`
  - `ophelia/hooks/pre-export.sh`
  - `ophelia/hooks/freeze.sh`
  - `ophelia/hooks/unfreeze.sh`
  - `ophelia/hooks/post-import.sh`

Missing or invalid manifests are blockers. Missing repo-local artifacts are
warnings because `ship pack init` can scaffold them in a preview-first flow.

## What It Emits

The JSON output is an `ophelia.plan` with:

- `blockers` and `warnings`
- `required_artifacts`
- embedded `pack_validation`
- `adoption_gates`
- `next_commands`
- `read_only: true`
- `mutates_state: false`
- `live_values_collected: false`

The next commands are ordered around the Ophelia contract:

1. preview repo artifact scaffolding with `ship pack init`
2. validate the manifest with `ship pack validate`
3. inspect the contract with `ship pack explain`
4. run `ship app readiness` later, after truthful runtime state exists
5. generate `ship app runbook` later, after readiness evidence exists

## Boundary

Adoption planning does not collect or write env values, secret values, runtime
state, provider state, probe results, receipts, deployment state, or GitHub
configuration. It does not mutate the app repo.

Use it to prove that an app repo is shaped for Ophelia before live hydration,
provider setup, production probes, or deployment work begins.
