# Change Record Template

```yaml
---
id: 0000
title: Short title
date: YYYY-MM-DD
status: planned
areas: [docs]
change_type: docs
commits: []
---
```

## Summary

One or two sentences describing what changed.

## Why

The product, operational, safety, or developer-experience reason for the change.

## Changed Areas

- `path/or/module.py`: concise description
- `docs/example.md`: concise description

## Contract Impact

Describe CLI, JSON, manifest, API, receipt, runtime, or policy contract changes.
Write "None" when there is no contract impact.

## Safety Impact

State whether the change can mutate VPS or runtime state. For mutating behavior,
identify the dry-run plan, confirmation token, and receipt path.

## Verification

- `command that was run`

## Follow-Ups

- Follow-up item, or `None`
