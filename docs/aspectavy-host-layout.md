# AspectAvy Host Layout

This document describes the host structure Ophelia now serves for AspectAvy.
`bagels.top` is the live rehearsal environment for the final `aspectavy.com`
host split.

## Live Rehearsal Layout

Production on `bagels.top`:

| Host | Role | Current ownership |
| --- | --- | --- |
| `app.bagels.top` | customer-facing app and public/universal-link pages | repo-owned AspectAvy service manifest |
| `api.bagels.top` | raw backend/API host | repo-owned AspectAvy service manifest |
| `docs.bagels.top` | docs and reference host | repo-owned AspectAvy service manifest plus generic rewrite rules |
| `admin.bagels.top` | operator/admin host | repo-owned AspectAvy service manifest plus generic rewrite rules |
| `dev.bagels.top` | static prototypes and previews | platform-owned static manifest |

Staging on `bagels.top`:

| Host | Role | Current ownership |
| --- | --- | --- |
| `staging-app.bagels.top` | staging app host | repo-owned AspectAvy service manifest |
| `staging-api.bagels.top` | staging API host | repo-owned AspectAvy service manifest |
| `staging-docs.bagels.top` | staging docs host | repo-owned AspectAvy service manifest plus generic rewrite rules |
| `staging-admin.bagels.top` | staging admin host | repo-owned AspectAvy service manifest plus generic rewrite rules |

The production and staging source manifests now live in the AspectAvy repo:

```text
/Users/kyle/Developer/projects/multiplatform/aspectavy/aspectavy-platform/ops/deploy/ophelia/
```

Ophelia still owns the shared runtime, shared Caddy, shared Postgres/Redis, and
the `dev.bagels.top` static preview host.

## Future Mirror

Future mirror on `aspectavy.com`:

| Rehearsal | Mirror |
| --- | --- |
| `app.bagels.top` | `app.aspectavy.com` |
| `api.bagels.top` | `api.aspectavy.com` |
| `docs.bagels.top` | `docs.aspectavy.com` |
| `admin.bagels.top` | `admin.aspectavy.com` |
| `dev.bagels.top` | `dev.aspectavy.com` |

Mirror example:

```text
examples/aspectavy-production-mirror.ophelia.yml
```

As of April 15, 2026:

- `app.aspectavy.com` still resolves elsewhere through Vercel
- `api.aspectavy.com`, `docs.aspectavy.com`, `admin.aspectavy.com`, and
  `dev.aspectavy.com` are not yet pointed at this VPS

## Docs And Admin Alias Behavior

The backend still serves:

- docs UI under `/docs`
- machine-readable docs under:
  - `/api/openapi.json`
  - `/api/admin-cli.json`
  - `/api/ai/defaults.json`
- admin under `/admin`

Ophelia handles that generically with route-level behavior:

1. exact-path passthrough routes preserve the machine-readable docs endpoints
2. prefix passthrough routes preserve already-prefixed `/docs` and `/admin` URLs
3. host-root catch-all routes rewrite requests onto `/docs` or `/admin`

That means:

- `docs.bagels.top/` reaches upstream `/docs`
- `docs.bagels.top/openapi` reaches upstream `/docs/openapi`
- `docs.bagels.top/api/openapi.json` stays `/api/openapi.json`
- `admin.bagels.top/` reaches upstream `/admin`
- `admin.bagels.top/settings` reaches upstream `/admin/settings`

This behavior is manifest-driven, not hardcoded for AspectAvy in the template.

## Static Preview Host

`dev.bagels.top` is intentionally static for prototypes and previews.

Source of truth in the platform repo:

```text
platform/static/aspectavy-dev/
```

Published runtime path on the VPS:

```text
/home/kyle/ophelia-runtime/static/aspectavy-dev
```

The reconciler script copies that static content into the runtime root before
applying manifests.

## Deployment Through Ophelia

Platform-owned AspectAvy assets are now limited to:

- the `aspectavy-dev` static manifest
- shared Caddy/runtime ownership
- shared addon services

Production and staging are no longer applied through `platform/scripts/apply-manifests.sh`.
They now deploy from the AspectAvy repo through `~/ophelia/cli/ship deploy`
against the app-repo-owned source manifests already synced into:

- `~/ophelia-runtime/apps/aspectavy-staging/source-manifest.yml`
- `~/ophelia-runtime/apps/aspectavy-production/source-manifest.yml`

## DNS Status

As of April 15, 2026, the full rehearsal set resolves to the VPS:

- `app.bagels.top`
- `api.bagels.top`
- `docs.bagels.top`
- `admin.bagels.top`
- `dev.bagels.top`
- `staging-app.bagels.top`
- `staging-api.bagels.top`
- `staging-docs.bagels.top`
- `staging-admin.bagels.top`

Public ingress is live through Ophelia-managed Caddy on `80/443`.

## Remaining Follow-up

The major Ophelia-side migration for AspectAvy is done. Remaining work is now:

- keep the AspectAvy app-repo workflows publishing the GHCR image tags used by
  the source manifests
- retire the stopped legacy AspectAvy DB containers once rollback comfort is no
  longer needed
- move the mirrored `aspectavy.com` hostnames onto the VPS when the final domain
  cutover is ready
