---
id: 0023
title: Phase 5 GitHub App and secret provider contracts
date: 2026-06-22
status: landed
areas: [github, secrets, providers, drift, doctor, command-catalog, docs, tests]
change_type: feature
commits: []
---

## Summary

Implemented Phase 5 of the strategic roadmap: GitHub provider contracts,
GitHub App metadata support, `gh` fallback semantics, local GitHub drift
observations, and secret-reference provider reports.

## Why

GitHub provisioning previously depended directly on local `gh` auth, and secret
readiness was centered on runtime env presence. Operators and agents need a
stable provider contract that can describe GitHub App configuration, fall back
to `gh`, and validate secret presence across providers without ever exposing
secret values.

## Changed Areas

- `src/ophelia/github_providers.py`: added GitHub provider status, GitHub App
  API operation descriptors, provider selection, and local GitHub drift
  observation comparison.
- `src/ophelia/secret_providers.py`: added names-only secret provider reports
  across local runtime env, GitHub environment observation files, and SOPS refs.
- `src/ophelia/app_factory.py`: extended GitHub provisioning plan/apply with
  `auto`, `gh`, and `github-app` provider contracts while preserving
  confirmation-token apply semantics.
- `src/ophelia/commands/providers.py`: added `ship providers github status`.
- `src/ophelia/commands/secrets.py`: added `ship secrets providers`.
- `src/ophelia/drift.py`: replaced the static GitHub `not_observed` placeholder
  with provider-backed local observation comparisons.
- `src/ophelia/inspection.py`: added doctor checks for GitHub and secret
  provider readiness.
- `config/ophelia-integrations.yml`: added the default non-secret integration
  contract.
- Docs and command catalog examples updated.

## Contract Impact

Additive CLI contract:

- `ship providers github status --json` emits
  `kind: "ophelia.github_provider_status"`.
- `ship secrets providers [manifest-or-app] --environment <env> --json` emits
  `kind: "ophelia.secret_provider_report"` when an app/manifest is supplied,
  or `kind: "ophelia.secret_provider_status"` for provider-only status.
- `ship app github plan|apply` now accepts
  `--github-provider auto|gh|github-app` and `--provider-config <path>`.
- `ship drift <manifest> --json` can consume local GitHub observation files
  under `<runtime_root>/github/observations/<app>.json`.

The existing `app.github.provision.plan` and `app.github.provision.apply`
operation IDs are unchanged.

## Safety Impact

Net positive. GitHub App private keys and webhook secrets are represented only
by env-var names and presence booleans. Secret provider reports emit names,
locations, presence booleans, and freshness metadata only. The GitHub App path
does not perform live HTTP mutation in this phase unless a caller supplies an
explicit runner; CLI usage without such a runner blocks before mutation.

## Verification

- `python3 -m compileall -q src/ophelia/github_providers.py src/ophelia/secret_providers.py src/ophelia/app_factory.py src/ophelia/commands/app.py src/ophelia/commands/providers.py src/ophelia/commands/secrets.py src/ophelia/drift.py src/ophelia/inspection.py`
- `PYTHONPATH=src python3 -m unittest tests.test_github_providers tests.test_secret_providers tests.test_app_factory tests.test_secrets_audit tests.test_drift tests.test_command_catalog`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m ophelia.docs_check`
- `PYTHON=python3 make validate-examples`
- CLI smoke tests for `providers github status`, `secrets providers`,
  GitHub provider planning, and drift with local observations.
- `git diff --check`

## Follow-Ups

- Begin Phase 6: multi-host inventory and placement planning.
- Later provider-adapter work can add real GitHub App HTTP execution and
  external vault adapters behind the contracts introduced here.
