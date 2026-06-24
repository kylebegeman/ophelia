# 0069: First-Class Static Sites And README Polish

Date: 2026-06-24

Status: landed

## Summary

Ophelia now treats relative `static_root` values as first-class app-owned static
site sources. Static deploy plans report asset state, applies publish immutable
runtime static releases, staged bundles carry static source files for remote
deploy and rollback evidence, and Caddy serves the managed `current` symlink.
Package metadata is bumped to `0.3.11`.

The README was also fully refreshed with the Persephone SVG visual, current
badges, a static-site quick path, clearer service-app instructions, runtime
layout, docs navigation, release gates, and public-safe wording.

## Changes

- `src/ophelia/templates.py`: renders relative static roots through
  `{$OPHELIA_STATIC_ROOT}/<app>/current` while preserving absolute-root
  compatibility.
- `src/ophelia/runtime.py`: stages static source directories into bundles,
  publishes immutable static releases, updates the `current` symlink, and records
  static asset metadata in release records.
- `src/ophelia/planning.py`: adds static asset plan metadata, risk notes,
  structured changes, summaries, and confirmation-token input.
- `src/ophelia/app_factory.py`: aligns the `static-site` scaffold with
  repo-local `public/` assets and writes a starter `public/index.html`.
- `src/ophelia/commands/deploy.py`: shows static asset state in human
  `deploy --plan` output.
- `src/ophelia/adoption.py` and `src/ophelia/verify.py`: recognize manifest data
  verifier commands as app-owned runtime contract evidence.
- `README.md` and docs: document first-class static deploys, runtime layout,
  pack behavior, release behavior, and updated package metadata.

## Safety Notes

This change can mutate runtime static directories only when an operator runs an
existing deploy apply command for a static manifest. Plans remain read-only,
production applies still require confirmation tokens, and relative static
sources are checked for external symlinks before being copied.

## Verification

- `PYTHONPATH=src .venv/bin/python -m py_compile src/ophelia/runtime.py src/ophelia/planning.py src/ophelia/templates.py src/ophelia/app_factory.py src/ophelia/commands/deploy.py src/ophelia/adoption.py src/ophelia/verify.py tests/test_planning.py tests/test_app_factory.py tests/test_adoption.py`
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_planning.PlanningTests.test_relative_static_root_syncs_assets_and_plan_tracks_digest tests.test_planning.PlanningTests.test_static_asset_digest_changes_confirmation_token tests.test_planning.PlanningTests.test_static_deploy_from_release_bundle_lock_uses_bundled_assets tests.test_app_factory.TemplateDiscoveryTests.test_static_template_declares_repo_local_asset_root tests.test_app_factory.CreateApplyTests.test_apply_refuses_existing_files_without_force tests.test_adoption.AdoptionPlanTests.test_app_owned_contract_recognizes_manifest_data_verifier_without_npm_command -v`
- `make test`
- `make docs-check`
- `make validate-examples validate-manifests render-examples render-manifests`
- `make validate-fixtures validate-adoption-fixtures validate-fixture-plugins`
- `make compile`
- `make open-source-audit-strict`
- `.venv/bin/python -m pip install -e ".[test]"`
- `./cli/ship pack init --app demo-static --environment staging --directory /tmp/ophelia-readme-static --include-manifest --kind static --domain demo-static.example.com --write --force --json`
- `./cli/ship validate /tmp/ophelia-readme-static/.ophelia.yml`
- `./cli/ship deploy /tmp/ophelia-readme-static/.ophelia.yml --plan --runtime-root /tmp/ophelia-readme-runtime`
- `git diff --check`
