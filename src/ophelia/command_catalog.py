"""Single source of truth for the agent-facing command catalog.

Ophelia exposes two surfaces to agents and operators: the typed *action*
registry (:mod:`ophelia.actions`) and the human/agent CLI. This module derives
one ``CommandDescriptor`` registry from the action catalog so the CLI, the
``ship commands catalog`` command, and ``GET /commands`` all describe the same
operations the same way, with explicit risk, mutation, and confirmation
metadata. Commands that exist only on the CLI (and are not actions) are seeded
into :data:`CLI_ONLY_DESCRIPTORS`; later phases append their own descriptors
there.

Every descriptor is JSON-serializable via :meth:`CommandDescriptor.to_dict` and
contains no raw-secret defaults: the catalog only describes input *shapes*, it
never carries values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .actions import action_catalog

PLAN_KIND = "ophelia.plan"
RECEIPT_KIND = "ophelia.receipt"
REPORT_KIND = "ophelia.report"

PLAN_SCHEMA_REF = "ophelia.plan.v1"
RECEIPT_SCHEMA_REF = "ophelia.receipt.v1"
REPORT_SCHEMA_REF = "ophelia.report.v1"


@dataclass(frozen=True)
class CommandDescriptor:
    """One agent-facing command/operation and its safety contract."""

    command: str
    operation: str
    summary: str
    risk: str
    mutates_state: bool
    requires_confirmation: bool
    plan_command: Optional[str]
    apply_command: Optional[str]
    json_kind: str
    args_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema_ref: str = REPORT_SCHEMA_REF
    artifacts: List[str] = field(default_factory=list)
    safety_notes: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        examples = list(self.examples) or _examples_for(self.operation, self.command)
        return {
            "command": self.command,
            "operation": self.operation,
            "summary": self.summary,
            "risk": self.risk,
            "mutates_state": self.mutates_state,
            "requires_confirmation": self.requires_confirmation,
            "plan_command": self.plan_command,
            "apply_command": self.apply_command,
            "json_kind": self.json_kind,
            "args_schema": dict(self.args_schema),
            "output_schema_ref": self.output_schema_ref,
            "artifacts": list(self.artifacts),
            "safety_notes": list(self.safety_notes),
            "examples": examples,
        }


def _command_for_operation(operation: str) -> str:
    return "ship " + operation.replace(".", " ")


def _json_kind_for(operation: str) -> str:
    if operation.endswith(".plan"):
        return PLAN_KIND
    if operation.endswith(".apply") or operation.endswith(".create"):
        return RECEIPT_KIND
    return REPORT_KIND


def _output_schema_ref_for(operation: str) -> str:
    if operation.endswith(".plan"):
        return PLAN_SCHEMA_REF
    if operation.endswith(".apply") or operation.endswith(".create"):
        return RECEIPT_SCHEMA_REF
    return REPORT_SCHEMA_REF


def _risk_for(item: Dict[str, Any]) -> str:
    """Derive a coarse risk tier from mutation level and environment reach."""
    if item.get("mutation_level") != "mutating":
        return "low"
    operation = str(item.get("id", ""))
    allowed = item.get("allowed_environments") or []
    production_capable = "production" in allowed
    # Deploy and traffic mutations touch live serving paths.
    if production_capable and (operation.startswith("deploy.") or operation.startswith("app.traffic.")):
        return "critical"
    if production_capable:
        return "high"
    return "medium"


def _safety_notes_for(item: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    note = item.get("safety_notes")
    if isinstance(note, str) and note:
        notes.append(note)
    elif isinstance(note, list):
        notes.extend(str(entry) for entry in note if entry)
    for gate in item.get("policy_gates") or []:
        if gate:
            notes.append(f"policy gate: {gate}")
    return notes


_EXAMPLES_BY_OPERATION: Dict[str, List[str]] = {
    "commands.catalog": ["ship commands catalog --json", "ship commands catalog --human"],
    "self_test": ["ship self-test --json"],
    "runtime.doctor": ["ship doctor --json"],
    "runtime.status": ["ship status --json"],
    "runtime.drift": ["ship drift examples/dragonwriter.ophelia.yml --json"],
    "validate": ["ship validate examples/dragonwriter.ophelia.yml --json"],
    "render": ["ship render examples/portfolio.ophelia.yml --output-dir ./build/portfolio --json"],
    "manifest.validate": ["ship validate examples/dragonwriter.ophelia.yml --json"],
    "manifest.explain": ["ship explain examples/dragonwriter.ophelia.yml --json"],
    "manifest.diff": ["ship diff examples/portfolio.ophelia.yml --json"],
    "manifest.conflicts": ["ship inspect conflicts --json"],
    "deploy.plan": ["ship deploy examples/portfolio.ophelia.yml --plan --json"],
    "deploy.apply": ["ship deploy examples/portfolio.ophelia.yml --apply --confirm <token>"],
    "backup.plan": ["ship backup plan dragon-writer --json"],
    "backup.status": ["ship backup status dragon-writer --environment production --json"],
    "backup.create": ["ship backup create dragon-writer --confirm <token> --json"],
    "restore.plan": ["ship restore plan dragon-writer <backup-id> --json"],
    "restore.apply": ["ship restore apply dragon-writer <backup-id> --confirm <token> --json"],
    "host.inventory": ["ship host inventory --json"],
    "host.readiness": ["ship host readiness local --json", "ship host readiness --json"],
    "app.readiness": ["ship app readiness dragon-writer --environment production --json"],
    "app.placement.plan": ["ship app placement dragon-writer --environment production --from spaceship --to ovh --json"],
    "app.runbook": ["ship app runbook dragon-writer --environment production"],
    "app.export.plan": ["ship app export plan dragon-writer --environment production --json"],
    "app.export.create": ["ship app export create dragon-writer --environment production --confirm <token> --json"],
    "app.import.plan": ["ship app import plan ./exports/dragon-writer.production.export/manifest.json --json"],
    "app.import.apply": ["ship app import apply ./exports/dragon-writer.production.export/manifest.json --confirm <token> --json"],
    "app.restore-drill.plan": ["ship app restore-drill plan dragon-writer --source ./exports/dragon-writer.production.export.tar --json"],
    "app.restore-drill.apply": ["ship app restore-drill apply dragon-writer --source ./exports/dragon-writer.production.export.tar --confirm <token> --json"],
    "app.cutover.plan": ["ship app cutover plan dragon-writer --from spaceship --to ovh --environment production --json"],
    "app.cutover.apply": ["ship app cutover apply dragon-writer --from spaceship --to ovh --environment production --confirm <token> --json"],
    "app.traffic.plan": ["ship app traffic plan dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --json"],
    "app.traffic.apply": ["ship app traffic apply dragon-writer --from spaceship --to ovh --target-origin dragonwriter-target.example.net --environment production --confirm <token> --json"],
    "app.traffic.rollback.plan": ["ship app traffic rollback plan dragon-writer --receipt latest:app.traffic.apply --environment production --json"],
    "app.traffic.rollback.apply": ["ship app traffic rollback apply dragon-writer --receipt latest:app.traffic.apply --environment production --confirm <token> --json"],
    "app.isolation.plan": ["ship app isolation plan dragon-writer --environment production --json"],
    "app.templates.list": ["ship app templates list --json"],
    "app.templates.explain": ["ship app templates explain web-postgres --json"],
    "app.create.plan": ["ship app create plan --app demo-app --template static-site --json"],
    "app.create.apply": ["ship app create apply --plan create-plan.json --target-dir ../demo-app --confirm <token>"],
    "app.github.provision.plan": [
        "ship app github plan --app demo-app --template static-site --owner example --repo example/demo-app --json",
        "ship app github plan --app demo-app --template static-site --owner example --repo example/demo-app --github-provider github-app --json",
    ],
    "app.github.provision.apply": ["ship app github apply --app demo-app --template static-site --owner example --repo example/demo-app --confirm <token> --json"],
    "receipts.list": ["ship receipts list --app dragon-writer --json"],
    "receipts.show": ["ship receipts show latest:dragon-writer --json", "ship receipts show <receipt-id-prefix> --json"],
    "receipts.timeline": ["ship receipts timeline --app dragon-writer --json"],
    "state.status": ["ship state status --json"],
    "state.rebuild": ["ship state rebuild --json"],
    "state.refresh": ["ship state refresh --json"],
    "state.summary": ["ship state summary --json"],
    "state.query.receipts": ["ship state query receipts --app dragon-writer --json", "ship state query receipts --ref latest:deploy.apply --json"],
    "workflow.plan": [
        "ship workflow plan move-app --app dragon-writer --from spaceship --to ovh --environment production --json",
        "ship workflow plan incident-triage --app dragon-writer --environment production --json",
    ],
    "workflow.preview": ["ship workflow run latest:dragon-writer --preview --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --json"],
    "workflow.run": ["ship workflow run latest:dragon-writer --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --confirm-node export-create=<token> --json"],
    "workflow.pause": ["ship workflow pause latest:dragon-writer --json"],
    "workflow.resume": ["ship workflow resume latest:dragon-writer --set MANIFEST_PATH=manifests/dragon-writer.ophelia.yml --confirm-node export-create=<token> --json"],
    "workflow.cancel": ["ship workflow cancel latest:dragon-writer --json"],
    "workflow.show": ["ship workflow show latest:dragon-writer --json"],
    "workflow.list": ["ship workflow list --json"],
    "production.hardening": [
        "ship hardening production-readiness --json",
        "ship hardening production-readiness --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --plugins-dir fixtures/app-suite/plugins --include-fixture-suite --allow-blocked-live-readiness --json",
    ],
    "live_drills.list": ["ship live-drills list --json", "ship live-drills list --profiles fixtures/app-suite/live-drills.yml --json"],
    "live_drills.run": ["ship live-drills run fixture-suite-review --json", "ship live-drills run fixture-postgres-focused --profiles fixtures/app-suite/live-drills.yml --json"],
    "live_drills.run_all": ["ship live-drills run-all --json", "ship live-drills run-all --profiles fixtures/app-suite/live-drills.yml --json"],
    "live_hydration.report": [
        "ship live-hydration report --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --allow-blocked --json",
        "ship live-hydration report --app quark-ops --environment production --host-config config/ophelia-hosts.yml --provider-config config/ophelia-integrations.yml --json",
    ],
    "live_hydration.evidence.validate": [
        "ship live-hydration validate-evidence --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json",
        "ship live-hydration validate-evidence --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --input-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging --json",
    ],
    "live_hydration.scaffold": [
        "ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --json",
        "ship live-hydration scaffold --profile quark-ops-staging-file-baseline --profiles config/ophelia-live-drills.yml --output-dir ~/ophelia-runtime/hydration/quark-ops-staging/staging --write --json",
    ],
    "lumen.console": ["ship lumen console-data --json", "ship lumen console-data --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --plugins-dir fixtures/app-suite/plugins --json"],
    "live.readiness.run": [
        "ship live-readiness run --environment staging --json",
        "ship live-readiness run --runtime-root fixtures/app-suite/runtime --manifests-dir fixtures/app-suite/manifests --host-config fixtures/app-suite/host-inventory.yml --provider-config fixtures/app-suite/integrations.yml --allow-blocked --json",
    ],
    "observability.plan": ["ship observability plan --app dragon-writer --environment production --json"],
    "observability.status": ["ship observability status --app dragon-writer --environment production --json"],
    "observability.export": ["ship observability export --app dragon-writer --environment production --json"],
    "observability.schedule.run": ["ship observability schedule run --json"],
    "providers.validate": ["ship providers validate --config ./traffic-providers.json --json"],
    "providers.explain": ["ship providers explain --config ./traffic-providers.json --json"],
    "providers.github.status": ["ship providers github status --json"],
    "plugins.list": ["ship plugins list --json", "ship plugins list --plugins-dir fixtures/app-suite/plugins --json"],
    "plugins.catalog": ["ship plugins catalog --plugins-dir fixtures/app-suite/plugins --json"],
    "plugins.validate": ["ship plugins validate fixtures/app-suite/plugins/fixture-app-suite/ophelia-plugin.yml --trusted-root fixtures/app-suite/plugins --json"],
    "secrets.audit": ["ship secrets audit dragon-writer --environment production --json"],
    "secrets.providers": ["ship secrets providers dragon-writer --environment production --json"],
    "policy.evaluate": ["ship policy evaluate --operation app.traffic.apply --app dragon-writer --environment production --json"],
}


def _examples_for(operation: str, command: str) -> List[str]:
    examples = _EXAMPLES_BY_OPERATION.get(operation)
    if examples:
        return list(examples)
    if command:
        return [f"{command} --json"]
    return []


def _descriptors_from_actions() -> List[CommandDescriptor]:
    catalog = action_catalog()
    ids = {item["id"] for item in catalog}
    descriptors: List[CommandDescriptor] = []
    for item in catalog:
        operation = item["id"]
        plan_command: Optional[str] = None
        apply_command: Optional[str] = None
        if operation.endswith(".plan"):
            base = operation[: -len(".plan")]
            # An apply counterpart is either `<base>.apply` or `<base>.create`.
            apply_id = next((candidate for candidate in (base + ".apply", base + ".create") if candidate in ids), None)
            if apply_id is not None:
                plan_command = _command_for_operation(operation)
                apply_command = _command_for_operation(apply_id)
        elif operation.endswith(".apply") or operation.endswith(".create"):
            base = operation.rsplit(".", 1)[0]
            plan_id = base + ".plan"
            if plan_id in ids:
                plan_command = _command_for_operation(plan_id)
                apply_command = _command_for_operation(operation)
        descriptors.append(
            CommandDescriptor(
                command=_command_for_operation(operation),
                operation=operation,
                summary=item.get("description", ""),
                risk=_risk_for(item),
                mutates_state=item.get("mutation_level") == "mutating",
                requires_confirmation=item.get("confirmation_requirement") == "required",
                plan_command=plan_command,
                apply_command=apply_command,
                json_kind=_json_kind_for(operation),
                args_schema=item.get("input_schema", {}) or {},
                output_schema_ref=_output_schema_ref_for(operation),
                artifacts=[],
                safety_notes=_safety_notes_for(item),
                examples=list(item.get("examples") or []),
            )
        )
    return descriptors


# CLI-only commands that are not in the action registry. Later phases append
# their own descriptors via ``register_cli_descriptor`` or ``.append(...)``.
CLI_ONLY_DESCRIPTORS: List[CommandDescriptor] = [
    CommandDescriptor(
        command="ship commands catalog",
        operation="commands.catalog",
        summary="List every agent-facing command with risk and mutation metadata.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.command_catalog",
        args_schema={
            "type": "object",
            "properties": {"json": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.command_catalog.v1",
        artifacts=[],
        safety_notes=["Read-only discovery surface. No mutation."],
    ),
    CommandDescriptor(
        command="ship validate",
        operation="validate",
        summary="Validate an app manifest.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=REPORT_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=[],
        safety_notes=["Read-only manifest validation. No mutation."],
    ),
    CommandDescriptor(
        command="ship render",
        operation="render",
        summary="Render a manifest into a local runtime bundle.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind=REPORT_KIND,
        args_schema={
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "output_dir": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["manifest"],
            "additionalProperties": False,
        },
        output_schema_ref=REPORT_SCHEMA_REF,
        artifacts=["rendered compose bundle"],
        safety_notes=["Writes only local rendered files. Does not touch VPS or runtime root."],
    ),
]


def register_cli_descriptor(descriptor: CommandDescriptor) -> None:
    """Append a CLI-only descriptor (used by later phases)."""
    CLI_ONLY_DESCRIPTORS.append(descriptor)


def _ensure_cli_descriptors_loaded() -> None:
    """Import command modules so their ``register_cli_descriptor`` side effects run.

    The CLI builds its parser (importing every command module) before any
    catalog call, so this is a no-op there. But ``GET /commands`` can be reached
    via ``ophelia.api`` without that import, which would otherwise return a
    partial catalog. This runtime import is cached and cannot cycle.
    """
    try:
        import ophelia.commands  # noqa: F401  (import side effects register CLI descriptors)
    except ModuleNotFoundError as exc:
        if exc.name != "ophelia.commands":
            raise


def command_registry() -> List[CommandDescriptor]:
    """All descriptors (actions + CLI-only), deduped by ``command``.

    Action-derived descriptors win when a command name collides, since the
    action registry is the authoritative typed surface.
    """
    _ensure_cli_descriptors_loaded()
    by_command: Dict[str, CommandDescriptor] = {}
    for descriptor in _descriptors_from_actions():
        by_command[descriptor.command] = descriptor
    for descriptor in CLI_ONLY_DESCRIPTORS:
        by_command.setdefault(descriptor.command, descriptor)
    return list(by_command.values())


def catalog() -> List[Dict[str, Any]]:
    """JSON-serializable catalog sorted by ``command``."""
    return [descriptor.to_dict() for descriptor in sorted(command_registry(), key=lambda d: d.command)]
