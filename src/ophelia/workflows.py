"""Agent-native operation graphs (Phase 8).

This module builds *inspectable* operation graphs ("workflows") that an agent or
operator can read before doing anything. A workflow is a small DAG of
:class:`WorkflowNode` objects; each node names a real Ophelia operation (from the
command catalog) and the exact typed CLI argument *array* that would run it.

``plan_workflow`` produces and persists a graph without executing anything.
``run_workflow`` can later execute read-only graph nodes, pause at mutating
nodes until that node's own confirmation token is supplied, honor dependencies,
and record per-node progress in the stored graph plus a workflow receipt.

Node commands are always typed argument arrays (``List[str]``), never shell
strings, and never contain shell metacharacters. They are derived from the
canonical command-catalog descriptor for the node's operation (the human
``command`` string split into tokens) plus typed positional/flag tokens.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .command_catalog import CommandDescriptor, command_registry
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .operation_schema import (
    SCHEMA_VERSION,
    artifact,
    issue,
    operation_id,
    plan_envelope,
    receipt_envelope,
    utc_now,
)
from .operation_refs import public_resolution, resolve_workflow_ref
from .policy import policy_check_entry
from .redaction import deep_redact

WORKFLOW_PLAN_KIND = "ophelia.workflow_plan"
WORKFLOW_TEMPLATES_KIND = "ophelia.workflow_templates"
WORKFLOW_REPORT_KIND = "ophelia.workflow_report"
WORKFLOW_PREVIEW_KIND = "ophelia.workflow_preview"
WORKFLOW_RUN_KIND = "ophelia.workflow_run"
WORKFLOW_STATE_KIND = "ophelia.workflow_state"

WORKFLOW_NODE_STATUSES = (
    "planned",
    "previewed",
    "ready",
    "running",
    "paused_for_confirmation",
    "succeeded",
    "failed",
    "blocked",
    "skipped",
    "rolled_back",
    "cancelled",
)

# Tokens that must never appear inside a node command array. A typed argument
# array should be passed straight to ``argv`` without any shell, so the presence
# of any shell metacharacter is a defect we refuse to emit.
_SHELL_METACHARACTERS = ("&&", "||", "|", ";", "`", "$(", ">", "<", "\n", "&")
_PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DEFAULT_NODE_TIMEOUT_SECONDS = 300.0

CommandRunner = Callable[[List[str], float], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class WorkflowNode:
    """One node in an operation graph.

    ``command`` is a typed argument array (e.g.
    ``["ship", "app", "readiness", "<app>", "--environment", "<env>", "--json"]``)
    and never a shell string. ``mutates_state`` mirrors the catalog descriptor for
    ``operation``; mutating nodes are confirmation-gated and are never run until
    the operator supplies a token for that exact node id.
    """

    id: str
    operation: str
    command: List[str]
    depends_on: List[str]
    mutates_state: bool
    requires_confirmation: bool = False
    plan_command: Optional[str] = None
    apply_command: Optional[str] = None
    status: str = "planned"
    blockers: List[Any] = field(default_factory=list)
    warnings: List[Any] = field(default_factory=list)
    artifacts: List[Any] = field(default_factory=list)
    rollback: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    plan_id: Optional[str] = None
    plan_operation_id: Optional[str] = None
    receipt_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "command": list(self.command),
            "depends_on": list(self.depends_on),
            "mutates_state": self.mutates_state,
            "requires_confirmation": self.requires_confirmation,
            "plan_command": self.plan_command,
            "apply_command": self.apply_command,
            "status": self.status,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "artifacts": list(self.artifacts),
            "rollback": dict(self.rollback) if isinstance(self.rollback, dict) else None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "plan_id": self.plan_id,
            "plan_operation_id": self.plan_operation_id,
            "receipt_id": self.receipt_id,
        }


def _registry_by_operation() -> Dict[str, CommandDescriptor]:
    return {descriptor.operation: descriptor for descriptor in command_registry()}


def _node_command(operation: str, *, context: Dict[str, str], env_flags: List[str]) -> List[str]:
    """Exact runnable argv for a move-app node operation.

    The action/catalog operation id remains the graph contract, but the command
    must match the actual CLI parser. Optional graph inputs that are not known at
    plan time are represented as shell-safe TOKEN_CASE placeholders.
    """
    templates: Dict[str, List[str]] = {
        "manifest.validate": ["ship", "validate", "MANIFEST_PATH"],
        "manifest.explain": ["ship", "explain", "MANIFEST_PATH"],
        "manifest.diff": ["ship", "diff", "MANIFEST_PATH"],
        "pack.validate": ["ship", "pack", "validate", "MANIFEST_PATH"],
        "pack.explain": ["ship", "pack", "explain", "MANIFEST_PATH"],
        "deploy.plan": ["ship", "deploy", "MANIFEST_PATH", "--plan"],
        "runtime.status": ["ship", "status"],
        "runtime.doctor": ["ship", "doctor"],
        "runtime.drift": ["ship", "drift", "MANIFEST_PATH"],
        "providers.validate": ["ship", "providers", "validate", "--config", "PROVIDER_CONFIG"],
        "secrets.audit": ["ship", "secrets", "audit", "{app}", *env_flags],
        "manifest.conflicts": ["ship", "inspect", "conflicts", "--manifest-dir", "MANIFEST_DIR"],
        "backup.status": ["ship", "backup", "status", "{app}", *env_flags],
        "backup.verify.plan": ["ship", "backup", "verify", "plan", "{app}", *env_flags, "--manifest", "MANIFEST_PATH"],
        "backup.verify.apply": [
            "ship",
            "backup",
            "verify",
            "apply",
            "{app}",
            *env_flags,
            "--manifest",
            "MANIFEST_PATH",
            "--confirm",
            "CONFIRMATION_TOKEN",
        ],
        "app.readiness": ["ship", "app", "readiness", "{app}", *env_flags],
        "app.runbook": ["ship", "app", "runbook", "{app}", *env_flags],
        "app.traffic.status": ["ship", "traffic", "status", "--app", "{app}", *env_flags],
        "app.templates.explain": ["ship", "app", "templates", "explain", "{template}"],
        "app.export.plan": ["ship", "app", "export", "plan", "{app}", *env_flags],
        "app.export.create": ["ship", "app", "export", "create", "{app}", *env_flags, "--confirm", "CONFIRMATION_TOKEN"],
        "app.import.plan": ["ship", "app", "import", "plan", "EXPORT_BUNDLE"],
        "app.import.apply": ["ship", "app", "import", "apply", "EXPORT_BUNDLE", "--confirm", "CONFIRMATION_TOKEN"],
        "app.restore-drill.plan": ["ship", "app", "restore-drill", "plan", "{app}", "--source", "EXPORT_BUNDLE", *env_flags],
        "app.restore-drill.apply": [
            "ship",
            "app",
            "restore-drill",
            "apply",
            "{app}",
            "--source",
            "EXPORT_BUNDLE",
            *env_flags,
            "--confirm",
            "CONFIRMATION_TOKEN",
        ],
        "app.traffic.plan": [
            "ship",
            "app",
            "traffic",
            "plan",
            "{app}",
            "--from",
            "{source}",
            "--to",
            "{target}",
            "--target-origin",
            "{target_origin}",
            *env_flags,
        ],
        "app.traffic.apply": [
            "ship",
            "app",
            "traffic",
            "apply",
            "{app}",
            "--from",
            "{source}",
            "--to",
            "{target}",
            "--target-origin",
            "{target_origin}",
            *env_flags,
            "--confirm",
            "CONFIRMATION_TOKEN",
        ],
        "app.github.provision.plan": [
            "ship",
            "app",
            "github",
            "plan",
            "--app",
            "{app}",
            "--template",
            "{template}",
            "--owner",
            "{owner}",
            "--repo",
            "{repo}",
            "--phase",
            "{phase}",
            *env_flags,
        ],
        "app.github.provision.apply": [
            "ship",
            "app",
            "github",
            "apply",
            "--app",
            "{app}",
            "--template",
            "{template}",
            "--owner",
            "{owner}",
            "--repo",
            "{repo}",
            "--phase",
            "{phase}",
            *env_flags,
            "--confirm",
            "CONFIRMATION_TOKEN",
        ],
        "observability.plan": ["ship", "observability", "plan", "--app", "{app}", *env_flags, "--manifest", "MANIFEST_PATH"],
        "observability.status": ["ship", "observability", "status", "--app", "{app}", *env_flags, "--manifest", "MANIFEST_PATH"],
        "observability.schedule.run": ["ship", "observability", "schedule", "run"],
        "receipts.timeline": ["ship", "receipts", "timeline", "--app", "{app}", *env_flags],
        "restore.drills.list": ["ship", "restore-drills", "list", "--app", "{app}", *env_flags],
    }
    tokens = templates.get(operation, ["ship", *operation.split(".")])
    return [context.get(token[1:-1], token) if token.startswith("{") and token.endswith("}") else token for token in [*tokens, "--json"]]


def command_has_shell_metacharacters(command: List[str]) -> bool:
    """True if any token in a command array contains a shell metacharacter."""
    return any(any(meta in token for meta in _SHELL_METACHARACTERS) for token in command)


def _workflow_node(
    *,
    node_id: str,
    operation: str,
    depends_on: List[str],
    context: Dict[str, str],
    env_flags: List[str],
    registry: Dict[str, CommandDescriptor],
) -> WorkflowNode:
    descriptor = registry.get(operation)
    mutates_state = bool(descriptor.mutates_state) if descriptor is not None else False
    requires_confirmation = bool(descriptor.requires_confirmation) if descriptor is not None else mutates_state
    return WorkflowNode(
        id=node_id,
        operation=operation,
        command=_node_command(operation, context=context, env_flags=env_flags),
        depends_on=list(depends_on),
        mutates_state=mutates_state,
        requires_confirmation=requires_confirmation,
        plan_command=descriptor.plan_command if descriptor is not None else None,
        apply_command=descriptor.apply_command if descriptor is not None else None,
    )


def _build_move_app_nodes(
    *,
    app: str,
    environment: Optional[str],
    source: Optional[str],
    target: Optional[str],
    target_origin: Optional[str],
    registry: Dict[str, CommandDescriptor],
    **_: Any,
) -> List[WorkflowNode]:
    """Build the ``move-app`` node graph in dependency order.

    Mutability is sourced from the catalog descriptor. Mutating nodes remain in
    the graph, carry their own plan/apply metadata, and pause until confirmed.
    """
    env_flags = ["--environment", environment] if environment else []
    # Placeholders are explicit when an optional input was not provided, so the
    # graph stays inspectable and the missing input is obvious to a human/agent.
    # They are bracket-free on purpose: a node command must never contain a shell
    # metacharacter, and `<`/`>` are metacharacters, so we use TOKEN_CASE markers.
    source_value = source or "SOURCE_HOST"
    target_value = target or "TARGET_HOST"
    target_origin_value = target_origin or "TARGET_ORIGIN"

    context = {
        "app": app,
        "source": source_value,
        "target": target_value,
        "target_origin": target_origin_value,
    }

    specs: List[Dict[str, Any]] = [
        {
            "id": "validate-manifest",
            "operation": "manifest.validate",
            "depends_on": [],
        },
        {
            "id": "validate-provider-config",
            "operation": "providers.validate",
            "depends_on": ["validate-manifest"],
        },
        {
            "id": "audit-secrets",
            "operation": "secrets.audit",
            "depends_on": ["validate-provider-config"],
        },
        {
            "id": "check-route-conflicts",
            "operation": "manifest.conflicts",
            "depends_on": ["audit-secrets"],
        },
        {
            "id": "check-backups",
            "operation": "backup.status",
            "depends_on": ["check-route-conflicts"],
        },
        {
            "id": "readiness",
            "operation": "app.readiness",
            "depends_on": ["check-backups"],
        },
        {
            "id": "export-plan",
            "operation": "app.export.plan",
            "depends_on": ["readiness"],
        },
        {
            "id": "export-create",
            "operation": "app.export.create",
            "depends_on": ["export-plan"],
        },
        {
            "id": "import-plan",
            # `app.import.plan` takes a positional export-bundle source produced
            # by the upstream export-plan/create step, not a host id.
            "operation": "app.import.plan",
            "depends_on": ["export-create"],
        },
        {
            "id": "import-apply",
            "operation": "app.import.apply",
            "depends_on": ["import-plan"],
        },
        {
            "id": "restore-drill-plan",
            "operation": "app.restore-drill.plan",
            "depends_on": ["import-apply"],
        },
        {
            "id": "restore-drill-apply",
            "operation": "app.restore-drill.apply",
            "depends_on": ["restore-drill-plan"],
        },
        {
            "id": "traffic-plan",
            "operation": "app.traffic.plan",
            "depends_on": ["restore-drill-apply"],
        },
        {
            "id": "traffic-apply",
            "operation": "app.traffic.apply",
            "depends_on": ["traffic-plan"],
        },
    ]

    return [
        _workflow_node(
            node_id=str(spec["id"]),
            operation=str(spec["operation"]),
            depends_on=list(spec["depends_on"]),
            context=context,
            env_flags=env_flags,
            registry=registry,
        )
        for spec in specs
    ]


def _context(
    *,
    app: str,
    source: Optional[str] = None,
    target: Optional[str] = None,
    target_origin: Optional[str] = None,
    template: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
) -> Dict[str, str]:
    return {
        "app": app,
        "source": source or "SOURCE_HOST",
        "target": target or "TARGET_HOST",
        "target_origin": target_origin or "TARGET_ORIGIN",
        "template": template or "APP_TEMPLATE",
        "owner": owner or "OWNER",
        "repo": repo or "REPOSITORY",
        "phase": phase or "all",
    }


def _nodes_from_specs(
    specs: List[Dict[str, Any]],
    *,
    context: Dict[str, str],
    env_flags: List[str],
    registry: Dict[str, CommandDescriptor],
) -> List[WorkflowNode]:
    return [
        _workflow_node(
            node_id=str(spec["id"]),
            operation=str(spec["operation"]),
            depends_on=[str(value) for value in spec.get("depends_on", [])],
            context=context,
            env_flags=env_flags,
            registry=registry,
        )
        for spec in specs
    ]


def _build_incident_triage_nodes(
    *,
    app: str,
    environment: Optional[str],
    registry: Dict[str, CommandDescriptor],
    **_: Any,
) -> List[WorkflowNode]:
    env_flags = ["--environment", environment] if environment else []
    specs = [
        {"id": "runtime-status", "operation": "runtime.status", "depends_on": []},
        {"id": "runtime-doctor", "operation": "runtime.doctor", "depends_on": ["runtime-status"]},
        {"id": "drift", "operation": "runtime.drift", "depends_on": ["runtime-status"]},
        {"id": "traffic-status", "operation": "app.traffic.status", "depends_on": ["runtime-status"]},
        {"id": "observability-status", "operation": "observability.status", "depends_on": ["runtime-status"]},
        {"id": "receipt-timeline", "operation": "receipts.timeline", "depends_on": ["runtime-status"]},
        {"id": "readiness", "operation": "app.readiness", "depends_on": ["drift", "traffic-status", "observability-status"]},
    ]
    return _nodes_from_specs(specs, context=_context(app=app), env_flags=env_flags, registry=registry)


def _build_release_readiness_nodes(
    *,
    app: str,
    environment: Optional[str],
    registry: Dict[str, CommandDescriptor],
    **_: Any,
) -> List[WorkflowNode]:
    env_flags = ["--environment", environment] if environment else []
    specs = [
        {"id": "validate-manifest", "operation": "manifest.validate", "depends_on": []},
        {"id": "pack-validate", "operation": "pack.validate", "depends_on": ["validate-manifest"]},
        {"id": "deploy-plan", "operation": "deploy.plan", "depends_on": ["pack-validate"]},
        {"id": "backup-status", "operation": "backup.status", "depends_on": ["pack-validate"]},
        {"id": "observability-plan", "operation": "observability.plan", "depends_on": ["pack-validate"]},
        {"id": "readiness", "operation": "app.readiness", "depends_on": ["deploy-plan", "backup-status", "observability-plan"]},
    ]
    return _nodes_from_specs(specs, context=_context(app=app), env_flags=env_flags, registry=registry)


def _build_github_provisioning_nodes(
    *,
    app: str,
    environment: Optional[str],
    template: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
    registry: Dict[str, CommandDescriptor],
    **_: Any,
) -> List[WorkflowNode]:
    env_flags = ["--environment", environment] if environment else []
    specs = [
        {"id": "template-explain", "operation": "app.templates.explain", "depends_on": []},
        {"id": "github-plan", "operation": "app.github.provision.plan", "depends_on": ["template-explain"]},
        {"id": "github-apply", "operation": "app.github.provision.apply", "depends_on": ["github-plan"]},
    ]
    return _nodes_from_specs(
        specs,
        context=_context(app=app, template=template, owner=owner, repo=repo, phase=phase),
        env_flags=env_flags,
        registry=registry,
    )


def _build_restore_rehearsal_nodes(
    *,
    app: str,
    environment: Optional[str],
    registry: Dict[str, CommandDescriptor],
    **_: Any,
) -> List[WorkflowNode]:
    env_flags = ["--environment", environment] if environment else []
    specs = [
        {"id": "backup-status", "operation": "backup.status", "depends_on": []},
        {"id": "backup-verify-plan", "operation": "backup.verify.plan", "depends_on": ["backup-status"]},
        {"id": "backup-verify-apply", "operation": "backup.verify.apply", "depends_on": ["backup-verify-plan"]},
        {"id": "restore-drills-list", "operation": "restore.drills.list", "depends_on": ["backup-verify-apply"]},
    ]
    return _nodes_from_specs(specs, context=_context(app=app), env_flags=env_flags, registry=registry)


@dataclass(frozen=True)
class WorkflowTemplate:
    name: str
    summary: str
    parameters: List[str]
    builder: Any  # callable(**kwargs) -> List[WorkflowNode]


WORKFLOW_TEMPLATES: Dict[str, WorkflowTemplate] = {
    "move-app": WorkflowTemplate(
        name="move-app",
        summary=(
            "Resumable graph to move an app between hosts: validate, "
            "audit, check conflicts/backups, score readiness, then plan export, "
            "import, restore-drill, and traffic cutover. Apply/create nodes pause "
            "until explicit per-node confirmation is supplied."
        ),
        parameters=["app", "environment", "source", "target", "target_origin"],
        builder=_build_move_app_nodes,
    ),
    "incident-triage": WorkflowTemplate(
        name="incident-triage",
        summary="Read-only triage graph for runtime status, doctor, drift, traffic, observability, receipts, and readiness.",
        parameters=["app", "environment", "manifest"],
        builder=_build_incident_triage_nodes,
    ),
    "release-readiness": WorkflowTemplate(
        name="release-readiness",
        summary="Read-only release readiness graph for manifest validation, pack validation, deploy plan, backup, observability, and readiness.",
        parameters=["app", "environment", "manifest"],
        builder=_build_release_readiness_nodes,
    ),
    "github-provisioning": WorkflowTemplate(
        name="github-provisioning",
        summary="GitHub provisioning graph with plan first and apply paused for explicit confirmation.",
        parameters=["app", "environment", "template", "owner", "repo", "phase"],
        builder=_build_github_provisioning_nodes,
    ),
    "restore-rehearsal": WorkflowTemplate(
        name="restore-rehearsal",
        summary="Restore rehearsal graph: inspect backup status, plan verification, then pause before verification apply.",
        parameters=["app", "environment", "manifest", "backup_id"],
        builder=_build_restore_rehearsal_nodes,
    ),
}


def list_workflow_templates() -> Dict[str, Any]:
    """Read-only list of available workflow templates."""
    templates: List[Dict[str, Any]] = []
    registry = _registry_by_operation()
    for template in WORKFLOW_TEMPLATES.values():
        # node_count is derived from a placeholder build so it never executes.
        nodes = template.builder(
            app="<app>",
            environment=None,
            source=None,
            target=None,
            target_origin=None,
            registry=registry,
        )
        templates.append(
            {
                "name": template.name,
                "summary": template.summary,
                "node_count": len(nodes),
                "parameters": list(template.parameters),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKFLOW_TEMPLATES_KIND,
        "templates": templates,
    }


def _workflow_artifact_path(runtime_root: Path, workflow_id: str) -> Path:
    return Path(runtime_root) / "workflows" / f"{workflow_id}.json"


def _workflow_receipt_path(runtime_root: Path, operation_id_value: str) -> Path:
    return Path(runtime_root) / "workflows" / "receipts" / f"{operation_id_value}.json"


def plan_workflow(
    name: str,
    *,
    app: str,
    environment: Optional[str] = None,
    source: Optional[str] = None,
    target: Optional[str] = None,
    target_origin: Optional[str] = None,
    template: Optional[str] = None,
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    phase: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Build (and persist) a plan-only operation graph. Never executes a node."""
    workflow_id = operation_id(f"workflow.{name}", app, environment)
    blockers: List[Any] = []
    warnings: List[Any] = []

    template_def = WORKFLOW_TEMPLATES.get(name)
    if template_def is None:
        known = ", ".join(sorted(WORKFLOW_TEMPLATES)) or "(none)"
        blockers.append(
            issue(
                "workflow_template_not_found",
                f"Unknown workflow template '{name}'. Known templates: {known}.",
            )
        )
        return plan_envelope(
            "workflow.plan",
            app,
            environment,
            f"No plan: unknown workflow template '{name}'.",
            blockers=blockers,
            warnings=warnings,
            checks=[],
            artifacts=[],
            confirmation_required=False,
            confirmation_token=None,
            risk="low",
            kind=WORKFLOW_PLAN_KIND,
            workflow_id=workflow_id,
            name=name,
            nodes=[],
        )

    registry = _registry_by_operation()
    nodes = template_def.builder(
        app=app,
        environment=environment,
        source=source,
        target=target,
        target_origin=target_origin,
        template=template,
        owner=owner,
        repo=repo,
        phase=phase,
        registry=registry,
    )

    node_ids = {node.id for node in nodes}
    seen: set[str] = set()
    for node in nodes:
        # Defensive guards: every dependency must reference an earlier node, and
        # no node command may contain shell metacharacters. These never fire for
        # the shipped template but keep future templates honest.
        for dependency in node.depends_on:
            if dependency not in node_ids:
                blockers.append(
                    issue(
                        "workflow_unknown_dependency",
                        f"Node '{node.id}' depends on unknown node '{dependency}'.",
                    )
                )
            elif dependency not in seen:
                blockers.append(
                    issue(
                        "workflow_forward_dependency",
                        f"Node '{node.id}' depends on '{dependency}' which is not an earlier node.",
                    )
                )
        if command_has_shell_metacharacters(node.command):
            blockers.append(
                issue(
                    "workflow_unsafe_command",
                    f"Node '{node.id}' command contains a shell metacharacter; refusing to emit.",
                )
            )
        if node.mutates_state:
            warnings.append(
                issue(
                    "workflow_mutating_node",
                    f"Node '{node.id}' ({node.operation}) is mutating; it is not runnable in a "
                    "workflow graph and requires a separate plan + confirmation.",
                )
            )
        seen.add(node.id)

    node_dicts = [node.to_dict() for node in nodes]
    artifacts: List[Dict[str, Any]] = []

    # Best-effort: persist the graph as an inspectable artifact. On OSError we
    # skip the file but still return the plan.
    artifact_path = _workflow_artifact_path(runtime_root, workflow_id)
    summary = (
        f"Plan-only operation graph '{name}' for app '{app}' "
        f"with {len(node_dicts)} node(s). No node is executed."
    )
    plan_without_artifact: Dict[str, Any] = plan_envelope(
        "workflow.plan",
        app,
        environment,
        summary,
        blockers=blockers,
        warnings=warnings,
        checks=[],
        artifacts=[],
        confirmation_required=False,
        confirmation_token=None,
        risk="low",
        kind=WORKFLOW_PLAN_KIND,
        workflow_id=workflow_id,
        name=name,
        status="planned" if not blockers else "blocked",
        workflow_status="planned" if not blockers else "blocked",
        lifecycle_statuses=list(WORKFLOW_NODE_STATUSES),
        parameters={
            "app": app,
            "environment": environment,
            "source": source,
            "target": target,
            "target_origin": target_origin,
            "template": template,
            "owner": owner,
            "repo": repo,
            "phase": phase,
        },
        nodes=node_dicts,
        node_counts=_node_counts(node_dicts),
    )
    written = False
    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(plan_without_artifact, indent=2, sort_keys=True))
        written = True
    except OSError:
        warnings.append(
            issue(
                "workflow_artifact_write_failed",
                f"Could not write workflow graph artifact to {artifact_path}.",
            )
        )

    if written:
        artifacts.append(
            artifact(
                str(artifact_path),
                "ophelia.workflow_graph",
                description="Stored plan-only operation graph (read via `ship workflow show`).",
                present=True,
            )
        )

    plan = dict(plan_without_artifact)
    plan["artifacts"] = artifacts
    return plan


def run_workflow(
    workflow_id: str,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    substitutions: Optional[Dict[str, str]] = None,
    confirmations: Optional[Dict[str, str]] = None,
    timeout: float = _DEFAULT_NODE_TIMEOUT_SECONDS,
    command_runner: Optional[CommandRunner] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Execute the runnable nodes in a stored workflow graph.

    Safety boundaries:

    * only nodes whose stored ``mutates_state`` flag is false are executed;
    * commands are passed as argv arrays, never through a shell;
    * dependencies must have succeeded before a node can run;
    * unresolved TOKEN_CASE placeholders block that node and its dependents;
    * raw stdout/stderr is not stored in receipts, only small parsed summaries.
    """
    started_at = utc_now()
    runtime_root = Path(runtime_root)
    substitutions = dict(substitutions or {})
    confirmations = dict(confirmations or {})
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    graph_report = show_workflow(workflow_id, runtime_root=runtime_root)
    workflow_id = str(graph_report.get("workflow_id") or workflow_id)
    graph = graph_report.get("workflow") if isinstance(graph_report.get("workflow"), dict) else None
    if graph is None:
        blockers.extend(_as_issue_list(graph_report.get("blockers")))
        return _workflow_run_receipt(
            workflow_id,
            app=None,
            environment=None,
            status="blocked",
            started_at=started_at,
            nodes=[],
            blockers=blockers,
            warnings=warnings,
            runtime_root=runtime_root,
            graph_artifact=None,
            substitutions=substitutions,
        )

    app = graph.get("app") if isinstance(graph.get("app"), str) else None
    environment = graph.get("environment") if isinstance(graph.get("environment"), str) else None
    graph_status = str(graph.get("workflow_status") or graph.get("status") or "planned")
    if graph_status == "cancelled":
        blockers.append(issue("workflow_cancelled", f"Workflow '{workflow_id}' is cancelled and cannot run."))
    if graph_status == "paused" and not resume:
        blockers.append(issue("workflow_paused", f"Workflow '{workflow_id}' is paused. Use `ship workflow resume`."))
    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    if not raw_nodes:
        blockers.append(issue("workflow_empty", f"Workflow '{workflow_id}' has no nodes to run."))

    runner = command_runner or _default_command_runner
    timeout_seconds = _bounded_timeout(timeout)
    status_by_id: Dict[str, str] = {}
    run_nodes: List[Dict[str, Any]] = []

    for raw_node in raw_nodes:
        node = dict(raw_node) if isinstance(raw_node, dict) else {}
        node_id = str(node.get("id") or f"node-{len(run_nodes) + 1}")
        node["id"] = node_id
        depends_on = [str(value) for value in node.get("depends_on", []) if isinstance(value, str)]
        node["depends_on"] = depends_on
        if blockers:
            node["status"] = "skipped"
            node.setdefault("blockers", [issue("workflow_run_blocked", "Skipped because the workflow is not runnable.")])
            status_by_id[node_id] = "skipped"
            run_nodes.append(node)
            continue
        if resume and node.get("status") == "succeeded":
            status_by_id[node_id] = "succeeded"
            run_nodes.append(node)
            continue
        if resume and node.get("status") == "cancelled":
            status_by_id[node_id] = "skipped"
            run_nodes.append(node)
            continue
        node["started_at"] = None
        node["completed_at"] = None
        node.setdefault("receipt_id", None)

        failed_dependencies = [
            dependency
            for dependency in depends_on
            if status_by_id.get(dependency) != "succeeded"
        ]
        if failed_dependencies:
            node["status"] = "skipped"
            node["blockers"] = [
                issue(
                    "workflow_dependency_not_satisfied",
                    f"Skipped because dependency node(s) did not succeed: {', '.join(failed_dependencies)}.",
                )
            ]
            status_by_id[node_id] = "skipped"
            run_nodes.append(node)
            continue

        if bool(node.get("mutates_state")):
            policy_check = _mutating_node_policy_check(node, app, environment, runtime_root, node_id in confirmations)
            node["policy_checks"] = [policy_check]
            if not bool(policy_check.get("ok", True)):
                node["status"] = "blocked"
                node["blockers"] = _policy_blockers(policy_check) or [
                    issue("workflow_policy_blocked", f"Policy blocked mutating node '{node_id}'.")
                ]
                blockers.extend(node["blockers"])
                status_by_id[node_id] = "blocked"
                run_nodes.append(node)
                continue
            if node_id not in confirmations:
                node["status"] = "paused_for_confirmation"
                node["started_at"] = None
                node["completed_at"] = None
                node["requires_confirmation"] = True
                node["blockers"] = [
                    issue(
                        "workflow_confirmation_required",
                        f"Node '{node_id}' requires an explicit confirmation token from its own plan command.",
                    )
                ]
                blockers.extend(node["blockers"])
                status_by_id[node_id] = "paused_for_confirmation"
                run_nodes.append(node)
                continue

        node_substitutions = dict(substitutions)
        if node_id in confirmations:
            node_substitutions["CONFIRMATION_TOKEN"] = confirmations[node_id]

        ready, node_blockers = _preflight_node_command(
            node,
            node_id,
            node_substitutions,
            check_executable=False,
        )
        if not ready:
            node["status"] = "blocked"
            node["blockers"] = node_blockers
            blockers.extend(node_blockers)
            status_by_id[node_id] = "blocked"
            run_nodes.append(node)
            continue

        resolved_command = [str(token) for token in node["resolved_command"]]
        executable = _local_cli_command(resolved_command)
        node["status"] = "running"
        node["started_at"] = utc_now()
        try:
            result = runner(executable, timeout_seconds)
        except subprocess.TimeoutExpired:
            node["status"] = "failed"
            node["completed_at"] = utc_now()
            node["return_code"] = None
            node["output"] = {"timed_out": True}
            node["blockers"] = [
                issue("workflow_node_timeout", f"Node '{node_id}' exceeded {timeout_seconds:g}s timeout.")
            ]
            blockers.extend(node["blockers"])
            status_by_id[node_id] = "failed"
            run_nodes.append(node)
            continue
        except FileNotFoundError:
            node["status"] = "failed"
            node["completed_at"] = utc_now()
            node["return_code"] = None
            node["output"] = {"command_found": False}
            node["blockers"] = [
                issue("workflow_command_not_found", f"Node '{node_id}' command was not found.")
            ]
            blockers.extend(node["blockers"])
            status_by_id[node_id] = "failed"
            run_nodes.append(node)
            continue
        except Exception as exc:  # noqa: BLE001 - a runner failure must become a receipt, not a crash
            node["status"] = "failed"
            node["completed_at"] = utc_now()
            node["return_code"] = None
            node["output"] = {"error_type": type(exc).__name__}
            node["blockers"] = [
                issue("workflow_node_runner_failed", f"Node '{node_id}' runner failed ({type(exc).__name__}).")
            ]
            blockers.extend(node["blockers"])
            status_by_id[node_id] = "failed"
            run_nodes.append(node)
            continue

        node["completed_at"] = utc_now()
        node["return_code"] = int(result.returncode)
        node["output"] = _command_output_summary(result.stdout)
        _apply_output_summary_to_node(node)
        output_operation_id = node["output"].get("operation_id") if isinstance(node.get("output"), dict) else None
        if isinstance(output_operation_id, str) and output_operation_id:
            if node.get("output", {}).get("kind") == "ophelia.plan" or str(node.get("operation", "")).endswith(".plan"):
                node["plan_id"] = output_operation_id
                node["plan_operation_id"] = output_operation_id
            else:
                node["receipt_id"] = output_operation_id
        output_status = str(node.get("output", {}).get("status") or "") if isinstance(node.get("output"), dict) else ""
        output_blockers = _as_issue_list(node.get("blockers"))
        if result.returncode == 0:
            if output_status in {"blocked", "failed", "error", "cancelled"} or output_blockers:
                node["status"] = "failed" if output_status == "failed" else "blocked"
                if not output_blockers:
                    node["blockers"] = [
                        issue(
                            "workflow_node_payload_blocked",
                            f"Node '{node_id}' emitted status '{output_status or 'blocked'}'.",
                        )
                    ]
                    output_blockers = _as_issue_list(node.get("blockers"))
                blockers.extend(output_blockers)
                status_by_id[node_id] = node["status"]
            else:
                node["status"] = "succeeded"
                node["blockers"] = []
                status_by_id[node_id] = "succeeded"
        else:
            node["status"] = "failed"
            node["blockers"] = [
                issue("workflow_node_failed", f"Node '{node_id}' exited with status {result.returncode}.")
            ]
            blockers.extend(node["blockers"])
            status_by_id[node_id] = "failed"
        run_nodes.append(node)

    if blockers:
        final_status = (
            "failed"
            if any(node.get("status") == "failed" for node in run_nodes)
            else "paused"
            if any(node.get("status") == "paused_for_confirmation" for node in run_nodes)
            else "blocked"
        )
    elif run_nodes and all(node.get("status") == "succeeded" for node in run_nodes):
        final_status = "succeeded"
    else:
        final_status = "blocked"

    graph_update = dict(graph)
    graph_update["nodes"] = run_nodes
    graph_update["status"] = final_status
    graph_update["workflow_status"] = final_status
    graph_update["updated_at"] = utc_now()
    graph_update["last_run"] = {
        "status": final_status,
        "started_at": started_at,
        "completed_at": utc_now(),
        "substitutions_applied": sorted(substitutions),
        "confirmations_applied": sorted(confirmations),
    }
    graph_artifact = _workflow_artifact_path(runtime_root, workflow_id)
    try:
        _write_json(graph_artifact, graph_update)
    except OSError:
        warnings.append(issue("workflow_artifact_write_failed", f"Could not update workflow graph at {graph_artifact}."))
        graph_artifact = None

    return _workflow_run_receipt(
        workflow_id,
        app=app,
        environment=environment,
        status=final_status,
        started_at=started_at,
        nodes=run_nodes,
        blockers=blockers,
        warnings=warnings,
        runtime_root=runtime_root,
        graph_artifact=graph_artifact,
        substitutions=substitutions,
        confirmations=confirmations,
    )


def preview_workflow(
    workflow_id: str,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    substitutions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Preview a workflow run without executing nodes or writing receipts."""
    substitutions = dict(substitutions or {})
    graph_report = show_workflow(workflow_id, runtime_root=runtime_root)
    resolved_workflow_id = str(graph_report.get("workflow_id") or workflow_id)
    graph = graph_report.get("workflow") if isinstance(graph_report.get("workflow"), dict) else None
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    if graph is None:
        blockers.extend(_as_issue_list(graph_report.get("blockers")))
        return _workflow_preview_report(
            resolved_workflow_id,
            app=None,
            environment=None,
            nodes=[],
            blockers=blockers,
            warnings=warnings,
            substitutions=substitutions,
            graph_artifact=Path(str(graph_report.get("path"))) if graph_report.get("path") else None,
            requested_ref=workflow_id,
            resolved_ref=graph_report.get("resolved_ref") if isinstance(graph_report.get("resolved_ref"), dict) else None,
        )

    app = graph.get("app") if isinstance(graph.get("app"), str) else None
    environment = graph.get("environment") if isinstance(graph.get("environment"), str) else None
    raw_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    if not raw_nodes:
        blockers.append(issue("workflow_empty", f"Workflow '{resolved_workflow_id}' has no nodes to preview."))

    status_by_id: Dict[str, str] = {}
    preview_nodes: List[Dict[str, Any]] = []
    for raw_node in raw_nodes:
        node = dict(raw_node) if isinstance(raw_node, dict) else {}
        node_id = str(node.get("id") or f"node-{len(preview_nodes) + 1}")
        node["id"] = node_id
        depends_on = [str(value) for value in node.get("depends_on", []) if isinstance(value, str)]
        node["depends_on"] = depends_on
        node["started_at"] = None
        node["completed_at"] = None
        node["receipt_id"] = None

        failed_dependencies = [
            dependency
            for dependency in depends_on
            if status_by_id.get(dependency) != "ready"
        ]
        if failed_dependencies:
            node["status"] = "skipped"
            node["blockers"] = [
                issue(
                    "workflow_dependency_not_satisfied",
                    f"Skipped because dependency node(s) would not be ready: {', '.join(failed_dependencies)}.",
                )
            ]
            status_by_id[node_id] = "skipped"
            preview_nodes.append(node)
            continue

        if bool(node.get("mutates_state")):
            preview_substitutions = dict(substitutions)
            preview_substitutions.setdefault("CONFIRMATION_TOKEN", "CONFIRMATION_TOKEN")
            ready, node_blockers = _preflight_node_command(
                node,
                node_id,
                preview_substitutions,
                check_executable=True,
            )
            if not ready:
                node["status"] = "blocked"
                node["blockers"] = node_blockers
                blockers.extend(node_blockers)
                status_by_id[node_id] = "blocked"
                preview_nodes.append(node)
                continue
            node["status"] = "paused_for_confirmation"
            node["requires_confirmation"] = True
            node["blockers"] = [
                issue(
                    "workflow_confirmation_required",
                    f"Node '{node_id}' would pause until its own plan confirmation token is supplied.",
                )
            ]
            warnings.extend(node["blockers"])
            status_by_id[node_id] = "paused_for_confirmation"
            preview_nodes.append(node)
            continue

        ready, node_blockers = _preflight_node_command(
            node,
            node_id,
            substitutions,
            check_executable=True,
        )
        if ready:
            node["status"] = "ready"
            node["blockers"] = []
            status_by_id[node_id] = "ready"
        else:
            node["status"] = "blocked"
            node["blockers"] = node_blockers
            blockers.extend(node_blockers)
            status_by_id[node_id] = "blocked"
        preview_nodes.append(node)

    return _workflow_preview_report(
        resolved_workflow_id,
        app=app,
        environment=environment,
        nodes=preview_nodes,
        blockers=blockers,
        warnings=warnings,
        substitutions=substitutions,
        graph_artifact=_workflow_artifact_path(Path(runtime_root), resolved_workflow_id),
        requested_ref=workflow_id,
        resolved_ref=graph_report.get("resolved_ref") if isinstance(graph_report.get("resolved_ref"), dict) else None,
    )


def show_workflow(workflow_id: str, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    """Read a stored graph artifact. Missing/unreadable -> clear blocker, no crash."""
    resolution = resolve_workflow_ref(workflow_id, runtime_root=runtime_root)
    resolved_workflow_id = str(resolution.get("resolved_id") or workflow_id)
    artifact_path = Path(str(resolution.get("path"))) if resolution.get("path") else _workflow_artifact_path(runtime_root, resolved_workflow_id)
    base: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKFLOW_REPORT_KIND,
        "workflow_id": resolved_workflow_id,
        "requested_ref": workflow_id,
        "resolved_ref": public_resolution(resolution),
        "path": str(artifact_path),
        "blockers": [],
        "warnings": [],
        "workflow": None,
    }
    resolution_blockers = _as_issue_list(resolution.get("blockers"))
    only_not_found = bool(resolution_blockers) and all(
        blocker.get("code") == "operation_ref_not_found" for blocker in resolution_blockers
    )
    if not resolution.get("ok") and resolution_blockers and not only_not_found:
        base["status"] = "unresolved"
        base["summary"] = f"Workflow reference unresolved: '{workflow_id}'."
        base["blockers"] = resolution_blockers
        return base
    if not artifact_path.exists():
        base["status"] = "not_found"
        base["summary"] = f"No stored workflow graph for id '{workflow_id}'."
        base["blockers"] = [
            issue(
                "workflow_not_found",
                f"No stored workflow graph at {artifact_path}. Run `ship workflow plan` first.",
            )
        ]
        return base
    try:
        graph = json.loads(artifact_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        base["status"] = "error"
        base["summary"] = f"Stored workflow graph for id '{workflow_id}' is unreadable."
        base["blockers"] = [
            issue(
                "workflow_unreadable",
                f"Could not read workflow graph at {artifact_path}: {exc}.",
            )
        ]
        return base
    base["status"] = "ok"
    base["summary"] = f"Stored workflow graph for id '{resolved_workflow_id}'."
    base["workflow"] = graph
    return base


def pause_workflow(workflow_id: str, *, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    started_at = utc_now()
    runtime_root = Path(runtime_root)
    report = show_workflow(workflow_id, runtime_root=runtime_root)
    graph = report.get("workflow") if isinstance(report.get("workflow"), dict) else None
    blockers = _as_issue_list(report.get("blockers"))
    if graph is None:
        return _workflow_control_receipt(
            "workflow.pause",
            workflow_id=str(report.get("workflow_id") or workflow_id),
            graph=None,
            status="blocked",
            started_at=started_at,
            runtime_root=runtime_root,
            blockers=blockers,
            summary=f"Workflow pause blocked for '{workflow_id}'.",
        )
    current_status = str(graph.get("workflow_status") or graph.get("status") or "planned")
    if current_status == "cancelled":
        blockers.append(issue("workflow_cancelled", "Cancelled workflows cannot be paused."))
    elif current_status == "succeeded":
        blockers.append(issue("workflow_already_succeeded", "Succeeded workflows do not need to be paused."))
    if blockers:
        return _workflow_control_receipt(
            "workflow.pause",
            workflow_id=str(graph.get("workflow_id") or workflow_id),
            graph=graph,
            status="blocked",
            started_at=started_at,
            runtime_root=runtime_root,
            blockers=blockers,
            summary=f"Workflow pause blocked for '{workflow_id}'.",
        )
    updated = dict(graph)
    updated["status"] = "paused"
    updated["workflow_status"] = "paused"
    updated["updated_at"] = utc_now()
    _persist_workflow_graph(runtime_root, str(updated.get("workflow_id") or workflow_id), updated)
    return _workflow_control_receipt(
        "workflow.pause",
        workflow_id=str(updated.get("workflow_id") or workflow_id),
        graph=updated,
        status="succeeded",
        started_at=started_at,
        runtime_root=runtime_root,
        blockers=[],
        summary=f"Workflow '{workflow_id}' paused.",
    )


def cancel_workflow(workflow_id: str, *, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    started_at = utc_now()
    runtime_root = Path(runtime_root)
    report = show_workflow(workflow_id, runtime_root=runtime_root)
    graph = report.get("workflow") if isinstance(report.get("workflow"), dict) else None
    blockers = _as_issue_list(report.get("blockers"))
    if graph is None:
        return _workflow_control_receipt(
            "workflow.cancel",
            workflow_id=str(report.get("workflow_id") or workflow_id),
            graph=None,
            status="blocked",
            started_at=started_at,
            runtime_root=runtime_root,
            blockers=blockers,
            summary=f"Workflow cancel blocked for '{workflow_id}'.",
        )
    updated = dict(graph)
    nodes = []
    for raw_node in updated.get("nodes", []) if isinstance(updated.get("nodes"), list) else []:
        node = dict(raw_node) if isinstance(raw_node, dict) else {}
        if node.get("status") not in {"succeeded", "failed", "rolled_back"}:
            node["status"] = "cancelled"
            node["completed_at"] = utc_now()
        nodes.append(node)
    updated["nodes"] = nodes
    updated["status"] = "cancelled"
    updated["workflow_status"] = "cancelled"
    updated["updated_at"] = utc_now()
    _persist_workflow_graph(runtime_root, str(updated.get("workflow_id") or workflow_id), updated)
    return _workflow_control_receipt(
        "workflow.cancel",
        workflow_id=str(updated.get("workflow_id") or workflow_id),
        graph=updated,
        status="succeeded",
        started_at=started_at,
        runtime_root=runtime_root,
        blockers=[],
        summary=f"Workflow '{workflow_id}' cancelled.",
    )


def resume_workflow(
    workflow_id: str,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    substitutions: Optional[Dict[str, str]] = None,
    confirmations: Optional[Dict[str, str]] = None,
    timeout: float = _DEFAULT_NODE_TIMEOUT_SECONDS,
    command_runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    return run_workflow(
        workflow_id,
        runtime_root=runtime_root,
        substitutions=substitutions,
        confirmations=confirmations,
        timeout=timeout,
        command_runner=command_runner,
        resume=True,
    )


def _workflow_preview_report(
    workflow_id: str,
    *,
    app: Optional[str],
    environment: Optional[str],
    nodes: List[Dict[str, Any]],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    substitutions: Dict[str, str],
    graph_artifact: Optional[Path],
    requested_ref: str,
    resolved_ref: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    ready = sum(1 for node in nodes if node.get("status") == "ready")
    paused = sum(1 for node in nodes if node.get("status") == "paused_for_confirmation")
    blocked = sum(1 for node in nodes if node.get("status") == "blocked")
    skipped = sum(1 for node in nodes if node.get("status") == "skipped")
    artifacts = []
    if graph_artifact is not None:
        artifacts.append(
            artifact(
                str(graph_artifact),
                "ophelia.workflow_graph",
                "Stored workflow graph that would be used by workflow run.",
                present=graph_artifact.exists(),
            )
        )
    status = "blocked" if blockers else "paused" if paused else "ready"
    return plan_envelope(
        operation="workflow.preview",
        app=app,
        environment=environment,
        summary=f"Workflow preview {status}: {ready} ready, {paused} paused, {blocked} blocked, {skipped} skipped.",
        blockers=blockers,
        warnings=warnings,
        checks=[
            {
                "name": "preview_only",
                "ok": True,
                "message": "No workflow node was executed and no receipt was written.",
            },
            {
                "name": "nodes_ready",
                "ok": not blockers,
                "message": f"{ready}/{len(nodes)} node(s) ready; {paused} would pause for confirmation.",
            },
        ],
        artifacts=artifacts,
        confirmation_required=False,
        confirmation_token=None,
        risk="low",
        kind=WORKFLOW_PREVIEW_KIND,
        workflow_id=workflow_id,
        requested_ref=requested_ref,
        resolved_ref=resolved_ref,
        nodes=nodes,
        substitutions_applied=sorted(substitutions),
        node_counts={
            "total": len(nodes),
            "ready": ready,
            "paused_for_confirmation": paused,
            "blocked": blocked,
            "skipped": skipped,
        },
        status=status,
    )


def _workflow_run_receipt(
    workflow_id: str,
    *,
    app: Optional[str],
    environment: Optional[str],
    status: str,
    started_at: str,
    nodes: List[Dict[str, Any]],
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    runtime_root: Path,
    graph_artifact: Optional[Path],
    substitutions: Dict[str, str],
    confirmations: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    succeeded = sum(1 for node in nodes if node.get("status") == "succeeded")
    failed = sum(1 for node in nodes if node.get("status") == "failed")
    paused = sum(1 for node in nodes if node.get("status") == "paused_for_confirmation")
    blocked = sum(1 for node in nodes if node.get("status") == "blocked")
    skipped = sum(1 for node in nodes if node.get("status") == "skipped")
    confirmations = dict(confirmations or {})
    artifacts = []
    if graph_artifact is not None:
        artifacts.append(
            artifact(
                str(graph_artifact),
                "ophelia.workflow_graph",
                "Stored workflow graph updated with latest run status.",
                present=True,
            )
        )
    receipt = receipt_envelope(
        operation="workflow.run",
        app=app,
        environment=environment,
        status=status,
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=artifacts,
        checks=[
            {
                "name": "nodes_succeeded",
                "ok": failed == 0 and blocked == 0 and skipped == 0,
                "message": f"{succeeded}/{len(nodes)} node(s) succeeded; {paused} paused.",
            },
            {
                "name": "mutating_nodes_confirmation_gated",
                "ok": True,
                "message": "Mutating workflow nodes pause unless their node id has an explicit confirmation token.",
            },
        ],
        rollback={"available": False, "note": "Workflow run stores per-node rollback metadata from child receipts when available."},
        workflow_id=workflow_id,
        summary=(
            f"Workflow run {status}: {succeeded} succeeded, {failed} failed, "
            f"{paused} paused, {blocked} blocked, {skipped} skipped."
        ),
        blockers=blockers,
        warnings=warnings,
        nodes=nodes,
        substitutions_applied=sorted(substitutions),
        confirmations_applied=sorted(confirmations),
        node_counts={
            "total": len(nodes),
            "succeeded": succeeded,
            "failed": failed,
            "paused_for_confirmation": paused,
            "blocked": blocked,
            "skipped": skipped,
        },
    )
    receipt_path = _workflow_receipt_path(runtime_root, str(receipt["operation_id"]))
    receipt["artifacts"].append(
        artifact(str(receipt_path), "ophelia.workflow_run_receipt", "Workflow run receipt.", present=True)
    )
    redacted = deep_redact(receipt, safe_keys={"inputs_redacted"}, propagate=True)
    try:
        _write_json(receipt_path, redacted)
        if app:
            _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", redacted)
    except OSError:
        redacted.setdefault("warnings", []).append(
            issue("workflow_receipt_write_failed", f"Could not write workflow run receipt under {runtime_root}.")
        )
    return redacted


def _workflow_control_receipt(
    operation: str,
    *,
    workflow_id: str,
    graph: Optional[Dict[str, Any]],
    status: str,
    started_at: str,
    runtime_root: Path,
    blockers: List[Dict[str, str]],
    summary: str,
) -> Dict[str, Any]:
    app = graph.get("app") if isinstance(graph, dict) and isinstance(graph.get("app"), str) else None
    environment = graph.get("environment") if isinstance(graph, dict) and isinstance(graph.get("environment"), str) else None
    graph_path = _workflow_artifact_path(runtime_root, workflow_id)
    artifacts = [
        artifact(
            str(graph_path),
            "ophelia.workflow_graph",
            "Stored workflow graph updated by workflow control command.",
            present=graph_path.exists(),
        )
    ]
    receipt = receipt_envelope(
        operation=operation,
        app=app,
        environment=environment,
        status=status,
        started_at=started_at,
        completed_at=utc_now(),
        artifacts=artifacts,
        checks=[
            {
                "name": "workflow_state_updated",
                "ok": status == "succeeded",
                "message": summary,
            }
        ],
        rollback={"available": False, "note": "Workflow control changes only local workflow state."},
        kind=WORKFLOW_STATE_KIND,
        workflow_id=workflow_id,
        workflow_status=graph.get("workflow_status") if isinstance(graph, dict) else None,
        node_counts=_node_counts(graph.get("nodes", [])) if isinstance(graph, dict) and isinstance(graph.get("nodes"), list) else {"total": 0},
        blockers=blockers,
        warnings=[],
        summary=summary,
    )
    receipt_path = _workflow_receipt_path(runtime_root, str(receipt["operation_id"]))
    receipt["artifacts"].append(
        artifact(str(receipt_path), "ophelia.workflow_control_receipt", "Workflow control receipt.", present=True)
    )
    redacted = deep_redact(receipt, safe_keys={"inputs_redacted"}, propagate=True)
    try:
        _write_json(receipt_path, redacted)
        if app:
            _write_json(runtime_root / "apps" / app / "receipts" / f"{receipt['operation_id']}.json", redacted)
    except OSError:
        redacted.setdefault("warnings", []).append(
            issue("workflow_receipt_write_failed", f"Could not write workflow control receipt under {runtime_root}.")
        )
    return redacted


def _resolve_command(command: List[str], substitutions: Dict[str, str]) -> Tuple[List[str], List[str]]:
    unresolved = sorted({token for token in command if _PLACEHOLDER_RE.fullmatch(token) and token not in substitutions})
    return [str(substitutions.get(token, token)) for token in command], unresolved


def _preflight_node_command(
    node: Dict[str, Any],
    node_id: str,
    substitutions: Dict[str, str],
    *,
    check_executable: bool,
) -> Tuple[bool, List[Dict[str, str]]]:
    command = node.get("command") if isinstance(node.get("command"), list) else []
    command_tokens = [str(token) for token in command if isinstance(token, str)]
    if not command_tokens:
        return False, [issue("workflow_missing_command", f"Node '{node_id}' has no runnable command.")]
    if command_has_shell_metacharacters(command_tokens):
        return False, [issue("workflow_unsafe_command", f"Node '{node_id}' command contains a shell metacharacter.")]

    resolved_command, unresolved = _resolve_command(command_tokens, substitutions)
    node["resolved_command"] = resolved_command
    if unresolved:
        node["unresolved_placeholders"] = unresolved
        return False, [
            issue(
                "workflow_unresolved_placeholder",
                f"Node '{node_id}' requires substitution(s): {', '.join(unresolved)}.",
            )
        ]

    executable = _local_cli_command(resolved_command)
    node["executable"] = executable
    node["executable_path"] = executable[0] if executable else None
    if check_executable and not _executable_found(executable):
        return False, [
            issue(
                "workflow_executable_not_found",
                f"Node '{node_id}' executable was not found: {node.get('executable_path')}.",
            )
        ]
    node["executable_found"] = _executable_found(executable) if check_executable else None
    return True, []


def _local_cli_command(command: List[str]) -> List[str]:
    if command and command[0] == "ship":
        local_ship = REPO_ROOT / "cli" / "ship"
        if local_ship.exists():
            return [str(local_ship), *command[1:]]
    return command


def _executable_found(command: List[str]) -> bool:
    if not command:
        return False
    executable = command[0]
    path = Path(executable)
    if path.is_absolute() or len(path.parts) > 1:
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def _default_command_runner(command: List[str], timeout: float) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    src_path = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing}" if existing else src_path
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _command_output_summary(stdout: str) -> Dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return {"json": False, "stdout_bytes": len((stdout or "").encode("utf-8"))}
    if not isinstance(payload, dict):
        return {"json": True, "shape": type(payload).__name__}
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    rollback = payload.get("rollback") if isinstance(payload.get("rollback"), dict) else None
    summary = {
        "json": True,
        "schema_version": payload.get("schema_version"),
        "kind": payload.get("kind"),
        "operation": payload.get("operation"),
        "operation_id": payload.get("operation_id"),
        "status": payload.get("status"),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "artifact_count": len(artifacts),
        "blockers": blockers[:10],
        "warnings": warnings[:10],
        "artifacts": artifacts[:20],
        "rollback": rollback,
        "confirmation_required": payload.get("confirmation_required"),
        "confirmation_token_present": bool(payload.get("confirmation_token")),
    }
    return deep_redact(summary, propagate=True)


def _apply_output_summary_to_node(node: Dict[str, Any]) -> None:
    output = node.get("output") if isinstance(node.get("output"), dict) else {}
    if not output:
        return
    if isinstance(output.get("blockers"), list):
        node["blockers"] = _as_issue_list(output.get("blockers"))
    if isinstance(output.get("warnings"), list):
        node["warnings"] = _as_issue_list(output.get("warnings"))
    if isinstance(output.get("artifacts"), list):
        node["artifacts"] = output.get("artifacts", [])
    if isinstance(output.get("rollback"), dict):
        node["rollback"] = output.get("rollback")
    operation_id_value = output.get("operation_id")
    if isinstance(operation_id_value, str) and operation_id_value:
        if output.get("kind") == "ophelia.plan" or str(node.get("operation", "")).endswith(".plan"):
            node["plan_id"] = operation_id_value
            node["plan_operation_id"] = operation_id_value
        else:
            node["receipt_id"] = operation_id_value


def _mutating_node_policy_check(
    node: Dict[str, Any],
    app: Optional[str],
    environment: Optional[str],
    runtime_root: Path,
    confirmation_provided: bool,
) -> Dict[str, Any]:
    operation = str(node.get("operation") or "")
    context = {
        "confirmation_required": True,
        "confirmation_provided": confirmation_provided,
        "plan_exists": bool(node.get("plan_command")),
        "json_receipts": True,
        "workflow_node": str(node.get("id") or ""),
    }
    return policy_check_entry(operation, app, environment, context, runtime_root=runtime_root)


def _policy_blockers(policy_check: Dict[str, Any]) -> List[Dict[str, str]]:
    result = policy_check.get("result") if isinstance(policy_check.get("result"), dict) else {}
    blockers = result.get("blockers") if isinstance(result.get("blockers"), list) else []
    return _as_issue_list(blockers)


def _node_counts(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {"total": len(nodes)}
    for status in WORKFLOW_NODE_STATUSES:
        counts[status] = sum(1 for node in nodes if node.get("status") == status)
    return counts


def _as_issue_list(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    issues: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("code"), str) and isinstance(item.get("message"), str):
            entry = {"code": str(item["code"]), "message": str(item["message"])}
            if isinstance(item.get("path"), str):
                entry["path"] = str(item["path"])
            issues.append(entry)
    return issues


def _bounded_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_NODE_TIMEOUT_SECONDS
    if timeout <= 0:
        return _DEFAULT_NODE_TIMEOUT_SECONDS
    return min(timeout, 3600.0)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _persist_workflow_graph(runtime_root: Path, workflow_id: str, graph: Dict[str, Any]) -> Path:
    graph_path = _workflow_artifact_path(runtime_root, workflow_id)
    _write_json(graph_path, graph)
    return graph_path
