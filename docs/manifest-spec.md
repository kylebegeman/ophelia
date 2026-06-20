# Manifest Spec

Each app repo should eventually include an `.ophelia.yml` file.

## Top-level Fields

- `version`: integer manifest version
- `app`: stable app slug
- `kind`: `service`, `multi-service`, `static`, `tunnel`, or `redirect`
- `environment`: optional `dev`, `staging`, or `production`
- `profile`: optional deployment preset, currently `prism`
- `image`: default container image reference for service-based apps
- `services`: named service definitions
- `routes`: public routing definitions
- `addons`: shared service requirements
- `resources`: runtime limits
- `env`: app-wide environment variables
- `env_files`: manifest-relative env fragments copied into the runtime bundle
- `edge`: optional public-edge features that need Caddy support beyond explicit host routes
- `static_root`: filesystem root for static apps
- `tunnel_target`: default upstream for tunnel apps
- `redirect_to`: destination for redirect apps
- `redirect_status`: redirect status for redirect apps
- `verify`: optional post-deploy HTTP verification checks
- `prism`: Prism-specific config when `profile: prism`
- `pack`: optional portable app pack metadata used by inventory, movement plans, and Lumen Ops receipts
- `host_requirements`: optional target host capability requirements
- `data`: optional data ownership, export, import, backup, and restore-drill contract
- `hooks`: optional allowlisted app pack hook paths for export/import/cutover workflows
- `depends_on`: optional app/service slugs that should be considered prerequisites
- `deployment_order`: optional integer sort key for explicit multi-app plans
- `migration_before`: optional migration step names that must run before apply
- `verify_before_next`: optional boolean requiring verification before later ordered apps

`environment` is backward compatible. Old manifests without it still load and
are treated as `unknown` by inspection commands. When set to `production`,
Ophelia requires a deploy-plan confirmation token before local apply.

Dependency metadata is advisory. Ophelia reports it in `ship explain`,
`ship preflight`, and operation templates, but it does not auto-run multi-app
deploys unless an operator explicitly invokes a template and confirms each
mutating step.

## Portable Pack Fields

The optional `pack`, `host_requirements`, `data`, and `hooks` sections make app
movement between hosts explicit while preserving old manifest behavior.

`pack` fields:

- `portability`: `critical`, `standard`, or `static`
- `owner`: `personal`, `business`, `platform`, or another owner label
- `description`: human-readable inventory summary
- `deploy_binding_file`: optional committed app binding file path

`host_requirements` fields:

- `arch`: target CPU architecture such as `amd64` or `arm64`
- `min_memory`: minimum target memory, for example `1g`
- `min_disk_free`: minimum free disk before import, for example `20g`
- `requires_edge`: whether Ophelia edge/Caddy must be ready
- `requires_docker`: whether Docker and Compose must be ready

`data` supported sections:

- `postgres`: app Postgres ownership, export, import, and verify behavior
- `redis`: Redis ownership or cache contract
- `volumes`: named volume or host path data that must survive movement
- `object_storage`: bucket or prefix state
- `static_assets`: static runtime asset state
- `external_services`: external systems needed by the app
- `backups`: backup, restore drill, and offsite requirements

When old manifests set `addons.postgres: true` or `addons.redis: true` and omit
`data`, Ophelia infers compatibility contracts:

```yaml
data:
  postgres:
    mode: shared-postgres-database
    inferred_from_addon: true
  redis:
    mode: redis-logical-db
    inferred_from_addon: true
```

Inferred contracts preserve deploy/status behavior, but critical app movement
needs explicit export, import, and verification behavior before cutover.

`hooks` fields:

- `pre_export`
- `freeze`
- `unfreeze`
- `post_import`
- `post_cutover`

Hook paths must be relative paths under `ophelia/hooks/` for pack validation.

## Service Fields

- `port`: container port exposed inside Docker
- `host_port`: optional `127.0.0.1` bridge port for incremental migration behind
  the legacy `~/edge` Caddy
- `image`: optional per-service image override
- `command`: optional command override
- `env`: service-specific environment values
- `env_files`: extra env fragments copied into the runtime bundle for this service
- `mounts`: manifest-relative files or directories copied into the runtime bundle and mounted into the container
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

## Edge Fields

Use `edge` for public-edge behavior that is not tied to one explicit domain.

- `edge.on_demand_tls.ask`: Caddy on-demand TLS ask URL. Use Caddy env placeholders like `{$TOKEN_NAME}` for secrets; Ophelia copies those values from the app runtime `env` file into the shared Caddy env file during `ship deploy --apply`.
- `edge.catch_all.service`: service target for a catch-all `https://` site block
- `edge.catch_all.upstream`: direct upstream target for a catch-all `https://` site block
- `edge.catch_all.http_redirect`: whether to render a catch-all `http://` to HTTPS redirect, default `true`
- `edge.catch_all.http_redirect_status`: redirect status for that HTTP redirect, default `308`

`edge.catch_all` requires `edge.on_demand_tls.ask` because Caddy needs a global
ask endpoint before it should issue certificates for arbitrary hostnames.
Ophelia renders one host-level `on_demand_tls` block from the active release set.
All active apps that use on-demand TLS must share the same ask endpoint; apply
fails during `caddy_global_sync` if active manifests disagree.

Example:

```yaml
edge:
  on_demand_tls:
    ask: http://my-app-control:8080/internal/caddy/allow?token={$MY_APP_EDGE_TOKEN}
  catch_all:
    service: redirector
```

## Example: single-service app

```yaml
version: 1
app: dragon-writer
environment: production
kind: service
image: ghcr.io/mrbagels/dragon-writer:latest

pack:
  portability: critical
  owner: personal
  description: Dragon Writer production app

host_requirements:
  arch: amd64
  min_memory: 1g
  min_disk_free: 20g
  requires_edge: true
  requires_docker: true

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

data:
  postgres:
    mode: shared-postgres-database
    database: dragon_writer
    export:
      format: custom
      command: pg_dump
    import:
      command: pg_restore
    verify:
      command: ophelia/checks/data-verify.sh
  volumes:
    - name: uploads
      mount: /app/uploads
      class: critical
      export: tar-zstd
      import: tar-zstd
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true

hooks:
  pre_export: ophelia/hooks/pre-export.sh
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh

resources:
  memory: 256m

env:
  NODE_ENV: production

verify:
  - name: health
    url: https://dragonwriter.begam.in/health
  - name: home
    url: https://dragonwriter.begam.in/
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

Portable pack and readiness commands are read-only by default:

```bash
./cli/ship pack validate examples/dragonwriter.ophelia.yml
./cli/ship pack explain examples/dragonwriter.ophelia.yml --json
./cli/ship env diff dragon-writer --environment production --json
./cli/ship backup status dragon-writer --environment production --json
./cli/ship app readiness dragon-writer --environment production --json
./cli/ship app runbook dragon-writer --environment production
./cli/ship app export plan dragon-writer --environment production --json
./cli/ship app import plan ./exports/dragon-writer.production.export/manifest.json --json
./cli/ship receipts list --app dragon-writer --json
./cli/ship pack init --app dragon-writer --environment production --critical --postgres --uploads --json
```

`ship pack init` previews by default. It writes scaffold files only when
`--write` is passed, and refuses to overwrite existing files without `--force`.
Future export create and import apply commands must use confirmation tokens from
matching plans.

## Prism Profile

Use `profile: prism` when the manifest is primarily hosting a Prism runtime or a Prism-backed operator surface.

Additional fields:

- `prism.admin_domain`: optional dedicated admin host that should proxy to the primary Prism service
- `prism.console_asset_path`: optional in-container path for a mounted Console bundle
- `prism.surface`: `console` or `quark`

When `prism.admin_domain` is set, Ophelia will synthesize a route for that host if you did not already declare one explicitly.

## Verification Checks

`verify` entries are HTTP checks that `ship verify` or `ship deploy --apply --verify` can run after deployment.
Ophelia-owned manifests should include at least one explicit verification check
so preflight, conflict scanning, rollback reports, and Quark operator views can
show concrete post-change checks.

Fields:

- `name`: optional human label
- `url`: required `http://` or `https://` URL
- `expect_status`: expected status code, default `200`
- `contains`: optional substring that must appear in the response body

`verify_policy` controls retry behavior and whether failed verification should
block the command. Defaults are tuned for first deploys where Caddy may still be
obtaining a certificate:

- `attempts`: total verification attempts, default `12`
- `interval`: initial delay between attempts in seconds, default `5`
- `timeout`: per-request and TLS handshake timeout in seconds, default `10`
- `failure_mode`: `hard` or `warn`, default `hard`

Verification uses backoff between attempts and separates certificate readiness
from external route checks for `https://` URLs. A failed run reports the phase as
`certificate_obtain` when TLS is not ready, or `external_route_verify` when TLS is
ready but the HTTP check still fails.

```yaml
verify:
  - name: health
    url: https://ops.begam.in/health
verify_policy:
  attempts: 12
  interval: 5
  timeout: 10
  failure_mode: hard
```

Prism manifests infer verification checks when `verify` is omitted:

- `surface: console`
  - `https://<primary-domain-or-admin-domain>/health`
  - `https://<primary-domain-or-admin-domain>/console`
- `surface: quark`
  - `https://<primary-domain-or-admin-domain>/health`
  - `https://<primary-domain-or-admin-domain>/`
  - `https://<primary-domain-or-admin-domain>/console`

## Mounts

Each service `mounts` item accepts:

- `source`: manifest-relative file or directory to copy into the runtime bundle
- `target`: absolute in-container path
- `read_only`: boolean, default `true`
- `bind`: when `true`, mount the source path directly from the VPS instead of copying it into the runtime bundle first

This is the primary way to ship Prism-hosted Console bundles or other static operator assets alongside an app image without baking them into the container first.

Use `bind: true` for persistent host paths such as uploads or local backup directories that should survive deploys and accept writes at runtime.

## Example: Prism-backed Quark surface

```yaml
version: 1
app: quark-ops
environment: production
profile: prism
kind: service
image: ghcr.io/bagelworks/prism:quark-latest

env_files:
  - env/quark-ops.shared.env

services:
  web:
    port: 8080
    env:
      PRISM_CONSOLE_SURFACE: quark
      PRISM_CONSOLE_ASSET_PATH: /opt/prism/console
    healthcheck:
      path: /health

routes:
  - domain: ops.begam.in
    service: web

addons:
  postgres: true
  redis: true

prism:
  admin_domain: ops.begam.in
  console_asset_path: /opt/prism/console
  surface: quark

verify:
  - name: health
    url: https://ops.begam.in/health
  - name: landing
    url: https://ops.begam.in/
  - name: console-fallback
    url: https://ops.begam.in/console
```

## Example: static site

```yaml
version: 1
app: portfolio
environment: production
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
