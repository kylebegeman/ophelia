# 0061: Private Registry Local Image Fallback

Date: 2026-06-23

Status: landed

## Summary

Deploy apply now handles retained private-registry deployments where the target
image is already present locally but `docker compose pull` is denied by the
registry. Ophelia still attempts the pull first. If the pull fails, it verifies
every manifest image reference exists in Docker's local image cache before
continuing.

## Changes

- `src/ophelia/runtime.py`: adds a pull-or-local-cache apply phase for service
  manifests.
- `tests/test_apply_safety.py`: covers a denied private pull where the exact
  image reference exists locally and compose up is allowed to proceed.
- `pyproject.toml` and `README.md`: bump current package metadata to `0.3.4`.

## Safety

If any image reference is missing locally, apply still fails with the pull
error plus the missing image names. Digest-pinned image references remain exact;
tag-only references proceed only when Docker already has that exact tag locally.

## Verification

- `PYTHONPATH=src python3 -m unittest tests.test_apply_safety -v`
- `python3 -m py_compile src/ophelia/runtime.py tests/test_apply_safety.py`
