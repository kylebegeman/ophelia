from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..app_factory import create_apply, create_plan, templates_explain, templates_list
from ..app_registry import app_health, app_logs, find_app
from ..command_catalog import RECEIPT_SCHEMA_REF, CommandDescriptor, register_cli_descriptor
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
    traffic_apply,
    traffic_plan,
    traffic_rollback_apply,
    traffic_rollback_plan,
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

    traffic = app_subparsers.add_parser("traffic", help="Plan production traffic automation")
    traffic_subparsers = traffic.add_subparsers(dest="app_traffic_command")
    traffic_plan_parser = traffic_subparsers.add_parser("plan", help="Plan production traffic movement without mutation")
    traffic_plan_parser.add_argument("app", help="App id")
    traffic_plan_parser.add_argument("--from", dest="source_host", required=True, help="Source host id")
    traffic_plan_parser.add_argument("--to", dest="target_host", required=True, help="Target host id")
    traffic_plan_parser.add_argument("--target-origin", required=True, help="Target DNS/Caddy origin host or IP")
    traffic_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    traffic_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    traffic_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    traffic_plan_parser.add_argument("--dns-provider", choices=["manual", "file", "cloudflare"], default="manual")
    traffic_plan_parser.add_argument("--caddy-provider", choices=["manual", "file"], default="manual")
    traffic_plan_parser.add_argument("--ttl", type=int, default=300)
    traffic_plan_parser.add_argument("--provider-config", type=Path, help="Traffic provider config JSON for opt-in provider execution")
    traffic_plan_parser.add_argument(
        "--execute-provider-mutation",
        action="store_true",
        help="Plan confirmed provider writes using --provider-config; defaults to checkpoint-only",
    )
    traffic_plan_parser.add_argument("--target-health-url", help="Read-only target health URL to check before traffic apply")
    traffic_plan_parser.add_argument("--run-target-health", action="store_true", help="Execute --target-health-url during plan/apply")
    traffic_plan_parser.add_argument("--target-health-timeout", type=float, default=10.0)
    traffic_plan_parser.add_argument("--target-health-expect-status", type=int, default=200)
    traffic_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    traffic_plan_parser.set_defaults(handler=run_traffic_plan)
    traffic_apply_parser = traffic_subparsers.add_parser("apply", help="Write a production traffic checkpoint receipt")
    traffic_apply_parser.add_argument("app", help="App id")
    traffic_apply_parser.add_argument("--from", dest="source_host", required=True, help="Source host id")
    traffic_apply_parser.add_argument("--to", dest="target_host", required=True, help="Target host id")
    traffic_apply_parser.add_argument("--target-origin", required=True, help="Target DNS/Caddy origin host or IP")
    traffic_apply_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    traffic_apply_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    traffic_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    traffic_apply_parser.add_argument("--dns-provider", choices=["manual", "file", "cloudflare"], default="manual")
    traffic_apply_parser.add_argument("--caddy-provider", choices=["manual", "file"], default="manual")
    traffic_apply_parser.add_argument("--ttl", type=int, default=300)
    traffic_apply_parser.add_argument("--provider-config", type=Path, help="Traffic provider config JSON used by the matching plan")
    traffic_apply_parser.add_argument(
        "--execute-provider-mutation",
        action="store_true",
        help="Execute configured provider writes after confirmation; defaults to checkpoint-only",
    )
    traffic_apply_parser.add_argument("--target-health-url", help="Read-only target health URL used by the matching plan")
    traffic_apply_parser.add_argument("--run-target-health", action="store_true", help="Execute --target-health-url during apply preflight")
    traffic_apply_parser.add_argument("--target-health-timeout", type=float, default=10.0)
    traffic_apply_parser.add_argument("--target-health-expect-status", type=int, default=200)
    traffic_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from traffic plan")
    traffic_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    traffic_apply_parser.set_defaults(handler=run_traffic_apply)
    traffic_rollback = traffic_subparsers.add_parser("rollback", help="Plan or apply traffic provider rollback")
    traffic_rollback_subparsers = traffic_rollback.add_subparsers(dest="app_traffic_rollback_command")
    traffic_rollback_plan_parser = traffic_rollback_subparsers.add_parser("plan", help="Plan traffic provider rollback from a receipt")
    traffic_rollback_plan_parser.add_argument("app", help="App id")
    traffic_rollback_plan_parser.add_argument("--receipt", required=True, help="Traffic apply receipt id or path")
    traffic_rollback_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    traffic_rollback_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    traffic_rollback_plan_parser.add_argument(
        "--approve-unsafe-delete",
        action="store_true",
        help="Authorize deleting DNS records/Caddy files the forward apply created (they had no prior state to restore)",
    )
    traffic_rollback_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    traffic_rollback_plan_parser.set_defaults(handler=run_traffic_rollback_plan)
    traffic_rollback_apply_parser = traffic_rollback_subparsers.add_parser("apply", help="Apply traffic provider rollback from a receipt")
    traffic_rollback_apply_parser.add_argument("app", help="App id")
    traffic_rollback_apply_parser.add_argument("--receipt", required=True, help="Traffic apply receipt id or path")
    traffic_rollback_apply_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    traffic_rollback_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    traffic_rollback_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from traffic rollback plan")
    traffic_rollback_apply_parser.add_argument(
        "--approve-unsafe-delete",
        action="store_true",
        help="Authorize deleting DNS records/Caddy files the forward apply created (they had no prior state to restore)",
    )
    traffic_rollback_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    traffic_rollback_apply_parser.set_defaults(handler=run_traffic_rollback_apply)

    isolation = app_subparsers.add_parser("isolation", help="Plan per-app isolation compatibility changes")
    isolation_subparsers = isolation.add_subparsers(dest="app_isolation_command")
    isolation_plan_parser = isolation_subparsers.add_parser("plan", help="Plan per-app isolation without mutation")
    isolation_plan_parser.add_argument("app", help="App id")
    isolation_plan_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    isolation_plan_parser.add_argument("--manifest", type=Path, help="Path to app .ophelia manifest")
    isolation_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    isolation_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    isolation_plan_parser.set_defaults(handler=run_isolation_plan)

    create = app_subparsers.add_parser("create", help="Scaffold a new app from a template")
    create_subparsers = create.add_subparsers(dest="app_create_command")
    create_plan_parser = create_subparsers.add_parser(
        "plan", help="Plan an app scaffold without writing anything"
    )
    create_plan_parser.add_argument("--app", required=True, help="App id (used for the manifest and domains)")
    create_plan_parser.add_argument("--template", required=True, help="Template name (see `app templates list`)")
    create_plan_parser.add_argument("--owner", default="personal", help="Owner label for the manifest pack")
    create_plan_parser.add_argument(
        "--environment", choices=["dev", "staging", "production"], default="production"
    )
    create_plan_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    create_plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    create_plan_parser.set_defaults(handler=run_create_plan)
    create_apply_parser = create_subparsers.add_parser(
        "apply", help="Write scaffold files only under --target-dir, gated by a confirmation token"
    )
    create_apply_group = create_apply_parser.add_mutually_exclusive_group(required=True)
    create_apply_group.add_argument("--plan", type=Path, help="Path to a saved plan JSON")
    create_apply_group.add_argument(
        "--plan-id",
        help="App id + template + owner + environment to re-derive the plan, as `app:template:owner:environment`",
    )
    create_apply_parser.add_argument("--confirm", required=True, help="Confirmation token from app create plan")
    create_apply_parser.add_argument(
        "--target-dir", type=Path, required=True, help="Workdir to scaffold into (outside runtime root and repo)"
    )
    create_apply_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    create_apply_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    create_apply_parser.set_defaults(handler=run_create_apply)

    templates = app_subparsers.add_parser("templates", help="List and explain app scaffold templates")
    templates_subparsers = templates.add_subparsers(dest="app_templates_command")
    templates_list_parser = templates_subparsers.add_parser("list", help="List available templates")
    templates_list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    templates_list_parser.set_defaults(handler=run_templates_list)
    templates_explain_parser = templates_subparsers.add_parser("explain", help="Explain one template")
    templates_explain_parser.add_argument("template", help="Template name")
    templates_explain_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    templates_explain_parser.set_defaults(handler=run_templates_explain)


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


def run_traffic_plan(args: Namespace) -> int:
    plan = traffic_plan(
        args.app,
        source_host=args.source_host,
        target_host=args.target_host,
        target_origin=args.target_origin,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        dns_provider=args.dns_provider,
        caddy_provider=args.caddy_provider,
        ttl=args.ttl,
        provider_config=args.provider_config,
        execute_provider_mutation=args.execute_provider_mutation,
        target_health_url=args.target_health_url,
        run_target_health=args.run_target_health,
        target_health_timeout=args.target_health_timeout,
        target_health_expect_status=args.target_health_expect_status,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_traffic_apply(args: Namespace) -> int:
    receipt = traffic_apply(
        args.app,
        source_host=args.source_host,
        target_host=args.target_host,
        target_origin=args.target_origin,
        environment=args.environment,
        runtime_root=args.runtime_root,
        manifest_path=args.manifest,
        dns_provider=args.dns_provider,
        caddy_provider=args.caddy_provider,
        ttl=args.ttl,
        provider_config=args.provider_config,
        execute_provider_mutation=args.execute_provider_mutation,
        target_health_url=args.target_health_url,
        run_target_health=args.run_target_health,
        target_health_timeout=args.target_health_timeout,
        target_health_expect_status=args.target_health_expect_status,
        confirm=args.confirm,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Traffic apply {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Traffic: {receipt.get('traffic_path')}")
        _print_string_items("Blockers", receipt.get("blockers", []))
        _print_string_items("Warnings", receipt.get("warnings", []))
    return 0 if receipt["status"] == "succeeded" else 1


def run_traffic_rollback_plan(args: Namespace) -> int:
    plan = traffic_rollback_plan(
        args.app,
        receipt_id=args.receipt,
        environment=args.environment,
        runtime_root=args.runtime_root,
        approve_unsafe_delete=getattr(args, "approve_unsafe_delete", False),
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        _print_import_plan(plan)
    return 0 if not plan["blockers"] else 1


def run_traffic_rollback_apply(args: Namespace) -> int:
    receipt = traffic_rollback_apply(
        args.app,
        receipt_id=args.receipt,
        environment=args.environment,
        runtime_root=args.runtime_root,
        confirm=args.confirm,
        approve_unsafe_delete=getattr(args, "approve_unsafe_delete", False),
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(receipt["summary"] if "summary" in receipt else f"Traffic rollback apply {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Traffic rollback: {receipt.get('traffic_rollback_path')}")
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


def run_templates_list(args: Namespace) -> int:
    report = templates_list()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{len(report['templates'])} app template(s):")
        for template in report["templates"]:
            print(f"  {template['name']}\t{template['kind']}\t{template['summary']}")
    return 0


def run_templates_explain(args: Namespace) -> int:
    report = templates_explain(args.template)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if report.get("kind") == "ophelia.error":
            print(report.get("error", "Unknown template."))
            return 1
        print(f"{report['name']}: {report['summary']}")
        print(f"  manifest kind: {report['manifest_kind']} (portability {report['portability']})")
        print("  required secrets: " + ", ".join(report["requires_secrets"]))
        print("  files:")
        for path in report["files"]:
            print(f"    - {path}")
    return 0 if report.get("kind") != "ophelia.error" else 1


def run_create_plan(args: Namespace) -> int:
    plan = create_plan(
        app=args.app,
        template=args.template,
        owner=args.owner,
        environment=args.environment,
        runtime_root=args.runtime_root,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(plan["summary"])
        print(f"App: {plan['app']}")
        print(f"Template: {plan.get('template')}")
        print(f"Environment: {plan['environment']}")
        _print_string_items("Blockers", plan.get("blockers", []))
        _print_string_items("Warnings", plan.get("warnings", []))
        if plan.get("files"):
            print("Files to create (only under your --target-dir):")
            for entry in plan["files"]:
                print(f"  - {entry['path']}")
        print(f"Confirmation token: {plan.get('confirmation_token')}")
    return 0 if not plan["blockers"] else 1


def run_create_apply(args: Namespace) -> int:
    if args.plan is not None:
        try:
            plan = json.loads(Path(args.plan).read_text())
        except (OSError, ValueError) as exc:
            return _print_error(f"Could not read plan JSON: {exc}", args.json)
        if not isinstance(plan, dict):
            return _print_error("Plan JSON must be an object.", args.json)
    else:
        parts = str(args.plan_id).split(":")
        if len(parts) < 2:
            return _print_error(
                "--plan-id must be `app:template[:owner[:environment]]`.", args.json
            )
        app = parts[0]
        template = parts[1]
        owner = parts[2] if len(parts) > 2 and parts[2] else "personal"
        environment = parts[3] if len(parts) > 3 and parts[3] else "production"
        plan = create_plan(
            app=app,
            template=template,
            owner=owner,
            environment=environment,
            runtime_root=args.runtime_root,
        )

    receipt = create_apply(
        plan,
        confirm=args.confirm,
        target_dir=args.target_dir,
        runtime_root=args.runtime_root,
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"App create apply {receipt['status']}.")
        if receipt["status"] == "succeeded":
            print(f"Target: {receipt.get('target_dir')}")
            print("Wrote:")
            for path in receipt.get("written_paths", []):
                print(f"  - {path}")
        _print_string_items("Blockers", receipt.get("blockers", []))
    return 0 if receipt["status"] == "succeeded" else 1


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


register_cli_descriptor(
    CommandDescriptor(
        command="ship app templates list",
        operation="app.templates.list",
        summary="List app scaffold templates with required secrets and generated files.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.app_templates",
        args_schema={
            "type": "object",
            "properties": {"json": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.app_templates.v1",
        artifacts=[],
        safety_notes=["Read-only template discovery. No mutation; secrets named only."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship app templates explain",
        operation="app.templates.explain",
        summary="Explain one app scaffold template (manifest shape, secrets, files).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.app_template",
        args_schema={
            "type": "object",
            "properties": {"template": {"type": "string"}, "json": {"type": "boolean"}},
            "required": ["template"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.app_template.v1",
        artifacts=[],
        safety_notes=["Read-only template detail. No mutation; secrets named only."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship app create plan",
        operation="app.create.plan",
        summary="Plan an app scaffold. Read-only: writes nothing and calls no GitHub API.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command="ship app create plan",
        apply_command="ship app create apply",
        json_kind="ophelia.app_create_plan",
        args_schema={
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "template": {"type": "string"},
                "owner": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["app", "template"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.app_create_plan.v1",
        artifacts=["generated scaffold files (preview only)"],
        safety_notes=[
            "Read-only. Writes nothing to disk; GitHub provisioning is described, never executed.",
        ],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship app create apply",
        operation="app.create.apply",
        summary="Write scaffold files only under --target-dir, gated by a confirmation token.",
        risk="medium",
        mutates_state=True,
        requires_confirmation=True,
        plan_command="ship app create plan",
        apply_command="ship app create apply",
        json_kind="ophelia.receipt",
        args_schema={
            "type": "object",
            "properties": {
                "plan": {"type": "string"},
                "plan_id": {"type": "string"},
                "confirm": {"type": "string"},
                "target_dir": {"type": "string"},
                "runtime_root": {"type": "string"},
                "json": {"type": "boolean"},
            },
            "required": ["confirm", "target_dir"],
            "additionalProperties": False,
        },
        output_schema_ref=RECEIPT_SCHEMA_REF,
        artifacts=["scaffolded app files under the target dir", "apply receipt"],
        safety_notes=[
            "Writes only inside the explicit --target-dir; refuses targets inside the runtime root or repo.",
            "No VPS, SSH, or GitHub mutation. Secrets referenced by name only.",
        ],
    )
)
