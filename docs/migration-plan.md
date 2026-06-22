# Migration Plan

Status: public migration outline

This plan describes the generic migration path into Ophelia. Product-specific
hostnames, runtime paths, DNS records, and rollback notes belong in private
operator documentation until that product has an approved adoption or deployment
phase.

## Migration Order

1. Add or generate `.ophelia.yml` in the app repository.
2. Run `ship app adoption plan` against the app repository.
3. Fix missing pack hooks, checks, manifests, CI scripts, and secret-name
   references in the app repo.
4. Validate the manifest with `ship validate` and `ship pack validate`.
5. Render into an isolated runtime root and inspect generated Compose/Caddy
   output.
6. Run fixture-backed readiness and hydration drills.
7. Add live provider/host/runtime evidence only when the product is approved for
   migration.
8. Produce deploy, export, restore, traffic, and rollback plans.
9. Apply only with reviewed confirmation tokens.
10. Record receipts and rollback notes outside the source checkout.

## App Adoption Entry Point

```bash
./cli/ship app adoption plan demo-app --repo-path ../demo-app --environment staging --json
```

The adoption plan is read-only and should run before collecting live env values,
provider state, DNS state, or deployment evidence.

## Live Evidence Boundary

Live evidence must be truthful and name-only unless explicitly approved:

- runtime env files stay outside Git
- secret values are never committed
- provider observations should report names and presence, not values
- probes are opt-in and should be gated by hydration evidence
- production mutations require dry-run plans and matching tokens
