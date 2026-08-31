---
id: 0086
title: Lumen Decision plan projection
date: 2026-07-19
status: landed
areas: [agent, manifest-v2, decisions, lumen, deploy, contracts, docs, tests]
change_type: feature
commits: []
---

## Summary

Expose every locally observed manifest-plan digest required for Lumen to create
an exact, short-lived deployment Decision after a remote host plans a manifest.

## Why

The Ophelia host already verified a Lumen Decision against its durable local
plan, but the remote plan report omitted `request_digest`,
`observed_state_digest`, and `policy_digest`. Lumen could not safely construct
the claim without guessing or duplicating host planning logic.

## Contract Impact

`ophelia.manifest-v2-plan` reports now include:

- `request_digest`
- `observed_state_digest`
- `policy_digest`

These are additive report fields. The host still loads its own durable plan and
verifies every field, signature, actor, audience, expiry, and nonce before
accepting `deploy.apply`. The local confirmation token remains removed from
agent results.

## Verification

- agent manifest-bundle and Decision tests
- complete unit suite
- Python compilation
- documentation link validation
