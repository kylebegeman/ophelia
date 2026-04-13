# Architecture

## Goals

- Put platform logic in one dedicated repo.
- Keep application code in application repos.
- Make runtime state reproducible from manifests and templates.
- Avoid VPS drift caused by hand-edited edge config.

## Boundaries

### `ophelia` owns

- `ship` CLI
- shared infrastructure definitions
- render templates
- migration and backup scripts
- operational docs

### app repos own

- application code
- Dockerfiles
- CI pipelines
- `.ophelia.yml` manifests

### the VPS owns

- generated per-app runtime bundles
- secrets and env files
- pulled container images
- Docker volumes
- logs and backups

## Runtime Shape

```text
~/ophelia-runtime/
  apps/
    dragon-writer/
      compose.yml
      env
      env.example
      manifest.lock.json
      release.json
      caddy/
        dragon-writer.caddy
  caddy/
    sites.d/
  static/
  backups/
  cache/
```

## Networking

Use two Docker networks:

- `ophelia-edge`: Caddy to public app traffic
- `ophelia-internal`: app to Postgres/Redis traffic

Shared services live in `platform/shared/compose.yml`. Generated app bundles
join both networks with stable aliases like `dragon-writer-web`.

## Deployment Model

Preferred flow:

1. app repo builds an image in CI
2. image is pushed to GHCR
3. `ship deploy` renders runtime config
4. `ship deploy` pulls the image and updates the app on the VPS
5. Caddy reloads against generated snippets

The current codebase only implements manifest parsing and local runtime bundle
generation plus remote bundle staging. Service startup and Caddy activation are
available through `ship deploy --host ... --apply`.
