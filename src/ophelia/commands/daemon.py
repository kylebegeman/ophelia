from __future__ import annotations

import json
import uuid
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import REPO_ROOT
from ..daemon.client import DaemonClient, DaemonClientError
from ..daemon.config import DEFAULT_SOCKET_PATH
from ..daemon.install import apply_daemon_install, daemon_install_plan
from ._output import print_error, print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("daemon", help="Inspect and operate the opheliad host authority")
    commands = parser.add_subparsers(dest="daemon_command")

    for name in ("status", "capabilities"):
        command = commands.add_parser(name)
        command.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=run_read)

    events = commands.add_parser("events")
    events.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--limit", type=int, default=250)
    events.add_argument("--json", action="store_true")
    events.set_defaults(handler=run_read)

    for name in ("drain", "maintenance"):
        command = commands.add_parser(name)
        state = command.add_mutually_exclusive_group(required=True)
        state.add_argument("--enable", action="store_true")
        state.add_argument("--disable", action="store_true")
        command.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
        command.add_argument("--idempotency-key")
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=run_host_control)

    task = commands.add_parser("run-task")
    task.add_argument("app")
    task.add_argument("environment")
    task.add_argument("workload")
    task.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    task.add_argument("--idempotency-key")
    task.add_argument("--json", action="store_true")
    task.set_defaults(handler=run_task)

    install = commands.add_parser("install")
    install.add_argument("--source-root", type=Path, default=REPO_ROOT)
    install.add_argument("--install-root", type=Path, default=Path("/opt/ophelia"))
    install.add_argument("--config-path", type=Path, default=Path("/etc/ophelia/agent.toml"))
    install.add_argument(
        "--unit-path", type=Path, default=Path("/etc/systemd/system/opheliad.service")
    )
    install.add_argument("--apply", action="store_true")
    install.add_argument("--confirm")
    install.add_argument("--json", action="store_true")
    install.set_defaults(handler=run_install)


def run_read(args: Namespace) -> int:
    try:
        client = DaemonClient(args.socket)
        if args.daemon_command == "status":
            report = client.get("/v1/health")
        elif args.daemon_command == "capabilities":
            report = client.get("/v1/capabilities")
        else:
            report = client.get(
                "/v1/events", query={"cursor": args.cursor, "limit": args.limit}
            )
    except DaemonClientError as exc:
        print_error(str(exc), exc.code, json_output=args.json)
        return 1
    _print(report, args.json)
    return 0


def run_host_control(args: Namespace) -> int:
    try:
        report = DaemonClient(args.socket).post(
            "/v1/host/" + args.daemon_command,
            {"enabled": bool(args.enable)},
            idempotency_key=args.idempotency_key or _key(args.daemon_command),
        )
    except DaemonClientError as exc:
        print_error(str(exc), exc.code, json_output=args.json)
        return 1
    _print(report, args.json)
    return 0


def run_task(args: Namespace) -> int:
    try:
        report = DaemonClient(args.socket).post(
            "/v1/workload-runs",
            {"app": args.app, "environment": args.environment, "workload": args.workload},
            idempotency_key=args.idempotency_key or _key("task"),
        )
    except DaemonClientError as exc:
        print_error(str(exc), exc.code, json_output=args.json)
        return 1
    _print(report, args.json)
    return 0


def run_install(args: Namespace) -> int:
    try:
        plan = daemon_install_plan(
            source_root=args.source_root,
            install_root=args.install_root,
            config_path=args.config_path,
            unit_path=args.unit_path,
        )
        if args.apply:
            if not args.confirm:
                raise ValueError("--confirm is required with --apply.")
            report = apply_daemon_install(plan, args.confirm)
        else:
            report = plan
    except (OSError, RuntimeError, ValueError) as exc:
        print_error(str(exc), "daemon_install_failed", json_output=args.json)
        return 1
    _print(report, args.json)
    return 0 if report.get("can_apply", True) else 2


def _key(prefix: str) -> str:
    return "%s:%s" % (prefix, uuid.uuid4().hex)


def _print(report: dict, json_output: bool) -> None:
    if json_output:
        print_json(report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
