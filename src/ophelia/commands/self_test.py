from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..self_test import run_self_test


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "self-test", help="Run local install self-test checks (read-only)"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--check-docker", action="store_true", help="Also check docker availability"
    )
    parser.add_argument(
        "--check-git", action="store_true", help="Also check git availability"
    )
    parser.add_argument(
        "--check-api", action="store_true", help="Also check the HTTP API module imports"
    )
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    result = run_self_test(
        check_docker=args.check_docker,
        check_git=args.check_git,
        check_api=args.check_api,
        runtime_root=args.runtime_root,
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        package = result["package"]
        version = package.get("version") or "unknown"
        print(f"ophelia self-test: {result['status']} (ophelia {version})")
        for check in result["checks"]:
            message = check.get("message", "")
            suffix = f" - {message}" if message else ""
            print(f"  [{check['status']}] {check['name']}{suffix}")
        for blocker in result["blockers"]:
            print(f"  blocker: {blocker['check']}: {blocker['message']}")

    return 0 if result["status"] != "blocked" else 1


register_cli_descriptor(
    CommandDescriptor(
        command="ship self-test",
        operation="self_test",
        summary="Run local install self-test checks (entrypoint, templates, runtime root).",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.self_test",
        args_schema={
            "type": "object",
            "properties": {
                "json": {"type": "boolean"},
                "check_docker": {"type": "boolean"},
                "check_git": {"type": "boolean"},
                "check_api": {"type": "boolean"},
                "runtime_root": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.self_test.v1",
        artifacts=[],
        safety_notes=[
            "Read-only diagnostics. Resolves paths but creates nothing and mutates no state.",
        ],
    )
)
