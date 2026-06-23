# Ophelia Change Records

Status: active practice

Ophelia uses lightweight file-per-change records so humans, private operator UIs, and future
agents can understand what changed without reconstructing intent from Git alone.
This is adapted for Ophelia's runtime, safety, and agent-contract work.

Change records are not release notes. They are small operational memory files
for logical changes.

## When To Add One

Add a change record for every logical change that affects:

- CLI behavior
- JSON contracts
- manifests or pack fields
- receipts or operation plans
- runtime layout
- safety behavior
- deployment, backup, restore, cutover, traffic, or provider flows
- API endpoints
- docs that guide implementation or operations
- tests that define behavior

Tiny typo-only docs fixes do not need a record unless they clarify a contract or
operational instruction.

## File Naming

Use monotonically increasing IDs:

```text
docs/changelog/0001-short-slug.md
docs/changelog/0002-next-change.md
```

Also update [INDEX](INDEX.md).

## Entry Size

Keep entries lightweight. A normal entry should fit on one screen. Include
enough context for a future agent to understand what changed, why, how it was
verified, and what safety constraints mattered.

## Status Values

Use one of:

- `planned`
- `in-progress`
- `landed`
- `superseded`

If a later change replaces or invalidates an older one, update the older entry
to `superseded` and link the replacement record. Do not delete the old record.

## Required Fields

Each record should include frontmatter:

```yaml
---
id: 0000
title: Short title
date: YYYY-MM-DD
status: landed
areas: [docs]
change_type: docs
---
```

Use [TEMPLATE](TEMPLATE.md) for the body.

## Commit Hashes

If the change has already landed, include commit hashes in the `commits` field:

```yaml
commits: [abc1234]
```

If the entry is written before committing, omit `commits` or leave it as an
empty list. Do not invent hashes.

## Safety Notes

Every entry should explicitly say whether the change can mutate VPS/runtime
state. If a change adds a mutating command, the entry must describe the plan
form, confirmation token behavior, and receipt behavior.

## Verification

List the commands that were actually run. If a command was intentionally not
run, say why in the entry.
