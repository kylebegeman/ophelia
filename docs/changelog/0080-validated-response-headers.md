---
id: 0080
title: Validated response-header policies
date: 2026-07-16
status: landed
areas: [manifest, caddy, static-sites, security, caching, docs, tests]
change_type: feature
commits: []
---

## Summary

App manifests can declare literal global, exact-path, path-prefix, and excluded
path response headers under `edge.response_headers`. Ophelia validates each rule
and renders it into every Caddy site block owned by the manifest.

## Why

Static applications need app-specific browser security and cache policies.
Keeping those policies in the app manifest makes them reviewable, repeatable,
and part of the same confirmed release as the site content.

## Changed Areas

- `src/ophelia/manifest.py`: parses, validates, round-trips, and diagnoses
  response-header rules.
- `src/ophelia/templates.py`: merges unconditional headers with the built-in
  security defaults and renders include/exclude matchers for scoped policies.
- `docs/manifest-spec.md`: documents the response-header contract and examples.
- `tests/test_manifest.py`, `tests/test_templates.py`, and
  `tests/test_schema_export.py`: cover safe parsing, unsafe-value rejection,
  schema export, lock round-tripping, and Caddy output.

## Contract Impact

The optional `edge.response_headers` list is additive. Existing manifests
render exactly the same default security headers as before.

## Safety Impact

Header names must use the HTTP token grammar. Values are bounded and reject
control characters and Caddy placeholders. Hop-by-hop headers and duplicate
name-and-selector pairs are rejected. Path matchers reuse Ophelia's canonical
HTTP path validation, and exclusions let cache rules remain mutually exclusive
instead of depending on repeated-field precedence.

## Verification

- `python3 -m unittest tests.test_manifest tests.test_templates tests.test_schema_export -v`
- `./cli/ship validate <manifest>` against a static-site manifest with global
  and scoped response headers

## Follow-Ups

- None.
