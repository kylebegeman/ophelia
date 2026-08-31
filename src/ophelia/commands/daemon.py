from __future__ import annotations

import json
import uuid
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import REPO_ROOT
from ..daemon.client import DaemonClient, DaemonClientError
from ..daemon.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_IDENTITY_ROOT,
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_SOCKET_PATH,
    load_daemon_config,
)
from ..daemon.install import apply_daemon_install, daemon_install_plan
from ..daemon.enrollment import apply_enrollment, enrollment_plan
from ..daemon.recovery import (
    apply_clean_host_restore,
    clean_host_restore_plan,
    create_host_backup,
    host_backup_plan,
)
from ..execution import SQLiteOperationJournal
from ..execution.legacy_adapter import local_host_id
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
    install.add_argument(
        "--launcher-path",
        type=Path,
        default=Path("/usr/local/libexec/opheliad-launcher"),
    )
    install.add_argument("--apply", action="store_true")
    install.add_argument(
        "--defer-start",
        action="store_true",
        help="Install and enable the unit without starting it, for clean-host recovery",
    )
    install.add_argument("--confirm")
    install.add_argument("--json", action="store_true")
    install.set_defaults(handler=run_install)

    enroll = commands.add_parser("enroll")
    enroll_commands = enroll.add_subparsers(dest="enroll_command", required=True)
    for name in ("plan", "apply"):
        command = enroll_commands.add_parser(name)
        command.add_argument("--host-id", default=local_host_id())
        command.add_argument("--control-plane", required=True)
        command.add_argument("--token-file", type=Path, required=True)
        command.add_argument(
            "--trust-root", type=Path, default=Path("/etc/ophelia/trust")
        )
        command.add_argument(
            "--identity-root", type=Path, default=DEFAULT_IDENTITY_ROOT
        )
        command.add_argument(
            "--config-path", type=Path, default=Path("/etc/ophelia/agent.toml")
        )
        command.add_argument("--bootstrap-ca", type=Path)
        command.add_argument("--json", action="store_true")
        if name == "apply":
            command.add_argument("--confirm", required=True)
        command.set_defaults(handler=run_enroll)

    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    for name in ("plan", "apply"):
        command = backup_commands.add_parser(name)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        command.add_argument("--backup-id", required=True)
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--json", action="store_true")
        if name == "apply":
            command.add_argument("--confirm", required=True)
        command.set_defaults(handler=run_backup)

    recover = commands.add_parser("recover")
    recover_commands = recover.add_subparsers(dest="recover_command", required=True)
    for name in ("plan", "apply"):
        command = recover_commands.add_parser(name)
        command.add_argument("--backup-root", type=Path, required=True)
        command.add_argument("--identity-file", type=Path, required=True)
        command.add_argument(
            "--target-runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT
        )
        command.add_argument(
            "--maximum-bytes", type=int, default=64 * 1024 * 1024 * 1024
        )
        command.add_argument("--json", action="store_true")
        if name == "apply":
            command.add_argument("--confirm", required=True)
        command.set_defaults(handler=run_recover)


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
            launcher_path=args.launcher_path,
            activate=not args.defer_start,
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


def run_enroll(args: Namespace) -> int:
    try:
        plan = enrollment_plan(
            host_id=args.host_id,
            control_plane_url=args.control_plane,
            token_file=args.token_file,
            trust_root=args.trust_root,
            identity_root=args.identity_root,
            config_path=args.config_path,
            bootstrap_ca_path=args.bootstrap_ca,
        )
        report = (
            apply_enrollment(plan, args.confirm)
            if args.enroll_command == "apply"
            else plan
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print_error(str(exc), "daemon_enrollment_failed", json_output=args.json)
        return 1
    _print(report, args.json)
    return 0 if report.get("can_apply", True) else 2


def run_backup(args: Namespace) -> int:
    try:
        config = load_daemon_config(args.config)
        journal = SQLiteOperationJournal.beneath_runtime_root(config.runtime_root)
        plan = host_backup_plan(
            config,
            journal,
            backup_id=args.backup_id,
            destination_root=args.destination,
        )
        report = (
            create_host_backup(
                config,
                journal,
                backup_id=args.backup_id,
                destination_root=args.destination,
                confirmation=args.confirm,
            )
            if args.backup_command == "apply"
            else plan
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print_error(str(exc), "daemon_backup_failed", json_output=args.json)
        return 1
    _print(report, args.json)
    return 0 if report.get("can_apply", True) else 2


def run_recover(args: Namespace) -> int:
    try:
        plan = clean_host_restore_plan(
            backup_root=args.backup_root,
            identity_file=args.identity_file,
            target_runtime_root=args.target_runtime_root,
            maximum_bytes=args.maximum_bytes,
        )
        report = (
            apply_clean_host_restore(plan, args.confirm)
            if args.recover_command == "apply"
            else plan
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print_error(str(exc), "daemon_recovery_failed", json_output=args.json)
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
