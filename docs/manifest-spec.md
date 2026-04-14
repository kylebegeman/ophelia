# Manifest Spec

Each app repo should eventually include an `.ophelia.yml` file.

## Top-level Fields

- `version`: integer manifest version
- `app`: stable app slug
- `kind`: `service`, `multi-service`, `static`, `tunnel`, or `redirect`
- `image`: default container image reference for service-based apps
- `services`: named service definitions
- `routes`: public routing definitions
- `addons`: shared service requirements
- `resources`: runtime limits
- `env`: app-wide environment variables
- `static_root`: filesystem root for static apps
- `tunnel_target`: default upstream for tunnel apps
- `redirect_to`: destination for redirect apps
- `redirect_status`: redirect status for redirect apps

## Service Fields

- `port`: container port exposed inside Docker
- `host_port`: optional `127.0.0.1` bridge port for incremental migration behind
  the legacy `~/edge` Caddy
- `image`: optional per-service image override
- `command`: optional command override
- `env`: service-specific environment values
- `healthcheck`: HTTP or command health check definition

## Route Fields

- `domain`: host name for the site block
- `service`: named service target inside the manifest
- `upstream`: direct upstream target such as `host.docker.internal:3501`
- `path`: exact path matcher
- `path_prefix`: prefix matcher for a route subtree
- `strip_prefix`: prefix to remove before proxying
- `rewrite_prefix`: prefix to prepend before proxying

Use `rewrite_prefix` for host aliases that should map to an upstream subpath.

Example:

- `docs.bagels.top/` -> upstream `/docs`
- `admin.bagels.top/` -> upstream `/admin`

Pair that with exact-path passthrough routes for endpoints that should stay
unaltered on the alias host.

## Example: single-service app

```yaml
version: 1
app: dragon-writer
kind: service
image: ghcr.io/mrbagels/dragon-writer:latest

services:
  web:
    port: 3000
    host_port: 3601
    healthcheck:
      path: /health

routes:
  - domain: dragonwriter.begam.in
    service: web

addons:
  postgres: true
  redis: false

resources:
  memory: 256m

env:
  NODE_ENV: production
```

## Example: path-routed multi-service app

```yaml
version: 1
app: pokedex
kind: multi-service

services:
  api:
    image: ghcr.io/mrbagels/pokedex-api:latest
    port: 3001
    host_port: 3701
    healthcheck:
      path: /health
  web:
    image: ghcr.io/mrbagels/pokedex-web:latest
    port: 3000
    host_port: 3702
    healthcheck:
      path: /

routes:
  - domain: pokedex.begam.in
    path_prefix: /api
    strip_prefix: /api
    service: api
  - domain: pokedex.begam.in
    service: web
```

When `addons.postgres: true`, `ship deploy --apply` provisions a dedicated
database and role in the shared Postgres container and writes `DATABASE_URL`
into the app runtime env file. `addons.redis: true` writes `REDIS_URL` against
the shared Redis instance and assigns the next free logical Redis database.

## Example: static site

```yaml
version: 1
app: portfolio
kind: static
static_root: /home/kyle/ophelia-runtime/static/portfolio

routes:
  - domain: kylebegeman.com
```

## Example: tunnel

```yaml
version: 1
app: aspectavy-staging
kind: tunnel
tunnel_target: host.docker.internal:3401

routes:
  - domain: staging-app.bagels.top
  - domain: staging-docs.bagels.top
    path: /api/openapi.json
  - domain: staging-docs.bagels.top
    path: /api/admin-cli.json
  - domain: staging-docs.bagels.top
    path: /api/ai/defaults.json
  - domain: staging-docs.bagels.top
    path_prefix: /docs
  - domain: staging-docs.bagels.top
    rewrite_prefix: /docs
  - domain: staging-admin.bagels.top
    path_prefix: /admin
  - domain: staging-admin.bagels.top
    rewrite_prefix: /admin
```

## Example: multi-upstream tunnel bridge

```yaml
version: 1
app: pokedex-dev
kind: tunnel

routes:
  - domain: dev.pokedex.begam.in
    path_prefix: /api
    strip_prefix: /api
    upstream: host.docker.internal:3711
  - domain: dev.pokedex.begam.in
    upstream: host.docker.internal:3712
```

## Example: redirect host

```yaml
version: 1
app: bagels-top-www
kind: redirect
redirect_to: https://bagels.top{uri}
redirect_status: 308

routes:
  - domain: www.bagels.top
```
