---
id: 0084
title: Encrypted host recovery and continuous observations
date: 2026-07-19
status: landed
areas: [backup, restore, recovery, age, observations, daemon, agent, journal, identity, systemd, cli, security, docs, tests]
change_type: feature
commits: []
---

## Summary

Add confirmation-bound encrypted host-control backups, verified clean-host
restore, bounded durable host observations, backup freshness, and outbound
observation delivery. Separate new host identity from the replaceable runtime
tree and add an installer mode that deliberately defers first daemon start for
recovery.

## Why

The authoritative operation journal made a host independently operable but
also made its recovery contract critical. A backup needed to prove encryption,
content integrity, safe archive structure, database consistency, and exact
publication. Lumen also needs current host posture without scraping the host or
opening an inbound port.

## Changed Areas

- `src/ophelia/daemon/recovery.py`: age-encrypted atomic backup objects, outer
  and inner manifests, online SQLite snapshot, safe extraction, digest and
  symlink verification, and atomic clean-root promotion.
- `src/ophelia/daemon/observations.py`: disk, load, memory, activity,
  certificate, backup, and connection posture.
- daemon config, service, API, agent, CLI, installer, enrollment, and systemd
  identity boundaries.
- operation-journal schema version 6 with canonical, digest-verified,
  retention-bounded host observations.

## Contract Impact

The outbound command protocol adds `host.backup.create` with the
`ophelia:backup:create` scope. Agent exchanges include the latest observation.
The local API adds `GET /v1/observations/latest`. New enrollment defaults put
host identity under `/var/lib/ophelia-identity`; existing explicit identity
paths remain valid.

## Safety Impact

Backup apply requires a drained host in maintenance, no active workload run,
no recoverable deployment, an allowlisted destination, and either the exact
local confirmation or an authenticated signed remote command. Identity and
trust keys are excluded. Restore requires root, an exact confirmation, a
protected age identity, an absent or empty target, bounded regular archive
members, complete digest verification, and a healthy restored SQLite database.
The runtime is promoted only after the receipt and ownership are prepared.

## Verification

- focused daemon, recovery, observation, agent, migration, and journal tests
- complete unit suite (786 tests)
- Python compilation
- documentation link validation
- strict public-surface audit

## Follow-Ups

- Implement and exercise the matching Lumen backup command and observation
  projection.
- Run clean-host recovery and lifecycle drills on disposable Linux hosts.
