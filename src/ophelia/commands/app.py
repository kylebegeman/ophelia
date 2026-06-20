from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..app_registry import app_health, app_logs, find_app
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..portability import export_plan, import_plan


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

    export = app_subparsers.add_parser("export", help="Plan app export bundles")
    export_subparsers = export.add_subparsers(dest="app_export_command")
    export_plan_parser = export_subparsers.add_parser("plan", help="Plan an app export without mutation")
    export_plan_parser.add_argument("app", help="App id")
    export_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    export_plan_parser.add_argument("--manifest", type=Path, help="Path to the app .ophelia manifest")
    export_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    export_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    export_plan_parser.set_defaults(handler=run_export_plan)

    import_cmd = app_subparsers.add_parser("import", help="Plan app imports from export bundles")
    import_subparsers = import_cmd.add_subparsers(dest="app_import_command")
    import_plan_parser = import_subparsers.add_parser("plan", help="Plan an app import without mutation")
    import_plan_parser.add_argument("source", type=Path, help="Export bundle directory, tarball, or metadata JSON")
    import_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    import_plan_parser.add_argument("--mode", choices=["rehearsal", "cutover"], default="rehearsal")
    import_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    import_plan_parser.set_defaults(handler=run_import_plan)


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


def run_export_plan(args: Namespace) -> int:
    plan = export_plan(
        app=args.app,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        ophelia_root=REPO_ROOT,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_export_plan(plan)
    return 0 if not plan["blockers"] else 1


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
        print(f"  - {item}")
