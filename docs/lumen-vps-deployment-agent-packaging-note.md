# Prompt For Lumen VPS Deployment Agent

```text
You are the Lumen/VPS deployment agent. Pause any Ophelia deployment work that assumes Kyle's local checkout or a raw `~/ophelia` repo checkout is the final integration model.

Current Ophelia source of truth:
- GitHub repo: https://github.com/braintreelabs/ophelia
- Branch: next
- Current pushed commit with Ophelia 2.0 portability foundation: 7fc89d6

Important direction change:
Kyle wants Lumen to avoid depending on local files or a repo-style checkout when possible. Another agent is about to do a packaging pass so Ophelia can be installed and invoked as a proper Python package from GitHub, ideally via `pip install git+https://github.com/braintreelabs/ophelia.git@<tag-or-commit>`.

Do not build new deployment automation that hardcodes:
- `/Users/kyle/Developer/platforms/ophelia`
- a Mac-local checkout
- prompt/scratchpad files
- a long-lived dependency on uninstalled source files
- direct assumptions that `templates/`, `config/`, or `manifests/` must be read from a repo root

Temporary fallback, if you absolutely must inspect current behavior before the packaging pass lands:

```bash
git clone https://github.com/braintreelabs/ophelia.git ~/ophelia
cd ~/ophelia
git checkout 7fc89d6
python3 -m venv .venv
.venv/bin/python -m pip install -e .
~/ophelia/cli/ship --help
```

But treat that as temporary only. The intended final model is:

```bash
python3 -m venv /opt/lumen/ophelia-venv
/opt/lumen/ophelia-venv/bin/python -m pip install "ophelia @ git+https://github.com/braintreelabs/ophelia.git@<tag-or-commit>"
/opt/lumen/ophelia-venv/bin/ship --help
```

Expected final integration shape:
- Lumen calls the installed `ship` console script.
- Runtime state remains on the VPS, for example `~/ophelia-runtime` or an explicitly configured runtime root.
- App manifests are supplied explicitly by path or through a configured manifest directory.
- Templates and package resources should come from the installed Ophelia package, not a repo checkout.
- Lumen should consume stable JSON from commands using `--json`.
- Mutating operations remain dry-run-first and require confirmation tokens.
- Do not mutate production DNS, Caddy, containers, volumes, backups, apps, or env files without explicit operator-reviewed plans and matching tokens.

Ophelia 2.0 capabilities available at commit `7fc89d6`:
- portable manifest fields: `pack`, `host_requirements`, `networking`, `data`, `hooks`
- pack validate/explain/init
- redacted env shape diff
- backup freshness/status
- route/domain conflict scanner
- readiness and portability score
- generated app runbook
- receipt browser
- export plan/create
- import plan/apply rehearsal preview
- restore drill plan/apply
- cutover plan/apply checkpoint
- traffic plan/apply/rollback
- per-app isolation planning
- local Job/Action API descriptors

Before doing live VPS setup, wait for the packaging pass commit/tag, then install that package version rather than wiring against this local checkout.
```

