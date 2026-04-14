# AspectAvy Host Layout

This document describes the host structure Ophelia now supports for AspectAvy.
The goal is to make `bagels.top` the real rehearsal environment for the final
`aspectavy.com` host split.

## Production Rehearsal

Live production layout target on `bagels.top`:

| Host | Role | Current Ophelia shape |
| --- | --- | --- |
| `app.bagels.top` | customer-facing app and public/universal-link pages | tunnel to the production backend |
| `api.bagels.top` | raw backend/API host | tunnel to the production backend |
| `docs.bagels.top` | docs and reference host | tunnel with exact endpoint passthrough plus `/docs` rewrite |
| `admin.bagels.top` | operator/admin host | tunnel with `/admin` passthrough plus `/admin` rewrite |
| `dev.bagels.top` | static prototypes and previews | static site served from Ophelia runtime |

Production manifest:

```text
manifests/aspectavy-production.ophelia.yml
```

## Staging

Preferred staging naming scheme:

| Host | Role |
| --- | --- |
| `staging-app.bagels.top` | staging app host |
| `staging-api.bagels.top` | staging API host |
| `staging-docs.bagels.top` | staging docs host |
| `staging-admin.bagels.top` | staging admin host |

Staging manifest:

```text
manifests/aspectavy-staging.ophelia.yml
```

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

As of April 14, 2026:

- `app.aspectavy.com` resolves elsewhere through Vercel
- `api.aspectavy.com`, `docs.aspectavy.com`, `admin.aspectavy.com`, and
  `dev.aspectavy.com` do not yet resolve to the VPS

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

Platform-owned AspectAvy ingress is now deployed through:

```text
platform/scripts/apply-manifests.sh
```

That script:

1. bootstraps the runtime root and Docker networks
2. syncs platform-owned static assets
3. applies every manifest in `manifests/`

Use this validation flow on the VPS:

1. `cd ~/ophelia`
2. `./platform/scripts/apply-manifests.sh`
3. `./platform/scripts/validate-caddy.sh`

If public DNS is ready and the generated Caddy config validates, use:

4. `./platform/scripts/cutover-public-edge.sh`

## DNS Status

As of April 14, 2026:

- already pointed at the VPS:
  - `app.bagels.top`
  - `api.bagels.top`
  - `staging-app.bagels.top`
  - `staging-api.bagels.top`
- not yet pointed at the VPS:
  - `docs.bagels.top`
  - `admin.bagels.top`
  - `dev.bagels.top`
  - `staging-docs.bagels.top`
  - `staging-admin.bagels.top`

That means Ophelia can render and stage the full rehearsal layout now, but the
public cutover should wait until those DNS records exist.

## Remaining Backend-Repo Follow-up

Ophelia can finish the host structure without touching the AspectAvy backend
repo, but the backend repo still needs follow-up later:

- move from tunnel manifests to repo-owned service/image manifests
- publish images through CI so Ophelia can manage runtime directly
- add explicit host-aware behavior if `app`, `api`, `docs`, and `admin` should
  diverge at the application layer
- verify that docs/admin HTML, assets, and redirects behave correctly under the
  new host split without leaking absolute legacy paths
