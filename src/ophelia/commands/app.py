from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..app_registry import app_health, app_logs, find_app


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


def run_health(args: Namespace) -> int:
    entry = find_app(args.name, args.registry)
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
    entry = find_app(args.name, args.registry)
    report = app_logs(entry, tail=args.tail, container=args.container)
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
