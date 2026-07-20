# Manifest Spec

Each app repo should eventually include an `.ophelia.yml` file.

## Version 2 Workload Manifests

Manifest v2 is the strict, journaled runtime contract for new applications.
It models independent workload lifecycles rather than treating one Compose
project as one indivisible service. Version 1 remains available as an explicit
compatibility contract and is never silently reinterpreted as v2.

Use the versioned commands for new manifests:

```bash
ship manifest check .ophelia.yml --json
ship manifest plan .ophelia.yml --runtime-root /var/lib/ophelia --json
ship manifest apply <plan-id> --confirm <token> --runtime-root /var/lib/ophelia --json
```

Generate a reviewed v2 candidate from a supported v1 service or static
manifest without changing the source file:

```bash
ship manifest migrate .ophelia.yml --to 2 --output .ophelia.v2.yml
```

Version 2 requires:

- an explicit `environment`
- named, digest-pinned production image artifacts or contained static roots
- one or more named workloads
- routes that target only `web` or `static` workloads
- an explicit or safely inferred `recreate`, `blue_green`, or `static_atomic`
  update strategy
- opaque `secret://` references, never secret values
- strict known fields at every level

Supported workload kinds are `web`, `worker`, `cron`, `task`, `migration`,
`internal`, and `static`. Web candidates can overlap for readiness checks.
Workers default to a fenced, non-overlapping handoff. Cron schedules are
fenced singletons. Tasks run only through separately accepted operations.
Migrations run exactly once per revision and retain container and marker
evidence for restart recovery. Blue-green migrations must be backward
compatible. Any migration that requires a backup remains blocked until the
plan can bind current backup evidence.

The v2 renderer creates a unique Compose project for each revision, a stable
per-app network, revision-specific edge aliases, strict resource and container
security defaults, a secret-reference document, an exact artifact lock, and a
revision-specific Caddy candidate. Planning writes only to operation staging
and the append-only plan index. Apply requires the exact plan-bound local
approval or a separately verified Lumen Decision claim, then runs through the
authoritative operation journal and receipt pipeline.

`security.run_as_non_root: true` is an enforced runtime assertion. Set an
explicit non-zero `run_as_user`, or ensure the image declares a non-root
`USER`. Preflight blocks an image whose effective user is empty, `root`, or a
zero UID instead of treating the manifest flag as advisory metadata.

Manifest-relative env files and static artifact trees are copied into private
operation staging during planning. Their exact paths and bytes are bound into
the reviewed plan. Apply rejects missing, added, or changed staged input before
journaling an operation. Published static artifacts live under a
revision-specific runtime directory, and Caddy switches to that immutable tree.

Example:

```yaml
version: 2
app: demo-service
environment: production

artifacts:
  app-image:
    image: ghcr.io/example/demo-service@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

workloads:
  web:
    kind: web
    artifact: app-image
    command: ["./bin/server"]
    port: 8080
    readiness:
      http: {path: /ready, port: 8080}
      timeout_seconds: 90
    resources: {memory: 512Mi, cpu: "1.0", pids: 256}
  jobs:
    kind: worker
    artifact: app-image
    command: ["./bin/worker"]
    update: {overlap: forbid}

routes:
  - name: public
    domain: demo-service.example.com
    target: {workload: web, port: 8080}

update:
  strategy: blue_green
  auto_rollback: true
  drain_seconds: 30

secrets:
  - name: DATABASE_URL
    ref: secret://demo-service/production/database-url
```

### Runtime identity, endpoints, and secrets

Manifest v2 can bind app-owned release identity into the exact plan:

```yaml
release:
  id: production-1842-a1b2c3d4e5f6
  commit_sha: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2
  build_time: 2026-07-19T20:00:00Z
```

Ophelia injects `OPHELIA_RELEASE_ID`, `OPHELIA_COMMIT_SHA`,
`OPHELIA_BUILD_TIME`, `OPHELIA_IMAGE_REF`, `OPHELIA_IMAGE_DIGEST`,
`OPHELIA_MANIFEST_HASH`, `OPHELIA_SERVICE`, and revision identity into each
container. If `release.id` is omitted, the immutable revision id is used.

A web workload can expose additional named ports when one process owns more
than one protocol endpoint. Routes may target only its primary `port` or one
declared endpoint:

```yaml
workloads:
  core:
    kind: web
    artifact: app-image
    port: 4773
    endpoints: {runner-control: 4774}
```

Opaque secret references resolve from a private host runtime env file. For
`secret://demo-service/production/database-url`, the resolver looks for
`DATABASE_URL` in a mode-0600 provider file beneath the runtime root. Values
are never copied into the manifest, plan, operation journal, receipt, or
Compose document. The default candidates are:

- `apps/<app>/environments/<environment>/env`
- `apps/<app>/env` for v1-compatible runtime state
- `secrets/<app>.<environment>.env`

Environment secrets remain the default. File secrets are materialized into a
revision-private directory and mounted read-only, which supports private keys,
CA bundles, and other multiline credentials without exposing their contents as
environment values:

```yaml
secrets:
  - name: APP_CA_PATH
    ref: secret://demo-service/production/app-ca-base64
    mode: file
    target: /run/demo-service/app-ca.pem
    encoding: base64
```

### TLS and authenticated machine routes

Routes default to automatic public TLS. Set `tls.mode: internal` for a private
edge such as a Cloudflare Tunnel origin. A direct machine-control hostname can
verify client certificates and forward only edge-derived identity:

```yaml
routes:
  - name: machine-control
    domain: control.example.com
    target: {workload: core, port: 4773}
    client_auth:
      # Enrollment may connect without a certificate; enrolled exchanges still
      # fail in the application unless Caddy forwards a verified fingerprint.
      mode: verify_if_given
      trust_pool_ref: secret://demo-service/production/client-ca-base64
      trust_pool_encoding: base64
      forward:
        authorization_ref: secret://demo-service/production/proxy-token
        authorization_header: X-Ophelia-Proxy-Authorization
        fingerprint_header: X-Ophelia-Client-Certificate-Sha256
```

Ophelia materializes the trust pool privately, synchronizes the proxy secret
into the shared Caddy env file, overwrites the protected upstream headers, and
forwards Caddy's verified SHA-256 client-certificate fingerprint. Use a direct
DNS record for this hostname. A TLS-terminating CDN cannot preserve the client
certificate boundary.

The remaining sections document manifest v1 compatibility behavior.

## Top-level Fields

- `version`: integer manifest version
- `app`: stable app slug
- `kind`: `service`, `multi-service`, `static`, `tunnel`, or `redirect`
- `environment`: optional `dev`, `staging`, or `production`
- `profile`: optional deployment preset, currently `console`
- `image`: default container image reference for service-based apps
- `services`: named service definitions
- `routes`: public routing definitions
- `addons`: shared service requirements
- `resources`: runtime limits
- `env`: app-wide environment variables
- `required_env`: env key names that must exist in the runtime `env` file but
  must not be rendered as inline Compose environment overrides
- `env_files`: manifest-relative env fragments copied into the runtime bundle
- `edge`: optional public-edge features that need Caddy support beyond explicit host routes
- `static_root`: static asset root for static apps. Relative paths are synced
  from the app repo into the Ophelia runtime during deploy. Absolute paths are
  treated as externally managed serving roots for backward compatibility.
- `tunnel_target`: default upstream for tunnel apps
- `redirect_to`: destination for redirect apps
- `redirect_status`: redirect status for redirect apps
- `verify`: optional post-deploy HTTP verification checks
- `console`: Console-specific config when `profile: console`
- `pack`: optional portable app pack metadata used by inventory, movement plans, and operator-console receipts
- `host_requirements`: optional target host capability requirements
- `networking`: optional Compose network topology, defaulting to shared compatibility
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

The optional `pack`, `host_requirements`, `networking`, `data`, and `hooks`
sections make app movement between hosts explicit while preserving old manifest
behavior.

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

`networking` fields:

- `edge`: currently only `shared`, meaning services join the shared
  `ophelia-edge` network for Caddy ingress
- `internal`: `shared` or `per-app`; omitted manifests keep `shared`
  compatibility with `ophelia-internal`

When `networking.internal: per-app` is set, service Compose output joins
`ophelia-edge` plus an app/environment private network named
`<app>-<environment>-internal`. Compose creates that private network when the
already-confirmed deploy apply flow runs. Ophelia does not migrate existing apps
to private internals unless their manifest opts in.

`data` supported sections:

- `postgres`: app Postgres ownership, export, import, and verify behavior
- `redis`: Redis ownership or cache contract
- `volumes`: named volume or host path data that must survive deploys and app
  movement
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

- `docs.example.com/` -> upstream `/docs`
- `admin.example.com/` -> upstream `/admin`

Pair that with exact-path passthrough routes for endpoints that should stay
unaltered on the alias host.

## Edge Fields

Use `edge` for public-edge behavior that is not tied to one explicit domain.

- `edge.on_demand_tls.ask`: Caddy on-demand TLS ask URL. Use Caddy env placeholders like `{$TOKEN_NAME}` for secrets; Ophelia copies those values from the app runtime `env` file into the shared Caddy env file during `ship deploy --apply`.
- `edge.tls.mode`: TLS rendering for explicit site blocks. Defaults to `auto`,
  which leaves Caddy's public ACME behavior unchanged. Use `internal` for
  private Cloudflare Access origins that cannot complete public ACME challenges,
  or `custom` with `cert_file` and `key_file` for a provisioned origin
  certificate.
- `edge.tls.cert_file`: certificate file path for `mode: custom`
- `edge.tls.key_file`: private key file path for `mode: custom`
- `edge.catch_all.service`: service target for a catch-all `https://` site block
- `edge.catch_all.upstream`: direct upstream target for a catch-all `https://` site block
- `edge.catch_all.http_redirect`: whether to render a catch-all `http://` to HTTPS redirect, default `true`
- `edge.catch_all.http_redirect_status`: redirect status for that HTTP redirect, default `308`

For Cloudflare-protected private hostnames, prefer a Cloudflare Origin CA
certificate rendered with `mode: custom` when the operator has one available.
`mode: internal` renders Caddy's internal CA and is suitable only when the edge
provider is configured to accept encrypted origin connections without public CA
validation.

`edge.catch_all` requires `edge.on_demand_tls.ask` because Caddy needs a global
ask endpoint before it should issue certificates for arbitrary hostnames.
Ophelia renders one host-level `on_demand_tls` block from the active release set.
All active apps that use on-demand TLS must share the same ask endpoint; apply
fails during `caddy_global_sync` if active manifests disagree.

Example:

```yaml
edge:
  tls:
    mode: internal
```

```yaml
edge:
  tls:
    mode: custom
    cert_file: /etc/caddy/certs/app-origin.pem
    key_file: /etc/caddy/certs/app-origin.key
```

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
app: demo-service
environment: production
kind: service
image: ghcr.io/example/demo-service:latest

pack:
  portability: critical
  owner: personal
  description: Demo service production app

host_requirements:
  arch: amd64
  min_memory: 1g
  min_disk_free: 20g
  requires_edge: true
  requires_docker: true

networking:
  internal: per-app

services:
  web:
    port: 3000
    host_port: 3601
    healthcheck:
      path: /health

routes:
  - domain: demo-service.example.com
    service: web

addons:
  postgres: true
  redis: false

data:
  postgres:
    mode: shared-postgres-database
    database: demo_service
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
    offsite:
      provider: restic
      target: s3://ophelia-fixture-backups/demo-service
      retention_days: 30
      encryption_required: true
      restore_rehearsal_cadence_days: 30
      last_rehearsal_ref: restore-drills/latest.json

hooks:
  pre_export: ophelia/hooks/pre-export.sh
  freeze: ophelia/hooks/freeze.sh
  unfreeze: ophelia/hooks/unfreeze.sh
  post_import: ophelia/hooks/post-import.sh

resources:
  memory: 256m

env:
  NODE_ENV: production

required_env:
  - API_TOKEN

verify:
  - name: health
    url: https://demo-service.example.com/health
  - name: home
    url: https://demo-service.example.com/
  - name: internal-runtime-health
    type: command
    service: web
    command:
      - npm
      - run
      - ophelia:health
      - --
      - --json
    expect_json:
      status: ok
```

## Example: path-routed multi-service app

```yaml
version: 1
app: demo-multi-service
kind: multi-service

services:
  api:
    image: ghcr.io/example/demo-api:latest
    port: 3001
    host_port: 3701
    healthcheck:
      path: /health
  web:
    image: ghcr.io/example/demo-web:latest
    port: 3000
    host_port: 3702
    healthcheck:
      path: /

routes:
  - domain: demo-multi.example.com
    path_prefix: /api
    strip_prefix: /api
    service: api
  - domain: demo-multi.example.com
    service: web
```

When `addons.postgres: true`, `ship deploy --apply` provisions a dedicated
database and role in the shared Postgres container and writes `DATABASE_URL`
into the app runtime env file. `addons.redis: true` writes `REDIS_URL` against
the shared Redis instance and assigns the next free logical Redis database.
Ophelia-injected runtime metadata such as `OPHELIA_ENVIRONMENT`,
`OPHELIA_APP`, `OPHELIA_SERVICE`, `OPHELIA_RELEASE_ID`,
`OPHELIA_IMAGE_REF`, `OPHELIA_IMAGE_DIGEST`, `OPHELIA_COMMIT_SHA`,
`OPHELIA_BUILD_TIME`, and `PORT` is rendered into Compose directly and is not
required in retained app runtime env files unless the manifest explicitly
declares it in `required_env`.

`ship deploy` accepts app-owned release metadata so copied manifests and VPS
deploys do not inherit Ophelia's source checkout identity:

```bash
ship deploy .ophelia.yml \
  --apply \
  --release-id app-v1.2.3 \
  --commit-sha <app-commit-sha> \
  --build-time 2026-06-25T12:00:00Z
```

Precedence is CLI flag, then environment, then generated fallback. Environment
fallbacks are `OPHELIA_DEPLOY_RELEASE_ID` or `OPHELIA_RELEASE_ID`,
`OPHELIA_DEPLOY_COMMIT_SHA` or `OPHELIA_COMMIT_SHA` or `GITHUB_SHA`, and
`OPHELIA_DEPLOY_BUILD_TIME` or `OPHELIA_BUILD_TIME`. If no commit SHA is
provided, `OPHELIA_COMMIT_SHA` is rendered as an empty string. It is never
filled from the Ophelia repository checkout.

Portable pack and readiness commands are read-only by default:

```bash
./cli/ship pack validate examples/service-app.ophelia.yml
./cli/ship pack explain examples/service-app.ophelia.yml --json
./cli/ship env diff demo-service --environment production --json
./cli/ship backup status demo-service --environment production --json
./cli/ship backup rehearse plan ./exports/demo-service.production.export.tar --manifest .ophelia.yml --json
./cli/ship backup rehearse apply ./exports/demo-service.production.export.tar --manifest .ophelia.yml --confirm <token> --json
./cli/ship backup rehearse ./exports/demo-service.production.export.tar --manifest .ophelia.yml --environment production --json
./cli/ship release image-lock plan .ophelia.yml --output .ophelia.image-lock.json --pinned-manifest .ophelia.pinned.yml --json
./cli/ship release image-lock apply .ophelia.yml --output .ophelia.image-lock.json --pinned-manifest .ophelia.pinned.yml --confirm <token> --json
./cli/ship app fresh-install plan demo-service --environment staging --manifest .ophelia.staging.yml --json
./cli/ship app readiness demo-service --environment production --json
./cli/ship app runbook demo-service --environment production
./cli/ship app export plan demo-service --environment production --json
./cli/ship app export create demo-service --environment production --confirm <token> --json
./cli/ship app import plan ./exports/demo-service.production.export/manifest.json --json
./cli/ship app import apply ./exports/demo-service.production.export/manifest.json --confirm <token> --json
./cli/ship app restore-drill plan demo-service --environment production --source ./exports/demo-service.production.export.tar --json
./cli/ship app restore-drill apply demo-service --environment production --source ./exports/demo-service.production.export.tar --confirm <token> --json
./cli/ship app cutover plan demo-service --from source-host --to target-host --environment production --json
./cli/ship app cutover apply demo-service --from source-host --to target-host --environment production --confirm <token> --json
./cli/ship app traffic plan demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --json
./cli/ship app traffic apply demo-service --from source-host --to target-host --target-origin demo-service-target.example.net --environment production --confirm <token> --json
./cli/ship app traffic rollback plan demo-service --receipt <traffic-receipt-id> --environment production --json
./cli/ship app traffic rollback apply demo-service --receipt <traffic-receipt-id> --environment production --confirm <token> --json
./cli/ship receipts list --app demo-service --json
./cli/ship pack init --app demo-service --environment production --critical --postgres --uploads --json
./cli/ship pack init --app demo-service --environment staging --directory ../demo-service --include-manifest --kind service --domain demo-service.example.com --image ghcr.io/example/demo-service:latest --json
```

For critical apps using `data.postgres.mode: shared-postgres-database`,
cutover plan/apply include a `shared_postgres_cutover` checkpoint. Apply writes
`shared-postgres-cutover-evidence.json` beside the cutover plan and receipt.
That artifact records source/target hosts, the declared Postgres export,
import, verifier contract, latest backup references, offsite policy, required
human evidence, and rollback checkpoint notes. It is evidence scaffolding only:
Ophelia still does not dump, restore, mutate shared Postgres, Caddy, or DNS in
the cutover apply step.

`ship pack init` previews by default. It writes scaffold files only when
`--write` is passed, and refuses to overwrite existing files without `--force`.
With `--include-manifest`, it also previews or writes `.ophelia.yml`; service
manifests require `--domain` and `--image`, and static manifests require
`--domain` and write a repo-local static root (`public/` by default).
`ship app export create` is confirmation-gated. By default it writes metadata,
redacted runtime files, local static/volume archives for declared local sources,
read-only Docker named-volume archives when a local helper image is available,
checksums, receipts, and a deterministic `.tar` archive of that bundle.
Successful complete export bundles satisfy `ship backup status`. Pass
`--include-postgres` on both export plan and create to run the allowlisted
read-only `pg_dump` path. If local `zstd` is available, export create also
writes the planned `.tar.zst` archive. Production traffic mutation remains
explicitly gated and must use confirmation tokens from matching plans.
`ship app import apply` writes an isolated rehearsal preview only. Restore drill
apply validates export artifact readability, safely extracts bundle data into
an isolated drill directory, runs declared app-owned volume verifier commands,
and writes receipts. Cutover apply writes a checkpoint receipt and does not
mutate Caddy or DNS.
Traffic apply writes a production traffic checkpoint receipt with DNS/Caddy
provider intent by default. File-backed provider execution is available only
when the matching plan and apply use `--execute-provider-mutation`,
`--provider-config`, non-manual providers, and provider config
`allow_mutation: true`. Cloudflare DNS execution uses `api_token_env`, updates
one matching DNS record by default, and requires `allow_create: true` before it
creates a missing record. Cloudflare TTL must be `1` for automatic TTL or
between `30` and `86400` seconds. Live Caddy reload additionally requires
provider config `reload: true`, `allow_reload: true`, and a Caddy `sites_dir`
matching `<runtime_root>/caddy/sites.d`.
Target health verification is read-only and available with
`--target-health-url` plus `--run-target-health`; apply must use the same health
inputs as the plan. Health URLs must be http(s) URLs without credentials, query
strings, or fragments.
Traffic rollback restores captured previous provider state from a traffic
receipt and refuses deletion-only rollback cases.

## Console Profile

Use `profile: console` when the manifest is primarily hosting an Ophelia console runtime or operator surface.

Additional fields:

- `console.admin_domain`: optional dedicated admin host that should proxy to the primary console service
- `console.console_asset_path`: optional in-container path for a mounted Console bundle
- `console.surface`: `console` or `root`

When `console.admin_domain` is set, Ophelia will synthesize a route for that host if you did not already declare one explicitly.

## Verification Checks

`verify` entries are post-deploy checks that `ship verify` or
`ship deploy --apply --verify` can run after deployment.
Ophelia-owned manifests should include at least one explicit verification check
so preflight, conflict scanning, rollback reports, and operator views can
show concrete post-change checks.

Fields:

- `name`: optional human label
- `type`: `http`, `internal`, or `command`. Defaults to `command` when
  `command` is present, `internal` when `path` is present, otherwise `http`.
- `url`: required `http://` or `https://` URL for `type: http`. URLs cannot
  contain credentials, query strings, or fragments. For compatibility,
  `type: internal` may also use an absolute `http://127.0.0.1`,
  `http://localhost`, or `http://[::1]` URL; Ophelia extracts the path and
  still runs the probe inside the target service container. External URLs are
  rejected for internal checks.
- `path`: required path for `type: internal`. Ophelia runs the check inside the
  target service container against `http://127.0.0.1:<service-port><path>`.
- `method`: HTTP method for `http` or `internal` checks, default `GET`.
- `expect_status`: expected HTTP status code, default `200`.
- `service`: Compose service name for `type: internal` or `type: command`.
  `service: app` is accepted for single-service manifests.
- `command`: argv array or shell-tokenized string for `type: command`. Command
  checks run as `docker compose exec -T <service> ...` inside the rendered app
  compose project.
- `expect_exit`: expected command exit code, default `0`.
- `contains`: optional substring that must appear in the HTTP response body or
  command stdout.
- `expect_json`: optional mapping of dot-paths to exact JSON values. HTTP checks
  parse the response body; command checks parse stdout. Values and command text
  are redacted in plans and receipts.
- `json_assertions`: optional schema-aware assertions. Supported forms include
  `$.kind == "product.runtime.health"`, `$.ok is true`, `$.release.version =~
  "^\\d+\\.\\d+\\.\\d+$"`, `$.checks.runtime present`, or mapping form with
  `path` plus `equals`, `present`, `is_true`, `is_false`, or `regex`.

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
ready but the HTTP check still fails. Command checks still run when HTTP/TLS
checks fail, so app-owned runtime evidence remains visible in the same receipt.

```yaml
verify:
  - name: health
    type: http
    url: https://ops.example.net/health
    expect_json:
      status: ok
  - name: ophelia-health
    service: app
    path: /ophelia/health
    method: GET
    expect_status: 200
    json_assertions:
      - $.kind == "product.runtime.health"
      - $.ok == true
  - name: internal-health
    type: command
    service: web
    command:
      - npm
      - run
      - ophelia:health
      - --
      - --json
    expect_json:
      status: ok
      checks.runtime.ok: true
verify_policy:
  attempts: 12
  interval: 5
  timeout: 10
  failure_mode: hard
```

Recommended app-owned runtime endpoints and scripts:

- `GET /ophelia/health`
- `GET /ophelia/release`
- `npm run ophelia:health`
- `npm run ophelia:data:verify`
- `npm run ophelia:release`

Readiness uses these checks through internal service verification when they are
declared, then includes redacted JSON in the readiness report.

Console manifests infer verification checks when `verify` is omitted:

- `surface: console`
  - `https://<primary-domain-or-admin-domain>/health`
  - `https://<primary-domain-or-admin-domain>/console`
- `surface: root`
  - `https://<primary-domain-or-admin-domain>/health`
  - `https://<primary-domain-or-admin-domain>/`
  - `https://<primary-domain-or-admin-domain>/console`

## Lifecycle

`lifecycle` makes non-live and resettable apps explicit:

```yaml
lifecycle:
  live: false
  data_can_be_reset: true
  production_apply_allowed: false
```

- `live`: whether the product is live for users. Default `true`.
- `data_can_be_reset`: whether declared data volumes may be deleted and
  initialized fresh by `ship app fresh-install`. Default `false`.
- `production_apply_allowed`: when `false`, `ship deploy --apply` refuses a
  production manifest before staging runtime files. Default `true`.

## Runtime Release Metadata

For service manifests, Ophelia injects these container env vars into the rendered
Compose environment:

- `OPHELIA_ENVIRONMENT`
- `OPHELIA_APP`
- `OPHELIA_SERVICE`
- `OPHELIA_RELEASE_ID`
- `OPHELIA_IMAGE_REF`
- `OPHELIA_IMAGE_DIGEST`
- `OPHELIA_COMMIT_SHA`
- `OPHELIA_BUILD_TIME`

Use these values as the recommended source for app-owned `/ophelia/release`
responses. They are runtime-owned keys; manifest `env` and service `env` values
with the same names are ignored during Compose rendering.

An installed Ophelia package can materialize and start the shared edge without
a source checkout:

```bash
ship caddy bootstrap --runtime-root /var/lib/ophelia --start --json
```

After changing shared Caddy files, use the native reload command instead of
custom container discovery:

```bash
ship caddy reload --runtime-root /var/lib/ophelia --json
```

The command validates config first, prefers the `shared-caddy-1` container, uses
legacy names only as fallback, and returns structured diagnostics with
`kind`, `ok`, `container`, `validated`, `reloaded`, `runtime_root`,
`config_path`, `warnings`, and `errors`. Reload adapts the Caddyfile with
`/etc/caddy/env` into temporary JSON before reloading so `{$OPHELIA_*}`
placeholders match container startup behavior.

## Mounts

Each service `mounts` item accepts:

- `source`: manifest-relative file or directory to copy into the runtime bundle
- `target`: absolute in-container path
- `read_only`: boolean, default `true`
- `bind`: when `true`, mount the source path directly from the VPS instead of copying it into the runtime bundle first

This is the primary way to ship console bundles or other static operator assets alongside an app image without baking them into the container first.

Use `bind: true` for persistent host paths such as uploads or local backup directories that should survive deploys and accept writes at runtime.

## Data Volumes

Each `data.volumes` item declares durable runtime data and export/import
behavior. When a volume declares `mount`, service Compose output mounts it into
the target container:

- `name`: logical volume id, required.
- `mount`: absolute in-container path. When present, Ophelia renders a runtime
  mount.
- `service`: target service for the mount. Omit only when the manifest has one
  service.
- `source`: optional host path or Compose-relative path. Bare relative values
  such as `uploads` render as explicit relative bind sources such as
  `./uploads` so Compose does not interpret them as named volumes. When omitted,
  Ophelia creates an app/environment-scoped Docker named volume.
- `class`: optional data class such as `critical`.
- `export`, `import`, `verify`: portability behavior for movement and restore
  drills.

`data.volumes[].verify.command` is an app-owned read-only verifier for restored
volume contents. It can use `{path}`, `{data_path}`, or `{volume_path}` as a
placeholder for the isolated extracted volume path; otherwise Ophelia appends
that path as the last argument.

`data.backups.offsite_required: true` should include concrete target metadata:

```yaml
data:
  backups:
    required: true
    restore_drill_required: true
    offsite_required: true
    offsite:
      provider: restic
      target: s3://ophelia-fixture-backups/demo-service
      retention_days: 30
      encryption_required: true
      restore_rehearsal_cadence_days: 30
      last_rehearsal_ref: restore-drills/latest.json
```

When offsite is required but any actionable field is missing, pack validation
emits `offsite_backup_target_missing` and lists the exact missing fields.
When `last_rehearsal_ref` is present, it must point to a JSON receipt or
evidence file relative to the manifest directory, unless it is an absolute path.
The evidence must report success through `ok: true` or a successful
`status`/`result`, and must include a timestamp such as `completed_at`.
`ship pack validate` reports missing, unreadable, unsuccessful, unstamped, or
stale evidence with a specific `offsite_rehearsal_evidence_*` warning.

Production manifests should pin images to immutable digests before apply. Use
`ship release image-lock plan <manifest> --json` to resolve tags through Docker
registry metadata, then `ship release image-lock apply <manifest> --confirm
<token>` to write a redacted image-lock artifact and, optionally, a pinned
manifest copy. The workflow writes only explicit output files and does not
deploy or mutate runtime state.

Named Docker volume ids include the app and environment, for example
`demo-service-production-uploads`, so staging and production apps on the same
host do not share data accidentally.

## Example: Console-backed operator surface

```yaml
version: 1
app: demo-console
environment: production
profile: console
kind: service
image: ghcr.io/example/console-runtime:latest

env_files:
  - env/demo-console.shared.env

services:
  web:
    port: 8080
    env:
      OPHELIA_CONSOLE_SURFACE: console
      OPHELIA_CONSOLE_ASSET_PATH: /opt/console/console
    healthcheck:
      path: /health

routes:
  - domain: console.example.com
    service: web

addons:
  postgres: true
  redis: true

console:
  admin_domain: console.example.com
  console_asset_path: /opt/console/console
  surface: console

verify:
  - name: health
    url: https://console.example.com/health
  - name: landing
    url: https://console.example.com/
  - name: console-fallback
    url: https://console.example.com/console
```

## Example: static site

```yaml
version: 1
app: portfolio
environment: production
kind: static
static_root: public

routes:
  - domain: portfolio.example.net
```

For relative `static_root` values, Ophelia treats the path as app-owned build
output. `ship deploy --plan` reports the source digest and whether assets need
syncing. `ship deploy --apply` copies the directory into
`<runtime-root>/static/<app>/releases/<release-id>` and updates
`<runtime-root>/static/<app>/current` for Caddy. The generated Caddy snippet
serves `{$OPHELIA_STATIC_ROOT}/<app>/current`, where the shared Caddy runtime
sets `OPHELIA_STATIC_ROOT` to the configured static runtime root.

Absolute `static_root` values still render as literal Caddy roots. Use that mode
only when another process owns publishing that directory.

## Example: tunnel

```yaml
version: 1
app: demo-tunnel
kind: tunnel
tunnel_target: host.docker.internal:3401

routes:
  - domain: app.example.com
  - domain: docs.example.com
    path: /api/openapi.json
  - domain: docs.example.com
    path: /api/admin-cli.json
  - domain: docs.example.com
    path: /api/ai/defaults.json
  - domain: docs.example.com
    path_prefix: /docs
  - domain: docs.example.com
    rewrite_prefix: /docs
  - domain: admin.example.com
    path_prefix: /admin
  - domain: admin.example.com
    rewrite_prefix: /admin
```

## Example: multi-upstream tunnel bridge

```yaml
version: 1
app: demo-tunnel
kind: tunnel

routes:
  - domain: demo-tunnel.example.com
    path_prefix: /api
    strip_prefix: /api
    upstream: host.docker.internal:3711
  - domain: demo-tunnel.example.com
    upstream: host.docker.internal:3712
```

## Example: redirect host

```yaml
version: 1
app: demo-static-www
kind: redirect
redirect_to: https://demo-static.example.com{uri}
redirect_status: 308

routes:
  - domain: www.demo-static.example.com
```
