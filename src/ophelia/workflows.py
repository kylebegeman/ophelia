"""Agent-native operation graphs (Phase 8).

This module builds *inspectable* operation graphs ("workflows") that an agent or
operator can read before doing anything. A workflow is a small DAG of
:class:`WorkflowNode` objects; each node names a real Ophelia operation (from the
command catalog) and the exact typed CLI argument *array* that would run it.

This phase is **plan-only**: ``plan_workflow`` produces and persists a graph, but
nothing in this module ever executes a node. Every node stays ``status="planned"``
with ``receipt_id=None``. The resumability fields (``started_at``,
``completed_at``, ``receipt_id``) exist so a *future* executor phase can record
per-node progress without changing this contract; they are not populated here.

Node commands are always typed argument arrays (``List[str]``), never shell
strings, and never contain shell metacharacters. They are derived from the
canonical command-catalog descriptor for the node's operation (the human
``command`` string split into tokens) plus typed positional/flag tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .command_catalog import CommandDescriptor, command_registry
from .config import DEFAULT_RUNTIME_ROOT
from .operation_schema import SCHEMA_VERSION, artifact, issue, operation_id

WORKFLOW_PLAN_KIND = "ophelia.workflow_plan"
WORKFLOW_TEMPLATES_KIND = "ophelia.workflow_templates"
WORKFLOW_REPORT_KIND = "ophelia.workflow_report"

# Tokens that must never appear inside a node command array. A typed argument
# array should be passed straight to ``argv`` without any shell, so the presence
# of any shell metacharacter is a defect we refuse to emit.
_SHELL_METACHARACTERS = ("&&", "||", "|", ";", "`", "$(", ">", "<", "\n", "&")


@dataclass(frozen=True)
class WorkflowNode:
    """One node in an operation graph.

    ``command`` is a typed argument array (e.g.
    ``["ship", "app", "readiness", "<app>", "--environment", "<env>", "--json"]``)
    and never a shell string. ``mutates_state`` mirrors the catalog descriptor for
    ``operation``; a mutating node is never runnable in this phase (its status
    stays ``"planned"`` and execution would require a separate plan + confirmation
    handled by a future executor).
    """

    id: str
    operation: str
    command: List[str]
    depends_on: List[str]
    mutates_state: bool
    status: str = "planned"
    blockers: List[Any] = field(default_factory=list)
    artifacts: List[Any] = field(default_factory=list)
    # Resumability fields for a future executor. Plan-only: always None/unset here.
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    receipt_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "command": list(self.command),
            "depends_on": list(self.depends_on),
            "mutates_state": self.mutates_state,
            "status": self.status,
            "blockers": list(self.blockers),
            "artifacts": list(self.artifacts),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "receipt_id": self.receipt_id,
        }


def _registry_by_operation() -> Dict[str, CommandDescriptor]:
    return {descriptor.operation: descriptor for descriptor in command_registry()}


def _command_tokens(operation: str, registry: Dict[str, CommandDescriptor]) -> List[str]:
    """Base CLI tokens for an operation, sourced from its catalog descriptor.

    The descriptor ``command`` is a human string like ``"ship app readiness"``;
    splitting on whitespace yields the typed argv prefix. Falls back to deriving
    tokens from the operation id if the operation is not in the registry (it
    should be, but we never crash).
    """
    descriptor = registry.get(operation)
    if descriptor is not None:
        return descriptor.command.split()
    return ["ship", *operation.split(".")]


def _node_command(
    operation: str,
    registry: Dict[str, CommandDescriptor],
    *,
    positionals: Optional[List[str]] = None,
    flags: Optional[List[str]] = None,
) -> List[str]:
    tokens = _command_tokens(operation, registry)
    tokens.extend(positionals or [])
    tokens.extend(flags or [])
    tokens.append("--json")
    return tokens


def command_has_shell_metacharacters(command: List[str]) -> bool:
    """True if any token in a command array contains a shell metacharacter."""
    return any(any(meta in token for meta in _SHELL_METACHARACTERS) for token in command)


def _build_move_app_nodes(
    *,
    app: str,
    environment: Optional[str],
    source: Optional[str],
    target: Optional[str],
    target_origin: Optional[str],
    registry: Dict[str, CommandDescriptor],
) -> List[WorkflowNode]:
    """Build the ``move-app`` node graph in dependency order.

    Every node here maps to a read/plan operation, so each ``mutates_state`` is
    sourced from the catalog descriptor and is expected to be ``False``. If a
    descriptor ever reports a mutating operation, the node is still emitted with
    ``mutates_state=True`` and stays ``status="planned"`` (not runnable in this
    plan-only phase).
    """
    env_flags = ["--environment", environment] if environment else []
    # Placeholders are explicit when an optional input was not provided, so the
    # graph stays inspectable and the missing input is obvious to a human/agent.
    # They are bracket-free on purpose: a node command must never contain a shell
    # metacharacter, and `<`/`>` are metacharacters, so we use TOKEN_CASE markers.
    source_value = source or "SOURCE_HOST"
    target_value = target or "TARGET_HOST"
    target_origin_value = target_origin or "TARGET_ORIGIN"

    def mutates(operation: str) -> bool:
        descriptor = registry.get(operation)
        return bool(descriptor.mutates_state) if descriptor is not None else False

    specs: List[Dict[str, Any]] = [
        {
            "id": "validate-manifest",
            "operation": "manifest.validate",
            "depends_on": [],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "validate-provider-config",
            "operation": "providers.validate",
            "depends_on": ["validate-manifest"],
            "positionals": [],
            "flags": [],
        },
        {
            "id": "audit-secrets",
            "operation": "secrets.audit",
            "depends_on": ["validate-provider-config"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "check-route-conflicts",
            "operation": "manifest.conflicts",
            "depends_on": ["audit-secrets"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "check-backups",
            "operation": "backup.status",
            "depends_on": ["check-route-conflicts"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "readiness",
            "operation": "app.readiness",
            "depends_on": ["check-backups"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "export-plan",
            "operation": "app.export.plan",
            "depends_on": ["readiness"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "import-plan",
            # `app.import.plan` takes a positional export-bundle source produced
            # by the upstream export-plan/create step, not a host id.
            "operation": "app.import.plan",
            "depends_on": ["export-plan"],
            "positionals": ["EXPORT_BUNDLE"],
            "flags": [],
        },
        {
            "id": "restore-drill-plan",
            "operation": "app.restore-drill.plan",
            "depends_on": ["import-plan"],
            "positionals": [app],
            "flags": list(env_flags),
        },
        {
            "id": "traffic-plan",
            "operation": "app.traffic.plan",
            "depends_on": ["restore-drill-plan"],
            "positionals": [app],
            "flags": [
                "--from",
                source_value,
                "--to",
                target_value,
                "--target-origin",
                target_origin_value,
                *env_flags,
            ],
        },
    ]

    nodes: List[WorkflowNode] = []
    for spec in specs:
        operation = spec["operation"]
        command = _node_command(
            operation,
            registry,
            positionals=spec["positionals"],
            flags=spec["flags"],
        )
        nodes.append(
            WorkflowNode(
                id=spec["id"],
                operation=operation,
                command=command,
                depends_on=list(spec["depends_on"]),
                mutates_state=mutates(operation),
            )
        )
    return nodes


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
            "Inspectable plan-only graph to move an app between hosts: validate, "
            "audit, check conflicts/backups, score readiness, then plan export, "
            "import, restore-drill, and traffic cutover. No node is executed."
        ),
        parameters=["app", "environment", "source", "target", "target_origin"],
        builder=_build_move_app_nodes,
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


def plan_workflow(
    name: str,
    *,
    app: str,
    environment: Optional[str] = None,
    source: Optional[str] = None,
    target: Optional[str] = None,
    target_origin: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Build (and persist) a plan-only operation graph. Never executes a node."""
    workflow_id = operation_id(f"workflow.{name}", app, environment)
    blockers: List[Any] = []
    warnings: List[Any] = []

    template = WORKFLOW_TEMPLATES.get(name)
    if template is None:
        known = ", ".join(sorted(WORKFLOW_TEMPLATES)) or "(none)"
        blockers.append(
            issue(
                "workflow_template_not_found",
                f"Unknown workflow template '{name}'. Known templates: {known}.",
            )
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": WORKFLOW_PLAN_KIND,
            "workflow_id": workflow_id,
            "name": name,
            "app": app,
            "environment": environment,
            "dry_run": True,
            "summary": f"No plan: unknown workflow template '{name}'.",
            "nodes": [],
            "blockers": blockers,
            "warnings": warnings,
            "artifacts": [],
        }

    registry = _registry_by_operation()
    nodes = template.builder(
        app=app,
        environment=environment,
        source=source,
        target=target,
        target_origin=target_origin,
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
                    "plan-only graph and requires a separate plan + confirmation.",
                )
            )
        seen.add(node.id)

    node_dicts = [node.to_dict() for node in nodes]
    artifacts: List[Dict[str, Any]] = []

    # Best-effort: persist the graph as an inspectable artifact. On OSError we
    # skip the file but still return the plan.
    artifact_path = _workflow_artifact_path(runtime_root, workflow_id)
    plan_without_artifact: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKFLOW_PLAN_KIND,
        "workflow_id": workflow_id,
        "name": name,
        "app": app,
        "environment": environment,
        "dry_run": True,
        "summary": (
            f"Plan-only operation graph '{name}' for app '{app}' "
            f"with {len(node_dicts)} node(s). No node is executed."
        ),
        "nodes": node_dicts,
        "blockers": blockers,
        "warnings": warnings,
        "artifacts": [],
    }
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
    plan["warnings"] = warnings
    return plan


def show_workflow(workflow_id: str, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> Dict[str, Any]:
    """Read a stored graph artifact. Missing/unreadable -> clear blocker, no crash."""
    artifact_path = _workflow_artifact_path(runtime_root, workflow_id)
    base: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": WORKFLOW_REPORT_KIND,
        "workflow_id": workflow_id,
        "blockers": [],
        "warnings": [],
        "workflow": None,
    }
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
    base["summary"] = f"Stored workflow graph for id '{workflow_id}'."
    base["workflow"] = graph
    return base
