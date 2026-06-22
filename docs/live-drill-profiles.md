# Live Drill Profiles

Live drill profiles are named, read-only rehearsal scenarios for
`ship live-readiness run` and the production hardening report. They let us keep
fixture-backed expected states in the repo now, then point the same profile
shape at real staging or production runtime roots later.

## Commands

List profiles:

```bash
./cli/ship live-drills list --json
```

Run one profile:

```bash
./cli/ship live-drills run fixture-suite-review --json
```

Run every profile:

```bash
make live-drills-fixtures
```

Equivalent direct command:

```bash
./cli/ship live-drills run-all \
  --profiles fixtures/app-suite/live-drills.yml \
  --json
```

## API

The local API exposes the fixture profile catalog and single-profile runs:

```text
GET /live-drills
GET /live-drills/<profile>
```

The API uses the default committed fixture profile file. Custom profile files
are intentionally CLI-only for now because the local API has no request body or
path override for this surface.

## Profile File

Fixture profiles live at:

```text
fixtures/app-suite/live-drills.yml
```

Each profile can define:

- runtime, manifest, host, provider, plugin, and fixture paths
- app/environment filters
- optional hardening report inclusion
- optional HTTP/Docker probe flags
- expected aggregate status, app counts, drift counts, app status maps, and
  hardening go/no-go state

Relative paths resolve from the profile file directory. This keeps fixture
profiles portable inside the repo and lets later staging/prod profiles live
next to their own observation files.

## Fixture Profiles

Current built-in profiles:

- `fixture-suite-review`: full fixture suite, including hardening and the
  intentionally incomplete blocked app.
- `fixture-postgres-focused`: one Postgres-backed stateful service.
- `fixture-static-focused`: one image-less static app.
- `fixture-incomplete-focused`: one deliberately blocked app.

All four profiles should return `status: "ok"` from the drill runner when the
expected mixed state is intact. Child reports can still be `blocked` or
`warning`; the drill status reports whether that state matched the profile
expectations.

## Safety Contract

Live drills are read-only:

- no provider mutation
- no workflow execution
- no state refresh
- no apply/create/rebuild commands
- no confirmation tokens accepted
- HTTP and Docker probes only run when a profile explicitly opts in
- output is passed through propagated redaction

For real staging or production profiles, omit expected states until the first
known-good baseline is reviewed. Without expectations, child blocked/warning
statuses propagate to the drill result.
