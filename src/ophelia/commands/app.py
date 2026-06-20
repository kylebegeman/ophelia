from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..app_registry import app_health, app_logs, find_app
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..portability import (
    app_readiness_report,
    app_runbook_report,
    cutover_apply,
    cutover_plan,
    export_create,
    export_plan,
    import_apply,
    import_plan,
    isolation_plan,
    restore_drill_apply,
    restore_drill_plan,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("app", help="Inspect one registered app")
    app_subparsers = parser.add_subparsers(dest="app_command")

    health = app_subparsers.add_parser("health", help="Run health checks for a registered app")
    health.add_argument("name")
    health.add_argument("--registry", type=Path, help="Path to app registry JSON")
    health.add_argument("--timeout", type=int, default=10)
    health.add_argument("--skip-docker", action="store_true", help="Only check health URLs")
    health.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    health.set_defaults(handler=run_health)

    logs = app_subparsers.add_parser("logs", help="Show logs for a registered app")
    logs.add_argument("name")
    logs.add_argument("--registry", type=Path, help="Path to app registry JSON")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--container", help="Limit logs to one registered container")
    logs.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    logs.set_defaults(handler=run_logs)

    readiness = app_subparsers.add_parser("readiness", help="Report app movement readiness")
    readiness.add_argument("app", help="App id")
    readiness.add_argument("--environment", choices=["dev", "staging", "production"])
    readiness.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    readiness.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    readiness.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    readiness.set_defaults(handler=run_readiness)

    runbook = app_subparsers.add_parser("runbook", help="Generate an app runbook")
    runbook.add_argument("app", help="App id")
    runbook.add_argument("--environment", choices=["dev", "staging", "production"])
    runbook.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    runbook.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    runbook.add_argument("--output", type=Path, help="Write Markdown runbook to this path")
    runbook.add_argument("--force", action="store_true", help="Overwrite --output if it exists")
    runbook.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    runbook.set_defaults(handler=run_runbook)

    export = app_subparsers.add_parser("export", help="Plan app export bundles")
    export_subparsers = export.add_subparsers(dest="app_export_command")
    export_plan_parser = export_subparsers.add_parser("plan", help="Plan an app export without mutation")
    export_plan_parser.add_argument("app", help="App id")
    export_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    export_plan_parser.add_argument("--manifest", type=Path, help="Path to the app .ophelia manifest")
    export_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    export_plan_parser.add_argument("--include-postgres", action="store_true", help="Plan opt-in live Postgres dump during export create")
    export_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    export_plan_parser.set_defaults(handler=run_export_plan)
    export_create_parser = export_subparsers.add_parser("create", help="Create a metadata/runtime export bundle")
    export_create_parser.add_argument("app", help="App id")
    export_create_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    export_create_parser.add_argument("--manifest", type=Path, help="Path to the app .ophelia manifest")
    export_create_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    export_create_parser.add_argument("--include-postgres", action="store_true", help="Run opt-in live Postgres dump planned with --include-postgres")
    export_create_parser.add_argument("--confirm", required=True, help="Confirmation token from app export plan")
    export_create_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    export_create_parser.set_defaults(handler=run_export_create)

    import_cmd = app_subparsers.add_parser("import", help="Plan app imports from export bundles")
    import_subparsers = import_cmd.add_subparsers(dest="app_import_command")
    import_plan_parser = import_subparsers.add_parser("plan", help="Plan an app import without mutation")
    import_plan_parser.add_argument("source", type=Path, help="Export bundle directory, tarball, or metadata JSON")
    import_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    import_plan_parser.add_argument("--mode", choices=["rehearsal", "cutover"], default="rehearsal")
    import_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    import_plan_parser.set_defaults(handler=run_import_plan)
    import_apply_parser = import_subparsers.add_parser("apply", help="Create a rehearsal import preview")
    import_apply_parser.add_argument("source", type=Path, help="Export bundle directory, tarball, or metadata JSON")
    import_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    import_apply_parser.add_argument("--mode", choices=["rehearsal", "cutover"], default="rehearsal")
    import_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from app import plan")
    import_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    import_apply_parser.set_defaults(handler=run_import_apply)

    restore_drill = app_subparsers.add_parser("restore-drill", help="Plan restore drills")
    restore_subparsers = restore_drill.add_subparsers(dest="app_restore_drill_command")
    restore_plan_parser = restore_subparsers.add_parser("plan", help="Plan a restore drill without mutation")
    restore_plan_parser.add_argument("app", help="App id")
    restore_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    restore_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    restore_plan_parser.add_argument("--source", type=Path, help="Export bundle directory, tarball, or metadata JSON")
    restore_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_plan_parser.set_defaults(handler=run_restore_drill_plan)
    restore_apply_parser = restore_subparsers.add_parser("apply", help="Run an isolated restore drill check")
    restore_apply_parser.add_argument("app", help="App id")
    restore_apply_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    restore_apply_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    restore_apply_parser.add_argument("--source", type=Path, required=True, help="Export bundle directory, tarball, or metadata JSON")
    restore_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    restore_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from restore-drill plan")
    restore_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    restore_apply_parser.set_defaults(handler=run_restore_drill_apply)

    cutover = app_subparsers.add_parser("cutover", help="Plan app cutovers")
    cutover_subparsers = cutover.add_subparsers(dest="app_cutover_command")
    cutover_plan_parser = cutover_subparsers.add_parser("plan", help="Plan cutover without mutation")
    cutover_plan_parser.add_argument("app", help="App id")
    cutover_plan_parser.add_argument("--from", dest="source_host", required=True, help="Source host id")
    cutover_plan_parser.add_argument("--to", dest="target_host", required=True, help="Target host id")
    cutover_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    cutover_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    cutover_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    cutover_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    cutover_plan_parser.set_defaults(handler=run_cutover_plan)
    cutover_apply_parser = cutover_subparsers.add_parser("apply", help="Write a cutover checkpoint receipt")
    cutover_apply_parser.add_argument("app", help="App id")
    cutover_apply_parser.add_argument("--from", dest="source_host", required=True, help="Source host id")
    cutover_apply_parser.add_argument("--to", dest="target_host", required=True, help="Target host id")
    cutover_apply_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    cutover_apply_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    cutover_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    cutover_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from cutover plan")
    cutover_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    cutover_apply_parser.set_defaults(handler=run_cutover_apply)

    isolation = app_subparsers.add_parser("isolation", help="Plan per-app isolation compatibility changes")
    isolation_subparsers = isolation.add_subparsers(dest="app_isolation_command")
    isolation_plan_parser = isolation_subparsers.add_parser("plan", help="Plan per-app isolation without mutation")
    isolation_plan_parser.add_argument("app", help="App id")
    isolation_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    isolation_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    isolation_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    isolation_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    isolation_plan_parser.set_defaults(handler=run_isolation_plan)


def run_health(args: Namespace) -> int:
    try:
        entry = find_app(args.name, args.registry)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return _print_error(str(exc), args.json)
    if args.timeout <= 0:
        return _print_error("--timeout must be greater than 0.", args.json)
    report = app_health(entry, timeout=args.timeout, skip_docker=args.skip_docker)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Health for {entry.name}: {'ok' if report['ok'] else 'failing'}")
        for item in report["containers"]:
            print(f"  container {item['container']}: status={item['status']} health={item['health']}")
        for item in report["health_urls"]:
            detail = f"HTTP {item['status_code']}" if item.get("status_code") is not None else item.get("error")
            print(f"  url {item['name']}: {'ok' if item['ok'] else 'fail'} {detail}")
    return 0 if report["ok"] else 1


def run_logs(args: Namespace) -> int:
    try:
        entry = find_app(args.name, args.registry)
        report = app_logs(entry, tail=args.tail, container=args.container)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return _print_error(str(exc), args.json)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["logs"]:
            print(f"===== {item['container']} =====")
            if item["stdout"]:
                print(item["stdout"])
            if item["stderr"]:
                print(item["stderr"])
    return 0 if all(item["ok"] for item in report["logs"]) else 1


def run_readiness(args: Namespace) -> int:
    report = app_readiness_report(args.app, args.environment, args.runtime_root, args.manifest)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"Score: {report['portability_score']['score']} ({report['portability_score']['level']})")
        _print_string_items("Blockers", report.get("blockers", []))
        _print_string_items("Warnings", report.get("warnings", []))
    return 0 if not report["blockers"] else 1


def run_runbook(args: Namespace) -> int:
    report = app_runbook_report(args.app, args.environment, args.runtime_root, args.manifest)
    markdown = str(report["markdown"])
    if args.output:
        if args.output.exists() and not args.force:
            return _print_error(f"Refusing to overwrite existing runbook: {args.output}", args.json)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown)
        report["artifacts"].append({"path": str(args.output), "kind": "runbook", "present": True})
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(markdown, end="")
    return 0 if not report["blockers"] else 1


def run_export_plan(args: Namespace) -> int:
    plan = export_plan(
        app=args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        ophelia_root=REPO_ROOT,
        include_postgres=args.include_postgres,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_export_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_export_create(args: Namespace) -> int:
    receipt = export_create(
        app=args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        confirm=args.confirm,
        ophelia_root=REPO_ROOT,
        include_postgres=args.include_postgres,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Export create {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Bundle: {receipt.get('bundle_path')}")
        _print_string_items("Blockers", receipt.get("blockers", []))
        _print_string_items("Warnings", receipt.get("warnings", []))
    return 0 if receipt["status"] == "succeeded" else 1


def run_import_plan(args: Namespace) -> int:
    plan = import_plan(
        source=args.source,
        runtime_root=args.runtime_root,
        mode=args.mode,
        ophelia_root=REPO_ROOT,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_import_apply(args: Namespace) -> int:
    receipt = import_apply(
        source=args.source,
        runtime_root=args.runtime_root,
        mode=args.mode,
        confirm=args.confirm,
        ophelia_root=REPO_ROOT,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Import apply {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Preview: {receipt.get('preview_path')}")
        _print_string_items("Blockers", receipt.get("blockers", []))
        _print_string_items("Warnings", receipt.get("warnings", []))
    return 0 if receipt["status"] == "succeeded" else 1


def run_restore_drill_plan(args: Namespace) -> int:
    plan = restore_drill_plan(args.app, args.environment, args.runtime_root, args.manifest, args.source)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_restore_drill_apply(args: Namespace) -> int:
    receipt = restore_drill_apply(
        args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        source=args.source,
        confirm=args.confirm,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Restore drill {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Drill: {receipt.get('drill_path')}")
        _print_string_items("Blockers", receipt.get("blockers", []))
        _print_string_items("Warnings", receipt.get("warnings", []))
    return 0 if receipt["status"] == "succeeded" else 1


def run_cutover_plan(args: Namespace) -> int:
    plan = cutover_plan(
        args.app,
        source_host=args.source_host,
        target_host=args.target_host,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_cutover_apply(args: Namespace) -> int:
    receipt = cutover_apply(
        args.app,
        source_host=args.source_host,
        target_host=args.target_host,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        confirm=args.confirm,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Cutover apply {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Cutover: {receipt.get('cutover_path')}")
        _print_string_items("Blockers", receipt.get("blockers", []))
        _print_string_items("Warnings", receipt.get("warnings", []))
    return 0 if receipt["status"] == "succeeded" else 1


def run_isolation_plan(args: Namespace) -> int:
    plan = isolation_plan(args.app, args.environment, args.runtime_root, args.manifest)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def _print_error(message: str, emit_json: bool) -> int:
    if emit_json:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(message)
    return 1


def _print_export_plan(plan: dict) -> None:
    print(plan["summary"])
    print(f"App: {plan['app']}")
    print(f"Environment: {plan['environment']}")
    print(f"Release: {plan.get('app_release_id') or 'unknown'}")
    artifacts = plan.get("artifact_paths", {})
    if isinstance(artifacts, dict):
        print(f"Bundle: {artifacts.get('bundle', 'unknown')}")
    print("Data dependencies: " + ", ".join(_data_dependency_names(plan.get("data_dependencies", {})) or ["none"]))
    _print_string_items("Blockers", plan.get("blockers", []))
    _print_string_items("Warnings", plan.get("warnings", []))
    print(f"Confirmation token: {plan.get('confirmation_token')}")


def _print_import_plan(plan: dict) -> None:
    print(plan["summary"])
    print(f"App: {plan.get('app') or 'unknown'}")
    print(f"Environment: {plan.get('environment') or 'unknown'}")
    print(f"Mode: {plan.get('mode')}")
    print(f"Source: {plan.get('source')}")
    _print_string_items("Blockers", plan.get("blockers", []))
    _print_string_items("Warnings", plan.get("warnings", []))
    print(f"Future apply token: {plan.get('confirmation_token')}")


def _data_dependency_names(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [key for key, item in sorted(value.items()) if item]


def _print_string_items(label: str, value: object) -> None:
    items = value if isinstance(value, list) else []
    if not items:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for item in items:
        if isinstance(item, dict):
            print(f"  - {item.get('message') or item}")
        else:
            print(f"  - {item}")
