"""App factory and the default GitHub release/deploy model (Phase 10).

This module turns a small template name into a complete, ready-to-commit app
scaffold: an Ophelia manifest, agent/runbook docs, a smoke check, and the
GitHub Actions workflows + repo policy that drive the default release model
(``next`` -> staging, ``master`` -> production, release-label gated).

Safety contract (enforced by tests):

* :func:`create_plan` is **read-only** and writes nothing to disk. It computes
  the full scaffold in memory, returns it as a plan envelope, and never touches
  the runtime root, the repo, the filesystem, or any GitHub API.
* :func:`create_apply` writes the generated files **only** under an explicit
  ``target_dir`` the caller passes. It is gated by a confirmation token derived
  from the canonical plan input and refuses any ``target_dir`` that resolves
  inside the runtime root or the Ophelia repo source tree.
* Scaffold planning/apply never call GitHub. GitHub provisioning has its own
  explicit plan/apply pair: ``github_provision_plan`` is read-only, and
  ``github_provision_apply`` calls ``gh`` only after a matching confirmation
  token is supplied.
* Secrets are referenced **by name only** (``OPHELIA_DEPLOY_KEY``,
  ``GHCR_TOKEN``, ...). No generated file ever embeds a secret value; workflow
  files reference them as ``${{ secrets.NAME }}`` and docs name them as text.

Every public function returns a JSON-serializable dict carrying
``schema_version`` and ``kind``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .manifest import ManifestError, load_manifest
from .operation_schema import (
    SCHEMA_VERSION,
    artifact,
    error_envelope,
    issue,
    plan_envelope,
    receipt_envelope,
    token,
    utc_now,
)
from .github_providers import GITHUB_PROVIDERS, github_app_operations, resolve_github_provider
from .redaction import deep_redact

# ---------------------------------------------------------------------------
# Release / deploy model constants (the default GitHub model)
# ---------------------------------------------------------------------------

#: Branch -> deployment environment mapping for the default release model.
BRANCH_ENVIRONMENTS: Dict[str, str] = {"next": "staging", "master": "production"}

#: Valid release labels for the production release workflow.
RELEASE_LABELS: List[str] = ["release:patch", "release:minor", "release:major"]

#: Required PR review approvals before merge to a protected branch.
REQUIRED_REVIEW_COUNT = 1

#: The deployment environments managed by the default model.
DEPLOYMENT_ENVIRONMENTS: List[str] = ["staging", "production"]

#: Deploy secrets referenced by name only (never embedded).
DEPLOY_SECRETS: List[str] = [
    "OPHELIA_DEPLOY_HOST",
    "OPHELIA_DEPLOY_PORT",
    "OPHELIA_DEPLOY_USER",
    "OPHELIA_DEPLOY_KEY",
]

#: Registry secrets referenced by name only.
REGISTRY_SECRETS: List[str] = ["GHCR_TOKEN"]


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    """One app scaffold template.

    ``manifest_extra`` is a callable that returns template-specific manifest
    sections merged onto the common base. Keeping it a function (not a static
    dict) lets the manifest reference the concrete app name.
    """

    name: str
    summary: str
    kind: str
    portability: str
    requires_secrets: List[str]
    extra_files: List[str] = field(default_factory=list)
    needs_postgres: bool = False
    needs_redis: bool = False
    is_static: bool = False
    is_worker: bool = False
    has_data_volume: bool = False
    backups_required: bool = False


TEMPLATES: Dict[str, Template] = {
    "static-site": Template(
        name="static-site",
        summary="Static site served from a build directory (no container runtime).",
        kind="static",
        portability="static",
        requires_secrets=list(DEPLOY_SECRETS),
        is_static=True,
    ),
    "docker-web": Template(
        name="docker-web",
        summary="Single containerized web service behind the shared edge.",
        kind="service",
        portability="standard",
        requires_secrets=list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS),
    ),
    "web-postgres": Template(
        name="web-postgres",
        summary="Containerized web service with a shared-Postgres logical database.",
        kind="service",
        portability="standard",
        requires_secrets=list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS),
        needs_postgres=True,
        backups_required=True,
    ),
    "web-redis": Template(
        name="web-redis",
        summary="Containerized web service with a Redis logical database addon.",
        kind="service",
        portability="standard",
        requires_secrets=list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS),
        needs_redis=True,
    ),
    "worker": Template(
        name="worker",
        summary="Background worker service with no public route (internal only).",
        kind="service",
        portability="standard",
        requires_secrets=list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS),
        needs_redis=True,
        is_worker=True,
    ),
    "critical-data": Template(
        name="critical-data",
        summary=(
            "Demo Service-style critical app: Postgres database, a durable "
            "uploads volume, and mandatory backups with restore drills."
        ),
        kind="service",
        portability="critical",
        requires_secrets=list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS),
        needs_postgres=True,
        has_data_volume=True,
        backups_required=True,
        extra_files=["ophelia/hooks/pre-export.sh", "ophelia/checks/data-verify.sh"],
    ),
}


_BASE_GENERATED_FILES: List[str] = [
    ".ophelia.yml",
    "ophelia/agent.md",
    "ophelia/runbook.md",
    "ophelia/checks/smoke.sh",
    ".github/pull_request_template.md",
    ".github/dependabot.yml",
    ".github/labeler.yml",
]

_WORKFLOW_FILES: List[str] = [
    ".github/workflows/ophelia-staging.yml",
    ".github/workflows/ophelia-release.yml",
]


# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------


def _generated_paths(template: Template) -> List[str]:
    paths = list(_BASE_GENERATED_FILES)
    paths.extend(_WORKFLOW_FILES)
    if template.is_static:
        paths.append("public/index.html")
    for extra in template.extra_files:
        if extra not in paths:
            paths.append(extra)
    return paths


def templates_list() -> Dict[str, Any]:
    """List every template with summary, kind, required secrets, and files."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.app_templates",
        "templates": [
            {
                "name": template.name,
                "summary": template.summary,
                "kind": template.kind,
                "portability": template.portability,
                "requires_secrets": list(template.requires_secrets),
                "files": _generated_paths(template),
            }
            for template in sorted(TEMPLATES.values(), key=lambda t: t.name)
        ],
    }


def templates_explain(name: str) -> Dict[str, Any]:
    """Detail for one template, or an error envelope for an unknown name."""
    template = TEMPLATES.get(name)
    if template is None:
        return error_envelope(
            f"Unknown template `{name}`. Run `ship app templates list` to see available templates.",
            "unknown_template",
            blockers=[issue("unknown_template", f"Unknown template `{name}`.")],
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.app_template",
        "name": template.name,
        "summary": template.summary,
        "manifest_kind": template.kind,
        "portability": template.portability,
        "requires_secrets": list(template.requires_secrets),
        "files": _generated_paths(template),
        "needs_postgres": template.needs_postgres,
        "needs_redis": template.needs_redis,
        "backups_required": template.backups_required,
        "release_label_policy": _release_label_policy(),
        "deployment_environments": _deployment_environments(template),
    }


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------


def _domain_for(app: str) -> str:
    return f"{app}.example.com"


def _internal_domain_for(app: str) -> str:
    """Internal-only route domain for services with no public route (e.g. workers)."""
    return f"{app}-internal.example.com"


def _manifest_text(
    app: str,
    template: Template,
    environment: str,
    owner: str,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> str:
    """Render a `.ophelia.yml` for the template as YAML text.

    Generated by hand (not via a YAML dumper) so the output is stable, readable,
    and identical between the in-memory preview and the file written on apply.
    No secret values appear: only structural manifest fields.
    """
    lines: List[str] = [
        "version: 1",
        f"app: {app}",
        f"environment: {environment}",
        f"kind: {template.kind}",
    ]

    if template.kind == "service":
        lines.append(f"image: ghcr.io/OWNER/{app}:latest")

    lines.append("")
    lines.append("pack:")
    lines.append(f"  portability: {template.portability}")
    lines.append(f"  owner: {owner}")
    lines.append(f"  description: {app} ({template.name})")

    if template.is_static:
        lines.append("")
        lines.append("static_root: public")
        lines.append("")
        lines.append("routes:")
        lines.append(f"  - domain: {_domain_for(app)}")
        return "\n".join(lines) + "\n"

    # service kind
    lines.append("")
    lines.append("services:")
    lines.append("  web:")
    lines.append("    port: 3000")
    lines.append("    healthcheck:")
    lines.append("      path: /health")

    lines.append("")
    lines.append("routes:")
    if template.is_worker:
        # Workers have no public route; a placeholder internal route keeps the
        # manifest valid (routes are required) while signalling internal-only.
        lines.append(f"  - domain: {_internal_domain_for(app)}")
        lines.append("    service: web")
    else:
        lines.append(f"  - domain: {_domain_for(app)}")
        lines.append("    service: web")

    if template.needs_postgres or template.needs_redis:
        lines.append("")
        lines.append("addons:")
        lines.append(f"  postgres: {'true' if template.needs_postgres else 'false'}")
        lines.append(f"  redis: {'true' if template.needs_redis else 'false'}")

    data_lines = _data_section(app, template)
    if data_lines:
        lines.append("")
        lines.extend(data_lines)

    if template.portability == "critical":
        lines.append("")
        lines.append("hooks:")
        lines.append("  pre_export: ophelia/hooks/pre-export.sh")

    lines.append("")
    lines.append("env:")
    lines.append("  NODE_ENV: production")

    lines.append("")
    lines.append("verify:")
    lines.append("  - name: health")
    lines.append(f"    url: https://{_domain_for(app)}/health")

    return "\n".join(lines) + "\n"


def _data_section(app: str, template: Template) -> List[str]:
    lines: List[str] = []
    if not (template.needs_postgres or template.has_data_volume or template.backups_required):
        return lines
    lines.append("data:")
    if template.needs_postgres:
        lines.append("  postgres:")
        lines.append("    mode: shared-postgres-database")
        lines.append(f"    database: {app.replace('-', '_')}")
        if template.portability == "critical":
            lines.append("    export:")
            lines.append("      command: pg_dump")
            lines.append("    import:")
            lines.append("      command: pg_restore")
            lines.append("    verify:")
            lines.append("      command: ophelia/checks/data-verify.sh")
    if template.has_data_volume:
        lines.append("  volumes:")
        lines.append("    - name: uploads")
        lines.append("      mount: /app/uploads")
        lines.append("      class: critical")
        lines.append("      export: tar-zstd")
        lines.append("      import: tar-zstd")
    if template.backups_required:
        lines.append("  backups:")
        lines.append("    required: true")
        if template.portability == "critical":
            lines.append("    restore_drill_required: true")
            lines.append("    offsite_required: true")
    return lines


# ---------------------------------------------------------------------------
# Doc + workflow generation
# ---------------------------------------------------------------------------


def _agent_md(app: str, template: Template) -> str:
    secrets = "\n".join(f"- `{name}`" for name in template.requires_secrets)
    return (
        f"# {app} agent guide\n\n"
        f"This app was scaffolded from the Ophelia `{template.name}` template.\n\n"
        f"{template.summary}\n\n"
        "## Release model\n\n"
        "- Merge to `next` deploys to **staging** automatically.\n"
        "- Merge to `master` with a `release:patch|minor|major` label cuts a "
        "**production** release.\n"
        f"- A PR needs {REQUIRED_REVIEW_COUNT} approving review and green required "
        "status checks before merge.\n\n"
        "## Required secrets (by name only)\n\n"
        "These are configured in the repo/environment, never committed:\n\n"
        f"{secrets}\n\n"
        "## Manifest\n\n"
        f"The deploy contract lives in `.ophelia.yml` (kind `{template.kind}`, "
        f"portability `{template.portability}`).\n"
    )


def _runbook_md(app: str, template: Template) -> str:
    extra = ""
    if template.backups_required:
        extra = (
            "\n## Data safety\n\n"
            "- Backups are required for this app.\n"
            "- Restore drills validate that an export can be re-imported "
            "before any host cutover.\n"
        )
    return (
        f"# {app} runbook\n\n"
        "## Deploy to staging\n\n"
        "1. Open a PR into `next`.\n"
        "2. Get an approving review and green checks.\n"
        "3. Merge; the `ophelia-staging` workflow deploys to staging.\n\n"
        "## Release to production\n\n"
        "1. Open a PR into `master` with a `release:patch|minor|major` label.\n"
        "2. Get an approving review and green checks.\n"
        "3. Merge; the `ophelia-release` workflow cuts the production release.\n\n"
        "## Rollback\n\n"
        "- Re-run the previous successful release, or roll back via "
        "`ship rollback` using the prior release id.\n"
        f"{extra}"
    )


def _smoke_sh(app: str, template: Template) -> str:
    if template.is_worker:
        target = "internal worker has no public route; check container health instead"
        return (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            f"# Smoke check for {app} ({template.name}).\n"
            f"# {target}.\n"
            'echo "worker smoke: verify the container is running and healthy"\n'
        )
    url = f"https://{_domain_for(app)}/health"
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Smoke check for {app} ({template.name}).\n"
        f'URL="${{1:-{url}}}"\n'
        'echo "smoke: GET $URL"\n'
        'curl --fail --silent --show-error "$URL" >/dev/null\n'
        'echo "smoke: ok"\n'
    )


def _staging_workflow(app: str, template: Template | None = None) -> str:
    """GitHub Actions workflow: deploy to staging on merge to `next`.

    Secrets appear only as ``${{ secrets.NAME }}`` references.
    """
    template = template or TEMPLATES["docker-web"]
    if template.is_static:
        return (
            "name: ophelia-staging\n"
            "\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - next\n"
            "\n"
            "permissions:\n"
            "  contents: read\n"
            "\n"
            "jobs:\n"
            "  deploy-staging:\n"
            "    runs-on: ubuntu-latest\n"
            "    environment: staging\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - name: Validate static manifest\n"
            f"        run: echo \"validate {app} static manifest before staging deploy\"\n"
            "      - name: Deploy to staging host\n"
            "        env:\n"
            "          OPHELIA_DEPLOY_HOST: ${{ secrets.OPHELIA_DEPLOY_HOST }}\n"
            "          OPHELIA_DEPLOY_PORT: ${{ secrets.OPHELIA_DEPLOY_PORT }}\n"
            "          OPHELIA_DEPLOY_USER: ${{ secrets.OPHELIA_DEPLOY_USER }}\n"
            "          OPHELIA_DEPLOY_KEY: ${{ secrets.OPHELIA_DEPLOY_KEY }}\n"
            f"        run: echo \"deploy static assets for {app} to staging using the OPHELIA_DEPLOY_* secrets\"\n"
        )
    return (
        "name: ophelia-staging\n"
        "\n"
        "on:\n"
        "  push:\n"
        "    branches:\n"
        "      - next\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "  packages: write\n"
        "\n"
        "jobs:\n"
        "  deploy-staging:\n"
        "    runs-on: ubuntu-latest\n"
        "    environment: staging\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Log in to GHCR\n"
        "        run: echo \"${{ secrets.GHCR_TOKEN }}\" | docker login ghcr.io -u ${{ github.actor }} --password-stdin\n"
        "      - name: Build and push image\n"
        f"        run: docker build -t ghcr.io/${{{{ github.repository }}}}/{app}:staging . && docker push ghcr.io/${{{{ github.repository }}}}/{app}:staging\n"
        "      - name: Deploy to staging host\n"
        "        env:\n"
        "          OPHELIA_DEPLOY_HOST: ${{ secrets.OPHELIA_DEPLOY_HOST }}\n"
        "          OPHELIA_DEPLOY_PORT: ${{ secrets.OPHELIA_DEPLOY_PORT }}\n"
        "          OPHELIA_DEPLOY_USER: ${{ secrets.OPHELIA_DEPLOY_USER }}\n"
        "          OPHELIA_DEPLOY_KEY: ${{ secrets.OPHELIA_DEPLOY_KEY }}\n"
        f"        run: echo \"deploy {app} to staging using the OPHELIA_DEPLOY_* secrets\"\n"
    )


def _release_workflow(app: str, template: Template | None = None) -> str:
    """GitHub Actions workflow: cut a production release on merge to `master`.

    Validates the release label and pins/pushes the image; secrets appear only
    as ``${{ secrets.NAME }}`` references.
    """
    template = template or TEMPLATES["docker-web"]
    if template.is_static:
        return (
            "name: ophelia-release\n"
            "\n"
            "on:\n"
            "  pull_request:\n"
            "    types:\n"
            "      - closed\n"
            "    branches:\n"
            "      - master\n"
            "\n"
            "permissions:\n"
            "  contents: read\n"
            "\n"
            "jobs:\n"
            "  release:\n"
            "    if: github.event.pull_request.merged == true\n"
            "    runs-on: ubuntu-latest\n"
            "    environment: production\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - name: Validate release label\n"
            "        run: |\n"
            "          labels='${{ toJSON(github.event.pull_request.labels.*.name) }}'\n"
            "          echo \"$labels\" | grep -E 'release:(patch|minor|major)' || (echo 'missing release label' && exit 1)\n"
            "      - name: Deploy static release to production host\n"
            "        env:\n"
            "          OPHELIA_DEPLOY_HOST: ${{ secrets.OPHELIA_DEPLOY_HOST }}\n"
            "          OPHELIA_DEPLOY_PORT: ${{ secrets.OPHELIA_DEPLOY_PORT }}\n"
            "          OPHELIA_DEPLOY_USER: ${{ secrets.OPHELIA_DEPLOY_USER }}\n"
            "          OPHELIA_DEPLOY_KEY: ${{ secrets.OPHELIA_DEPLOY_KEY }}\n"
            f"        run: echo \"release static assets for {app} to production using the OPHELIA_DEPLOY_* secrets\"\n"
        )
    return (
        "name: ophelia-release\n"
        "\n"
        "on:\n"
        "  pull_request:\n"
        "    types:\n"
        "      - closed\n"
        "    branches:\n"
        "      - master\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "  packages: write\n"
        "\n"
        "jobs:\n"
        "  release:\n"
        "    if: github.event.pull_request.merged == true\n"
        "    runs-on: ubuntu-latest\n"
        "    environment: production\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Validate release label\n"
        "        run: |\n"
        "          labels='${{ toJSON(github.event.pull_request.labels.*.name) }}'\n"
        "          echo \"$labels\" | grep -E 'release:(patch|minor|major)' || (echo 'missing release label' && exit 1)\n"
        "      - name: Log in to GHCR\n"
        "        run: echo \"${{ secrets.GHCR_TOKEN }}\" | docker login ghcr.io -u ${{ github.actor }} --password-stdin\n"
        "      - name: Build and push release image\n"
        f"        run: docker build -t ghcr.io/${{{{ github.repository }}}}/{app}:latest . && docker push ghcr.io/${{{{ github.repository }}}}/{app}:latest\n"
        "      - name: Deploy to production host\n"
        "        env:\n"
        "          OPHELIA_DEPLOY_HOST: ${{ secrets.OPHELIA_DEPLOY_HOST }}\n"
        "          OPHELIA_DEPLOY_PORT: ${{ secrets.OPHELIA_DEPLOY_PORT }}\n"
        "          OPHELIA_DEPLOY_USER: ${{ secrets.OPHELIA_DEPLOY_USER }}\n"
        "          OPHELIA_DEPLOY_KEY: ${{ secrets.OPHELIA_DEPLOY_KEY }}\n"
        f"        run: echo \"release {app} to production using the OPHELIA_DEPLOY_* secrets\"\n"
    )


def _pull_request_template() -> str:
    labels = " | ".join(label.split(":", 1)[1] for label in RELEASE_LABELS)
    return (
        "## Summary\n\n"
        "Describe the change.\n\n"
        "## Release\n\n"
        f"For a production release into `master`, add a `release:{labels}` label.\n\n"
        "## Checklist\n\n"
        "- [ ] Tests pass\n"
        "- [ ] Manifest (`.ophelia.yml`) updated if the deploy contract changed\n"
        f"- [ ] {REQUIRED_REVIEW_COUNT} approving review requested\n"
    )


def _dependabot_yml() -> str:
    return (
        "version: 2\n"
        "updates:\n"
        "  - package-ecosystem: github-actions\n"
        "    directory: /\n"
        "    schedule:\n"
        "      interval: weekly\n"
        "  - package-ecosystem: docker\n"
        "    directory: /\n"
        "    schedule:\n"
        "      interval: weekly\n"
    )


def _labeler_yml() -> str:
    return (
        "# Map changed paths to labels for the labeler action.\n"
        "ophelia-manifest:\n"
        "  - changed-files:\n"
        "      - any-glob-to-any-file: '.ophelia.yml'\n"
        "ci:\n"
        "  - changed-files:\n"
        "      - any-glob-to-any-file: '.github/workflows/**'\n"
    )


def _pre_export_sh(app: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Pre-export hook for {app}: quiesce writes before a data export.\n"
        'echo "pre-export: flush and quiesce before export"\n'
    )


def _data_verify_sh(app: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Post-import data verification for {app}.\n"
        'echo "data-verify: confirm row counts and integrity after import"\n'
    )


def _generated_contents(
    app: str,
    template: Template,
    environment: str,
    owner: str,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, str]:
    """Map of relative path -> file content for the full scaffold."""
    contents: Dict[str, str] = {
        ".ophelia.yml": _manifest_text(app, template, environment, owner, runtime_root),
        "ophelia/agent.md": _agent_md(app, template),
        "ophelia/runbook.md": _runbook_md(app, template),
        "ophelia/checks/smoke.sh": _smoke_sh(app, template),
        ".github/workflows/ophelia-staging.yml": _staging_workflow(app, template),
        ".github/workflows/ophelia-release.yml": _release_workflow(app, template),
        ".github/pull_request_template.md": _pull_request_template(),
        ".github/dependabot.yml": _dependabot_yml(),
        ".github/labeler.yml": _labeler_yml(),
    }
    if template.is_static:
        contents["public/index.html"] = _static_index_html(app)
    if "ophelia/hooks/pre-export.sh" in template.extra_files:
        contents["ophelia/hooks/pre-export.sh"] = _pre_export_sh(app)
    if "ophelia/checks/data-verify.sh" in template.extra_files:
        contents["ophelia/checks/data-verify.sh"] = _data_verify_sh(app)
    return contents


def _static_index_html(app: str) -> str:
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\">\n"
        "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"    <title>{app}</title>\n"
        "  </head>\n"
        "  <body>\n"
        f"    <main><h1>{app}</h1></main>\n"
        "  </body>\n"
        "</html>\n"
    )


_FILE_DESCRIPTIONS: Dict[str, str] = {
    ".ophelia.yml": "Ophelia deploy manifest (the deploy contract).",
    "ophelia/agent.md": "Agent guide: release model and required secrets.",
    "ophelia/runbook.md": "Operator runbook: deploy, release, rollback steps.",
    "ophelia/checks/smoke.sh": "Post-deploy smoke check.",
    ".github/workflows/ophelia-staging.yml": "Deploy to staging on merge to next.",
    ".github/workflows/ophelia-release.yml": "Cut a production release on merge to master.",
    ".github/pull_request_template.md": "PR template prompting for a release label.",
    ".github/dependabot.yml": "Dependabot config for actions and docker updates.",
    ".github/labeler.yml": "Path-to-label mapping for the labeler action.",
    "ophelia/hooks/pre-export.sh": "Pre-export quiesce hook for critical data.",
    "ophelia/checks/data-verify.sh": "Post-import data verification hook.",
}


# ---------------------------------------------------------------------------
# GitHub provisioning descriptor and apply support
# ---------------------------------------------------------------------------


GithubCommandRunner = Callable[[Dict[str, Any], float], subprocess.CompletedProcess[str]]


def _release_label_policy() -> Dict[str, Any]:
    return {
        "required_for_production": True,
        "valid_labels": list(RELEASE_LABELS),
        "branch": "master",
        "note": "A production release into master requires exactly one release label.",
    }


def _deployment_environments(template: Optional[Template] = None) -> List[Dict[str, Any]]:
    required_secrets = list(template.requires_secrets) if template is not None else list(DEPLOY_SECRETS) + list(REGISTRY_SECRETS)
    return [
        {
            "name": "staging",
            "branch": "next",
            "required_secrets": list(required_secrets),
        },
        {
            "name": "production",
            "branch": "master",
            "required_secrets": list(required_secrets),
            "release_label_required": True,
        },
    ]


def _github_provisioning_commands(repo: str, branch_policy: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Typed ``gh`` commands used by plans and by the token-gated apply path."""
    commands: List[Dict[str, Any]] = [
        {
            "id": "create_repo",
            "provider": "gh",
            "description": f"Create the private GitHub repository {repo}.",
            "argv": ["gh", "repo", "create", repo, "--private"],
            "executed": False,
        },
        {
            "id": "create_staging_environment",
            "provider": "gh",
            "description": "Create the staging deployment environment.",
            "argv": ["gh", "api", "-X", "PUT", f"repos/{repo}/environments/staging"],
            "executed": False,
        },
        {
            "id": "create_production_environment",
            "provider": "gh",
            "description": "Create the production deployment environment.",
            "argv": ["gh", "api", "-X", "PUT", f"repos/{repo}/environments/production"],
            "executed": False,
        },
    ]
    for branch, policy in branch_policy.items():
        commands.append(
            {
                "id": f"protect_{branch}",
                "provider": "gh",
                "description": f"Protect the {branch} branch (reviews + status checks).",
                "argv": [
                    "gh",
                    "api",
                    "--method",
                    "PUT",
                    f"repos/{repo}/branches/{branch}/protection",
                    "--input",
                    "-",
                ],
                "stdin_json": {
                    "required_status_checks": {
                        "strict": True,
                        "contexts": list(policy.get("required_status_checks") or []),
                    },
                    "enforce_admins": True,
                    "required_pull_request_reviews": {
                        "required_approving_review_count": int(policy.get("required_reviews") or REQUIRED_REVIEW_COUNT),
                    },
                    "restrictions": None,
                },
                "requires_existing_branch": True,
                "executed": False,
            }
        )
    return commands


def _github_settings(app: str, owner: str, template: Template) -> Dict[str, Any]:
    """Repo settings + branch policy + provisioning command list (described).

    This is a description for an operator/CI or for ``github_provision_apply``.
    This helper never calls the GitHub API or ``gh``.
    """
    required_status_checks = ["validate", "tests"]
    branch_policy = {
        "next": {
            "deploys_to": "staging",
            "required_reviews": REQUIRED_REVIEW_COUNT,
            "required_status_checks": list(required_status_checks),
            "release_label_required": False,
        },
        "master": {
            "deploys_to": "production",
            "required_reviews": REQUIRED_REVIEW_COUNT,
            "required_status_checks": list(required_status_checks),
            "release_label_required": True,
        },
    }
    repo = f"{owner}/{app}"
    provisioning_commands = _github_provisioning_commands(repo, branch_policy)
    for command in provisioning_commands:
        command["gh"] = " ".join(str(token) for token in command["argv"])
    return {
        "repo": repo,
        "visibility": "private",
        "default_branch": "next",
        "branch_policy": branch_policy,
        "required_review_count": REQUIRED_REVIEW_COUNT,
        "required_status_checks": list(required_status_checks),
        "release_label_validation": list(RELEASE_LABELS),
        "environments": list(DEPLOYMENT_ENVIRONMENTS),
        "required_secrets": list(template.requires_secrets),
        "provisioning": {
            "executed": False,
            "note": "Described only. Apply with an operator/CI; no GitHub mutation happened here.",
            "commands": provisioning_commands,
        },
    }


# ---------------------------------------------------------------------------
# Release metadata contract
# ---------------------------------------------------------------------------


def release_metadata(
    app: str,
    environment: str,
    version: str,
    git_sha: str,
    image: str,
    image_digest: Optional[str] = None,
    manifest_path: str = ".ophelia.yml",
    release_notes: str = "",
    previous_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce the ``ophelia.release_metadata`` contract shape."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ophelia.release_metadata",
        "app": app,
        "environment": environment,
        "version": version,
        "git_sha": git_sha,
        "image": image,
        "image_digest": image_digest,
        "manifest_path": manifest_path,
        "release_notes": release_notes,
        "rollback": {"previous_version": previous_version},
    }


# ---------------------------------------------------------------------------
# Plan / apply
# ---------------------------------------------------------------------------


def _canonical_apply_input(
    app: str, template: str, owner: str, environment: str, runtime_root: Path
) -> Dict[str, Any]:
    """The canonical input the confirmation token is derived from."""
    return {
        "app": app,
        "template": template,
        "owner": owner,
        "environment": environment,
        "runtime_root": str(Path(runtime_root).expanduser()),
    }


def _canonical_github_apply_input(
    app: str,
    template: str,
    owner: str,
    environment: str,
    repo: str,
    phase: str,
    runtime_root: Path,
    github_provider: str,
    provider_config: Optional[Path],
) -> Dict[str, Any]:
    return {
        "app": app,
        "template": template,
        "owner": owner,
        "environment": environment,
        "repo": repo,
        "phase": phase,
        "github_provider": github_provider,
        "provider_config": str(provider_config) if provider_config is not None else None,
        "runtime_root": str(Path(runtime_root).expanduser()),
    }


def github_provision_plan(
    app: str,
    template: str,
    owner: str = "personal",
    environment: str = "production",
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    repo: Optional[str] = None,
    phase: str = "all",
    github_provider: str = "auto",
    provider_config: Optional[Path] = None,
) -> Dict[str, Any]:
    """Plan token-gated GitHub repo/environment/branch protection provisioning."""
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    template_obj = TEMPLATES.get(template)

    if template_obj is None:
        blockers.append(issue("unknown_template", f"Unknown template `{template}`. Run `ship app templates list`."))
    if environment not in {"dev", "staging", "production"}:
        blockers.append(issue("invalid_environment", "`environment` must be dev, staging, or production."))
    if phase not in {"all", "repo", "environments", "protection"}:
        blockers.append(issue("invalid_github_phase", "`phase` must be all, repo, environments, or protection."))
    if github_provider not in GITHUB_PROVIDERS:
        blockers.append(issue("invalid_github_provider", "`github_provider` must be auto, gh, or github-app."))
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", app or ""):
        blockers.append(
            issue(
                "invalid_app_name",
                "`app` must be a DNS-safe label: lowercase letters, digits, and hyphens, not starting or ending with a hyphen.",
            )
        )

    repo_name = repo or f"{owner}/{app}"
    if not _valid_github_repo(repo_name):
        blockers.append(issue("invalid_github_repo", "`repo` must be in OWNER/REPO form using GitHub-safe name characters."))

    if blockers or template_obj is None:
        return plan_envelope(
            operation="app.github.provision",
            app=app,
            environment=environment,
            summary=f"Cannot plan GitHub provisioning for {app}: {len(blockers)} blocker(s).",
            blockers=blockers,
            warnings=warnings,
            checks=[],
            artifacts=[],
            confirmation_required=False,
            confirmation_token=None,
            exact_apply_input=None,
            risk="high",
            kind="ophelia.github_provision_plan",
            repo=repo_name,
            phase=phase,
            commands=[],
            github=None,
        )

    github = _github_settings(app, owner, template_obj)
    github["repo"] = repo_name
    branch_policy = github.get("branch_policy") if isinstance(github.get("branch_policy"), dict) else {}
    selected_provider, provider_status = resolve_github_provider(
        github_provider,
        runtime_root=runtime_root,
        config_path=provider_config,
    )
    provider_blockers = provider_status.get("blockers") if isinstance(provider_status.get("blockers"), list) else []
    for provider_blocker in provider_blockers:
        if isinstance(provider_blocker, dict):
            warnings.append(
                issue(
                    str(provider_blocker.get("code") or "github_provider_not_ready"),
                    str(provider_blocker.get("message") or "GitHub provider is not ready; apply may block."),
                    str(provider_blocker.get("path")) if provider_blocker.get("path") else None,
                )
            )
    provider_warnings = provider_status.get("warnings") if isinstance(provider_status.get("warnings"), list) else []
    warnings.extend(provider_warnings)
    if selected_provider == "github_app":
        commands = _select_github_phase(github_app_operations(repo_name, branch_policy), phase)
    else:
        commands = _select_github_phase(_github_provisioning_commands(repo_name, branch_policy), phase)
        for command in commands:
            command["gh"] = " ".join(str(token) for token in command["argv"])
    github["provider"] = selected_provider
    github["provider_status"] = provider_status

    if phase in {"all", "protection"}:
        warnings.append(
            issue(
                "github_branch_protection_requires_branches",
                "Branch protection steps require `next` and `master` to already exist on GitHub.",
            )
        )

    apply_input = _canonical_github_apply_input(
        app,
        template,
        owner,
        environment,
        repo_name,
        phase,
        runtime_root,
        selected_provider,
        provider_config,
    )
    confirmation_token = token("app.github.provision.apply", apply_input)
    return plan_envelope(
        operation="app.github.provision",
        app=app,
        environment=environment,
        summary=f"Provision GitHub repo {repo_name} for {app}: {len(commands)} command(s) planned.",
        blockers=[],
        warnings=warnings,
        checks=[
            {"name": "github_repo_valid", "ok": True, "message": f"Repository target is {repo_name}."},
            {
                "name": "github_provider_selected",
                "ok": True,
                "message": f"Selected GitHub provider is {selected_provider}.",
            },
            {"name": "commands_typed", "ok": True, "message": "Provisioning uses typed provider operations, never shell strings."},
        ],
        artifacts=[],
        confirmation_required=True,
        confirmation_token=confirmation_token,
        exact_apply_input=apply_input,
        risk="high",
        kind="ophelia.github_provision_plan",
        repo=repo_name,
        phase=phase,
        commands=commands,
        github_provider=selected_provider,
        provider_status=provider_status,
        github=github,
        required_secrets=list(template_obj.requires_secrets),
    )


def github_provision_apply(
    app: str,
    template: str,
    confirm: str,
    owner: str = "personal",
    environment: str = "production",
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    repo: Optional[str] = None,
    phase: str = "all",
    github_provider: str = "auto",
    provider_config: Optional[Path] = None,
    timeout: float = 60.0,
    command_runner: Optional[GithubCommandRunner] = None,
) -> Dict[str, Any]:
    """Run the token-gated GitHub provisioning plan with ``gh``."""
    started_at = utc_now()
    plan = github_provision_plan(
        app=app,
        template=template,
        owner=owner,
        environment=environment,
        runtime_root=runtime_root,
        repo=repo,
        phase=phase,
        github_provider=github_provider,
        provider_config=provider_config,
    )
    blockers: List[Dict[str, Any]] = list(plan.get("blockers", [])) if isinstance(plan.get("blockers"), list) else []
    warnings: List[Dict[str, Any]] = list(plan.get("warnings", [])) if isinstance(plan.get("warnings"), list) else []
    apply_input = plan.get("exact_apply_input")

    if not isinstance(apply_input, dict):
        blockers.append(issue("invalid_plan", "GitHub provisioning plan is missing `exact_apply_input`."))
    else:
        expected = token("app.github.provision.apply", apply_input)
        if not confirm:
            blockers.append(issue("confirmation_token_missing", "GitHub provisioning apply requires a confirmation token from the plan."))
        elif confirm != expected:
            blockers.append(issue("confirmation_token_mismatch", "Confirmation token does not match the GitHub provisioning plan input."))

    selected_provider = str(plan.get("github_provider") or "gh")
    if selected_provider == "gh" and command_runner is None and shutil.which("gh") is None:
        blockers.append(issue("github_cli_missing", "`gh` is required for GitHub provisioning apply."))
    if selected_provider == "github_app" and command_runner is None:
        blockers.append(
            issue(
                "github_app_runner_missing",
                "GitHub App provider selected, but no live GitHub App runner is configured in this phase.",
            )
        )

    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    if blockers:
        return _github_provision_receipt(
            app=app,
            environment=environment,
            status="blocked",
            started_at=started_at,
            runtime_root=runtime_root,
            plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
            repo=str(plan.get("repo") or repo or ""),
            phase=phase,
            provider=selected_provider,
            steps=[],
            blockers=blockers,
            warnings=warnings,
        )

    runner = command_runner or _default_github_command_runner
    steps: List[Dict[str, Any]] = []
    status = "succeeded"
    for command in commands:
        if not isinstance(command, dict):
            continue
        step = {
            "id": command.get("id"),
            "description": command.get("description"),
            "status": "planned",
            "executed": False,
            "return_code": None,
        }
        try:
            result = runner(command, _bounded_github_timeout(timeout))
        except subprocess.TimeoutExpired:
            step.update({"status": "failed", "executed": True, "error_type": "TimeoutExpired"})
            blockers.append(issue("github_command_timeout", f"GitHub command `{command.get('id')}` timed out."))
            status = "failed"
        except FileNotFoundError:
            step.update({"status": "failed", "executed": False, "error_type": "FileNotFoundError"})
            blockers.append(issue("github_cli_missing", "`gh` was not found while applying GitHub provisioning."))
            status = "failed"
        except Exception as exc:  # noqa: BLE001 - external command errors must become receipts
            step.update({"status": "failed", "executed": True, "error_type": type(exc).__name__})
            blockers.append(issue("github_command_failed", f"GitHub command `{command.get('id')}` failed ({type(exc).__name__})."))
            status = "failed"
        else:
            step.update({"executed": True, "return_code": int(result.returncode)})
            if result.returncode == 0:
                step["status"] = "succeeded"
            else:
                step["status"] = "failed"
                blockers.append(issue("github_command_failed", f"GitHub command `{command.get('id')}` exited with status {result.returncode}."))
                status = "failed"
        steps.append(step)
        if status == "failed":
            remaining = [item for item in commands[len(steps):] if isinstance(item, dict)]
            for skipped in remaining:
                steps.append(
                    {
                        "id": skipped.get("id"),
                        "description": skipped.get("description"),
                        "status": "skipped",
                        "executed": False,
                        "return_code": None,
                    }
                )
            break

    return _github_provision_receipt(
        app=app,
        environment=environment,
        status=status,
        started_at=started_at,
        runtime_root=runtime_root,
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        repo=str(plan.get("repo") or ""),
        phase=phase,
        provider=selected_provider,
        steps=steps,
        blockers=blockers,
        warnings=warnings,
    )


def _valid_github_repo(repo: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""))


def _select_github_phase(commands: List[Dict[str, Any]], phase: str) -> List[Dict[str, Any]]:
    if phase == "all":
        return list(commands)
    if phase == "repo":
        return [command for command in commands if command.get("id") == "create_repo"]
    if phase == "environments":
        return [command for command in commands if str(command.get("id") or "").startswith("create_") and "environment" in str(command.get("id") or "")]
    if phase == "protection":
        return [command for command in commands if str(command.get("id") or "").startswith("protect_")]
    return []


def _default_github_command_runner(command: Dict[str, Any], timeout: float) -> subprocess.CompletedProcess[str]:
    argv = command.get("argv") if isinstance(command.get("argv"), list) else []
    argv_tokens = [str(token) for token in argv]
    stdin_json = command.get("stdin_json")
    stdin = json.dumps(stdin_json, separators=(",", ":")) if isinstance(stdin_json, dict) else None
    return subprocess.run(
        argv_tokens,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _bounded_github_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 60.0
    if timeout <= 0:
        return 60.0
    return min(timeout, 600.0)


def _github_provision_receipt(
    *,
    app: str,
    environment: str,
    status: str,
    started_at: str,
    runtime_root: Path,
    plan_operation_id: Optional[str],
    repo: str,
    phase: str,
    provider: str,
    steps: List[Dict[str, Any]],
    blockers: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    succeeded = sum(1 for step in steps if step.get("status") == "succeeded")
    failed = sum(1 for step in steps if step.get("status") == "failed")
    skipped = sum(1 for step in steps if step.get("status") == "skipped")
    receipt = receipt_envelope(
        operation="app.github.provision.apply",
        app=app,
        environment=environment,
        status=status,
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=[],
        checks=[
            {
                "name": "github_commands",
                "ok": failed == 0 and status == "succeeded",
                "message": f"{succeeded}/{len(steps)} GitHub command(s) succeeded.",
            }
        ],
        rollback={
            "available": False,
            "note": "GitHub repository/environment/protection changes must be reverted in GitHub if needed.",
        },
        plan_operation_id=plan_operation_id,
        repo=repo,
        phase=phase,
        github_provider=provider,
        steps=steps,
        step_counts={"total": len(steps), "succeeded": succeeded, "failed": failed, "skipped": skipped},
        blockers=blockers,
        warnings=warnings,
        summary=(
            f"GitHub provisioning {status} for {repo}: "
            f"{succeeded} succeeded, {failed} failed, {skipped} skipped."
        ),
    )
    receipt_path = runtime_root / "github" / "provisioning" / f"{receipt['operation_id']}.json"
    app_receipt_path = runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json"
    receipt["artifacts"] = [
        artifact(str(receipt_path), "github-provision-receipt", "GitHub provisioning receipt.", present=True),
        artifact(str(app_receipt_path), "receipt", "App receipt timeline entry.", present=True),
    ]
    redacted = deep_redact(receipt, safe_keys={"inputs_redacted"}, propagate=True)
    try:
        _write_json(receipt_path, redacted)
        _write_json(app_receipt_path, redacted)
    except OSError:
        redacted.setdefault("warnings", []).append(
            issue("github_receipt_write_failed", f"Could not write GitHub provisioning receipt under {runtime_root}.")
        )
    return redacted


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def create_plan(
    app: str,
    template: str,
    owner: str = "personal",
    environment: str = "production",
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Plan an app scaffold. **Writes nothing**; pure in-memory computation."""
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    template_obj = TEMPLATES.get(template)
    if template_obj is None:
        blockers.append(
            issue(
                "unknown_template",
                f"Unknown template `{template}`. Run `ship app templates list`.",
            )
        )

    if environment not in {"dev", "staging", "production"}:
        blockers.append(
            issue("invalid_environment", "`environment` must be dev, staging, or production.")
        )

    # The app id flows into generated domains, image tags, and (hyphen->underscore)
    # database names, so it must be a DNS-safe label. This also rejects `/`/`..`.
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", app or ""):
        blockers.append(
            issue(
                "invalid_app_name",
                "`app` must be a DNS-safe label: lowercase letters, digits, and hyphens, "
                "not starting or ending with a hyphen.",
            )
        )

    release_label_policy = _release_label_policy()
    deployment_environments = _deployment_environments(template_obj)

    # Production path requires a release label policy. Surface a blocker when a
    # production plan lacks one (it is always present here, but the check makes
    # the contract explicit and testable, and guards against a removed policy).
    if environment == "production" and not release_label_policy.get("valid_labels"):
        blockers.append(
            issue(
                "release_label_required",
                "Production releases require a release-label policy (release:patch|minor|major).",
            )
        )

    if blockers:
        return plan_envelope(
            operation="app.create",
            app=app,
            environment=environment,
            summary=f"Cannot scaffold {app}: {len(blockers)} blocker(s).",
            blockers=blockers,
            warnings=warnings,
            checks=[],
            artifacts=[],
            confirmation_required=False,
            confirmation_token=None,
            exact_apply_input=None,
            risk="low",
            kind="ophelia.app_create_plan",
            github=None,
            files=[],
            manifest_preview=None,
            release_label_policy=release_label_policy,
            deployment_environments=deployment_environments,
            rollback=None,
        )

    assert template_obj is not None
    contents = _generated_contents(app, template_obj, environment, owner, runtime_root)
    manifest_text = contents[".ophelia.yml"]

    # Validate the generated manifest in memory by loading it from a throwaway
    # temp directory. This must NOT touch the target workdir, the runtime root,
    # or the repo. We use the OS temp dir.
    manifest_dict = _load_generated_manifest_dict(manifest_text)

    files = [
        {"path": path, "description": _FILE_DESCRIPTIONS.get(path, "Generated file.")}
        for path in _generated_paths(template_obj)
    ]

    github = _github_settings(app, owner, template_obj)

    rollback = {
        "model": "github-release",
        "note": "Roll back by re-running the previous release or `ship rollback` with the prior release id.",
        "previous_version": None,
    }

    apply_input = _canonical_apply_input(app, template, owner, environment, runtime_root)
    confirmation_token = token("app.create.apply", apply_input)

    checks = [
        {"name": "manifest_valid", "ok": True, "message": "Generated manifest loads via load_manifest."},
        {
            "name": "release_label_policy",
            "ok": bool(release_label_policy.get("valid_labels")),
            "message": "Production releases are gated by a release label.",
        },
        {"name": "no_github_mutation", "ok": True, "message": "GitHub provisioning is described only."},
    ]

    return plan_envelope(
        operation="app.create",
        app=app,
        environment=environment,
        summary=(
            f"Scaffold {app} from the {template} template "
            f"({len(files)} file(s)); apply writes only under your --target-dir."
        ),
        blockers=[],
        warnings=warnings,
        checks=checks,
        artifacts=[
            artifact(path, "scaffold-file", _FILE_DESCRIPTIONS.get(path), present=False)
            for path in _generated_paths(template_obj)
        ],
        confirmation_required=True,
        confirmation_token=confirmation_token,
        exact_apply_input=apply_input,
        risk="low",
        kind="ophelia.app_create_plan",
        template=template,
        owner=owner,
        files=files,
        manifest_preview={"text": manifest_text, "parsed": manifest_dict},
        github=github,
        release_label_policy=release_label_policy,
        deployment_environments=deployment_environments,
        rollback=rollback,
    )


def _load_generated_manifest_dict(manifest_text: str) -> Dict[str, Any]:
    """Validate the generated manifest in memory and return its raw YAML dict.

    Uses a throwaway temp file so :func:`load_manifest` can validate it (this
    raises if the manifest is invalid). The returned dict is the *raw YAML*
    parse of the manifest text, which is what the manifest JSON Schema is built
    to validate (absent optional fields rather than null-materialized ones). The
    temp file lives in the OS temp dir (never the target workdir, runtime root,
    or repo) and is removed immediately.
    """
    import tempfile

    import yaml  # type: ignore

    with tempfile.TemporaryDirectory() as temp_dir:
        # Source-path checks run for non-.json manifests, but our generated
        # manifests reference no local env_files or mounts, so validation
        # passes. load_manifest raises ManifestError on any invalid manifest.
        path = Path(temp_dir) / "preview.ophelia.yml"
        path.write_text(manifest_text)
        load_manifest(path)
    parsed = yaml.safe_load(manifest_text)
    if not isinstance(parsed, dict):  # defensive; load_manifest already enforces this
        raise ManifestError("Generated manifest did not parse to a mapping.")
    return parsed


def _resolve_target_dir(target_dir: Path, runtime_root: Path) -> Path:
    """Resolve and validate a target dir is outside runtime root and repo tree."""
    resolved = target_dir.expanduser().resolve()
    forbidden_roots = [runtime_root.expanduser().resolve(), REPO_ROOT.resolve()]
    for forbidden in forbidden_roots:
        if resolved == forbidden or _is_relative_to(resolved, forbidden):
            raise _UnsafeTargetError(
                f"Refusing to scaffold into `{resolved}`: it resolves inside a protected tree (`{forbidden}`)."
            )
    return resolved


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


class _UnsafeTargetError(Exception):
    """Raised when a target_dir resolves inside a protected tree."""


def create_apply(
    plan: Dict[str, Any],
    confirm: str,
    target_dir: Path,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    force: bool = False,
) -> Dict[str, Any]:
    """Write the scaffold files only under ``target_dir``, token-gated.

    Recomputes the confirmation token from the plan's canonical apply input and
    rejects a mismatch. Refuses any ``target_dir`` inside the runtime root or
    the repo source tree. No GitHub call is ever made.
    """
    started_at = utc_now()
    app = str(plan.get("app") or "unknown")
    environment = str(plan.get("environment") or "unknown")

    apply_input = plan.get("exact_apply_input")
    blockers: List[Dict[str, Any]] = []

    if not isinstance(apply_input, dict):
        blockers.append(
            issue("invalid_plan", "Plan is missing `exact_apply_input`; re-run `ship app create plan`.")
        )
        return _blocked_receipt(app, environment, started_at, blockers)

    expected = token("app.create.apply", apply_input)
    if not isinstance(confirm, str) or not confirm:
        blockers.append(
            issue("confirmation_token_missing", "App create apply requires a confirmation token from the plan.")
        )
    elif confirm != expected:
        blockers.append(
            issue("confirmation_token_mismatch", "Confirmation token does not match the plan input.")
        )

    if blockers:
        return _blocked_receipt(app, environment, started_at, blockers)

    # Everything written must come from the token-bound apply input, so a tampered
    # top-level plan `app`/`environment` cannot change the files that get generated.
    app = str(apply_input.get("app") or app)
    environment = str(apply_input.get("environment") or environment)
    template_name = str(apply_input.get("template"))
    owner = str(apply_input.get("owner") or "personal")
    manifest_runtime_root = Path(str(apply_input.get("runtime_root") or runtime_root)).expanduser()
    template_obj = TEMPLATES.get(template_name)
    if template_obj is None:
        blockers.append(issue("unknown_template", f"Unknown template `{template_name}`."))
        return _blocked_receipt(app, environment, started_at, blockers)

    # Safe-target gate: refuse anything inside the runtime root or repo tree.
    try:
        resolved_target = _resolve_target_dir(target_dir, runtime_root)
    except _UnsafeTargetError as exc:
        blockers.append(issue("unsafe_target_dir", str(exc)))
        return _blocked_receipt(app, environment, started_at, blockers)

    contents = _generated_contents(app, template_obj, environment, owner, manifest_runtime_root)

    existing = sorted(
        relative_path
        for relative_path in contents
        if (resolved_target / relative_path).exists()
    )
    if existing and not force:
        blockers.append(
            issue(
                "target_file_exists",
                "Refusing to overwrite existing scaffold file(s); pass --force to replace them.",
                ", ".join(existing),
            )
        )
        return _blocked_receipt(app, environment, started_at, blockers)

    resolved_target.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for relative_path, body in contents.items():
        destination = resolved_target / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(body)
        if relative_path.endswith(".sh"):
            destination.chmod(0o755)
        written.append(relative_path)

    written.sort()

    # Write a receipt artifact under the target dir.
    receipt = receipt_envelope(
        operation="app.create.apply",
        app=app,
        environment=environment,
        status="succeeded",
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=[
            artifact(path, "scaffold-file", _FILE_DESCRIPTIONS.get(path), present=True)
            for path in written
        ],
        checks=[
            {"name": "confirmation_token", "ok": True, "message": "Matched the plan input."},
            {"name": "target_dir_safe", "ok": True, "message": f"Wrote only under `{resolved_target}`."},
            {"name": "no_github_mutation", "ok": True, "message": "No GitHub API or gh call was made."},
        ],
        rollback={
            "available": True,
            "note": f"Delete the scaffolded files under `{resolved_target}` to undo.",
        },
        plan_operation_id=plan.get("operation_id") if isinstance(plan.get("operation_id"), str) else None,
        target_dir=str(resolved_target),
        written_paths=written,
        template=template_name,
        force=force,
    )

    receipt_path = resolved_target / "ophelia" / "create-receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    receipt_path.write_text(_json.dumps(receipt, indent=2, sort_keys=True))
    if "ophelia/create-receipt.json" not in written:
        written.append("ophelia/create-receipt.json")
        written.sort()
        receipt["written_paths"] = written
        receipt["artifacts"].append(
            artifact("ophelia/create-receipt.json", "receipt", "Apply receipt.", present=True)
        )

    return receipt


def _blocked_receipt(
    app: str, environment: str, started_at: str, blockers: List[Dict[str, Any]]
) -> Dict[str, Any]:
    return receipt_envelope(
        operation="app.create.apply",
        app=app,
        environment=environment,
        status="blocked",
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=[],
        checks=[],
        rollback={"available": False, "note": "App create apply was blocked before writing any file."},
        blockers=blockers,
        written_paths=[],
    )
