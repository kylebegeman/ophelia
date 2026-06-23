# Plugin Contracts

Ophelia plugins are metadata contracts in this phase. They let trusted
directories describe extension points without letting arbitrary code run inside
Ophelia.

## Contract Boundary

Plugin discovery is read-only:

- Ophelia reads `ophelia-plugin.yml`, `ophelia-plugin.yaml`, or
  `ophelia-plugin.json`.
- Ophelia validates metadata, capability descriptors, command descriptors,
  operator-console surfaces, compatibility notes, and safety notes.
- Ophelia does not import plugin modules.
- Ophelia does not execute plugin code.
- Plugin command descriptors are not injected into the executable command
  catalog.
- Plugins must be disabled by default.

Runtime execution, installation, sandboxing, signing, and provider adapter
loading belong in later hardening work.

## Commands

```bash
./cli/ship plugins list --json
./cli/ship plugins validate path/to/ophelia-plugin.yml --trusted-root path/to/plugins --json
./cli/ship plugins catalog --plugins-dir path/to/plugins --json
make validate-fixture-plugins
```

## Manifest Shape

```yaml
schema_version: 1
kind: ophelia.plugin_manifest
name: example-plugin
version: 0.1.0
enabled_by_default: false
capabilities:
  - type: app_template
    id: example-template
    summary: Example app template metadata.
commands:
  - command: ship example inspect
    operation: plugin.example-plugin.inspect
    summary: Read-only example command descriptor.
    risk: low
    mutates_state: false
    requires_confirmation: false
    args_schema:
      type: object
      properties: {}
      required: []
      additionalProperties: false
```

Allowed capability types:

- `app_template`
- `workflow_template`
- `policy_pack`
- `provider_adapter`
- `secret_provider`
- `host_inventory_adapter`
- `lumen_surface`

## Safety Rules

Validation blocks plugin manifests when:

- `schema_version` is not `1`
- `kind` is not `ophelia.plugin_manifest`
- plugin names, capability ids, or operator-console surface ids are not lowercase slugs
- `enabled_by_default` is true
- command `args_schema` is not a bounded object schema with
  `additionalProperties: false`
- a mutating command descriptor lacks both `requires_confirmation: true` and a
  `plan_command`
- the manifest contains literal secret-shaped values
- validation is asked to trust a root that does not contain the manifest path

Plugin reports are passed through deep redaction. Fixed capability names such as
`secret_provider` are treated as metadata keys, not values.

## Fixture Plugin

The fixture app suite includes a plugin manifest at:

```text
fixtures/app-suite/plugins/fixture-app-suite/ophelia-plugin.yml
```

It describes the committed fixture apps, local provider observations, secret
observations, host inventory, and fixture live-readiness operator-console surface. It is a
metadata fixture only; it does not add executable commands.
