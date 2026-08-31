---
id: 0093
title: Safe edge cutover and non-relocating installs for release 0.6.3
date: 2026-07-20
status: landed
areas: [release, edge, caddy, daemon, installer, migration, lumen, tests]
change_type: fix
commits: []
---

## Summary

Publish Ophelia 0.6.3 with an explicit static-root mount for the packaged Caddy
edge, actionable edge-start diagnostics, and non-relocating daemon virtual
environments. These changes let a host preserve an existing static tree while
moving edge authority to the packaged runtime and ensure installed `opheliad`
console scripts always reference their real interpreter.

Daemon releases are addressed by both package version and source digest. They
are created directly at that immutable final path, validated, and only then
promoted through the stable `current` link.

## Safety Invariants

- The configured static root must be an absolute, non-root, real directory.
- The packaged edge mounts the exact static root read-only at the same path.
- Caddy validation and reload reuse the static root persisted by bootstrap, so
  callers do not need to repeat migration-specific environment state.
- A failed Compose start includes bounded stderr or stdout detail in its JSON
  error report.
- Python virtual environments are never moved after console scripts are
  generated.
- The daemon release path binds both the package version and source digest.

## Verification

- edge-runtime materialization, custom static-root, and start-failure tests
- daemon installer final-path and digest-addressing tests
- full Ophelia test, compile, example, fixture, manifest, and documentation gates
