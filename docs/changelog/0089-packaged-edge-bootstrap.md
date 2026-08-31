# 0089: Packaged edge bootstrap

Date: 2026-07-19

## Change

- Package the minimal shared Caddy runtime with Ophelia.
- Add `ship caddy bootstrap --runtime-root <path> [--start]` to materialize it
  without requiring a source checkout.
- Keep Caddy state and runtime route fragments outside the installed package so
  package upgrades remain replaceable and application routes remain durable.

## Why

Manifest V2 correctly blocks routed plans when no shared edge exists, but an
installed Ophelia CLI previously had no self-contained way to create that edge.
Lumen exposed this missing bootstrap seam while converting its live topology to
Manifest V2.
