from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .manifest import load_manifest
from .operation_schema import SCHEMA_VERSION


OPERATIONS_KIND = "ophelia.operations"
OPERATION_PLAN_KIND = "ophelia.operation_plan"
OPERATION_REPORT_KIND = "ophelia.operation_report"

OPERATION_TEMPLATES = {
    "deploy-with-preflight": [
        "preflight manifest",
        "deploy plan",
        "deploy apply with confirmation when required",
        "verify manifest",
    ],
    "rollback-with-verify": [
        "rollback plan",
        "rollback apply with confirmation",
        "verify restored manifest when available",
    ],
    "backup-before-deploy": [
        "backup plan",
        "backup create with confirmation",
        "deploy plan",
        "deploy apply with confirmation when required",
    ],
    "refresh-static-site": [
        "sync static assets",
        "deploy plan",
        "deploy apply with confirmation when required",
    ],
    "validate-all-manifests": [
        "load manifests sorted by deployment_order",
        "validate each manifest",
        "scan conflicts",
    ],
}


def list_operations() -> Dict[str, object]:
    operations = [
        {
            "name": name,
            "steps": steps,
            "dry_run_first": True,
            "creates_result_artifact": True,
            "executes_autonomously": False,
        }
        for name, steps in sorted(OPERATION_TEMPLATES.items())
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": OPERATIONS_KIND,
        "operations": operations,
        "summary": f"{len(operations)} operation template(s) available.",
    }


def operation_plan(
    name: str,
    manifest_dir: Path = REPO_ROOT / "manifests",
    runtime_root: Path | None = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, object]:
    runtime_root = Path(runtime_root or DEFAULT_RUNTIME_ROOT).expanduser()
    manifest_dir = Path(manifest_dir).expanduser()
    if name not in OPERATION_TEMPLATES:
        raise ValueError(f"Unknown operation template: {name}")
    manifests = _ordered_manifests(manifest_dir)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": OPERATION_PLAN_KIND,
        "operation": name,
        "steps": OPERATION_TEMPLATES[name],
        "manifest_order": manifests,
        "runtime_root": str(runtime_root),
        "dry_run": True,
        "warnings": [],
        "executes_autonomously": False,
        "result_artifact": _artifact(
            f"Operation {name} dry-run plan.",
            {
                "operation": name,
                "steps": OPERATION_TEMPLATES[name],
                "manifest_order": manifests,
            },
        ),
    }
    plan["confirmation_token"] = operation_token(plan)
    plan["summary"] = f"Operation {name} plans {len(plan['steps'])} explicit step(s)."
    return plan


def run_operation(
    name: str,
    confirm: str | None,
    manifest_dir: Path = REPO_ROOT / "manifests",
    runtime_root: Path | None = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, object]:
    runtime_root = Path(runtime_root or DEFAULT_RUNTIME_ROOT).expanduser()
    manifest_dir = Path(manifest_dir).expanduser()
    plan = operation_plan(name, manifest_dir, runtime_root)
    if confirm is None:
        return plan
    if confirm != plan["confirmation_token"]:
        raise ValueError("Operation confirmation token did not match the plan.")
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": OPERATION_REPORT_KIND,
        "operation": name,
        "applied_at": _utc_now(),
        "executed_autonomously": False,
        "steps": plan["steps"],
        "manifest_order": plan["manifest_order"],
        "runtime_root": str(runtime_root),
        "summary": "Operation template confirmed. Steps remain explicit operator commands.",
        "result_artifact": _artifact(
            "Operation template confirmed.",
            {
                "operation": name,
                "steps": plan["steps"],
                "manifest_order": plan["manifest_order"],
                "executed_autonomously": False,
            },
        ),
    }
    report_path = _write_operation_report(runtime_root, report)
    report["report_path"] = str(report_path)
    return report


def operation_token(plan: Dict[str, object]) -> str:
    encoded = json.dumps(
        {
            "operation": plan["operation"],
            "steps": plan["steps"],
            "manifest_order": plan["manifest_order"],
            "runtime_root": plan["runtime_root"],
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _ordered_manifests(manifest_dir: Path) -> List[Dict[str, object]]:
    entries = []
    for manifest_path in sorted(manifest_dir.glob("*.ophelia.yml")) if manifest_dir.exists() else []:
        manifest = load_manifest(manifest_path)
        entries.append(
            {
                "app": manifest.app,
                "environment": manifest.environment,
                "manifest_path": str(manifest_path),
                "deployment_order": manifest.deployment_order,
                "depends_on": manifest.depends_on,
                "migration_before": manifest.migration_before,
                "verify_before_next": manifest.verify_before_next,
            }
        )
    return sorted(entries, key=lambda item: (item["deployment_order"] is None, item["deployment_order"] or 0, item["app"]))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _artifact(summary: str, payload: Dict[str, object]) -> Dict[str, object]:
    return {
        "summary": summary,
        "payload": payload,
        "report_markdown": f"### {summary}\n\n```json\n{json.dumps(payload, indent=2, sort_keys=True)}\n```",
    }


def _write_operation_report(runtime_root: Path, report: Dict[str, object]) -> Path:
    reports_root = runtime_root / "operations"
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = reports_root / f"{stamp}-{report['operation']}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report_path
