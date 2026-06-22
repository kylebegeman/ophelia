from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..caddy_manager import reload_caddy, validate_caddy
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("caddy", help="Validate and reload shared Caddy")
    caddy_subparsers = parser.add_subparsers(dest="caddy_command")

    validate = caddy_subparsers.add_parser("validate", help="Validate shared Caddy config")
    _add_common(validate)
    validate.set_defaults(handler=run_validate)

    reload = caddy_subparsers.add_parser("reload", help="Validate then reload shared Caddy")
    _add_common(reload)
    reload.set_defaults(handler=run_reload)


def run_validate(args: Namespace) -> int:
    report = validate_caddy(runtime_root=args.runtime_root, ophelia_root=args.ophelia_root)
    return _print_result(report, args.json)


def run_reload(args: Namespace) -> int:
    report = reload_caddy(runtime_root=args.runtime_root, ophelia_root=args.ophelia_root)
    return _print_result(report, args.json)


def _add_common(parser) -> None:
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--ophelia-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def _print_result(report: dict, emit_json: bool) -> int:
    if emit_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if report.get("stdout"):
            print(report["stdout"])
        if report.get("stderr"):
            print(report["stderr"])
        print("ok" if report["returncode"] == 0 else "failed")
    return int(report["returncode"])
