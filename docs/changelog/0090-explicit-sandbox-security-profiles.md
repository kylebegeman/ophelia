# 0090: Explicit sandbox security profiles

Date: 2026-07-19

## Change

- Add strict `seccomp_profile` and `apparmor_profile` workload security fields
  to Manifest V2.
- Keep both profiles at `runtime_default` unless a manifest explicitly selects
  `unconfined`.
- Render the exception into Compose only for the workload that declares it.
- Add explicit capability grants and structured `/dev` device mappings so a
  sandbox can declare its complete outer-container boundary without a
  handwritten Compose override.
- Add an explicit `privileged` escape hatch for nested runtimes that require
  protected kernel mounts. It is accepted only with a declared non-zero runtime
  UID, disabled `no_new_privileges`, and explicitly unconfined seccomp and
  AppArmor profiles.

## Why

Lumen Runner executes a non-root, rootless Podman sandbox inside its own
container. Linux blocks the nested user namespace under the default outer
seccomp and AppArmor policies, while Docker's protected `/proc` mounts prevent
the inner OCI runtime from launching a workload even after bounded namespace
capabilities are granted. Manifest V2 previously offered no typed way to declare
these exceptions, which forced operators to choose between a nonfunctional
Runner and an undocumented Compose mutation.

The new fields keep every existing workload confined and make sandbox
exceptions visible, reviewable, digest-bound, and strictly validated.
Capability dropping and non-root execution remain independent secure defaults;
`ALL` cannot be added, and device mappings are limited to bounded `/dev` paths.
The privileged path remains fail-closed and Runner-specific: Compose pins the
outer process to the declared non-zero UID, and manifests that omit any required
security acknowledgement are rejected.
