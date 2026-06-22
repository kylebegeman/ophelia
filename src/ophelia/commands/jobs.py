from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..actions import ActionError, cancel_job, run_job
from ..config import DEFAULT_RUNTIME_ROOT
from ..operation_schema import error_envelope
from ._output import print_json


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("jobs", help="Run or inspect local Ophelia action jobs")
    job_subparsers = parser.add_subparsers(dest="jobs_command")

    run_parser = job_subparsers.add_parser("run", help="Run one action job")
    run_parser.add_argument("action_id")
    run_parser.add_argument("--input-json", required=True, help="JSON object of action inputs")
    run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    run_parser.add_argument("--requested-by", default="unknown")
    run_parser.add_argument("--source", default="cli")
    run_parser.add_argument("--idempotency-key")
    run_parser.add_argument("--events-json", action="store_true")
    run_parser.set_defaults(handler=run_action_job)

    show_parser = job_subparsers.add_parser("show", help="Show one job")
    show_parser.add_argument("job_id")
    show_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    show_parser.set_defaults(handler=show_job)

    cancel_parser = job_subparsers.add_parser("cancel", help="Cancel a queued or waiting job")
    cancel_parser.add_argument("job_id")
    cancel_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    cancel_parser.set_defaults(handler=cancel_action_job)


def run_action_job(args: Namespace) -> int:
    try:
        inputs = json.loads(args.input_json)
        if not isinstance(inputs, dict):
            raise ValueError("input-json must be an object")
        result = run_job(
            args.action_id,
            inputs,
            args.runtime_root,
            requested_by=args.requested_by,
            source=args.source,
            idempotency_key=args.idempotency_key,
            events_json=args.events_json,
        )
    except (ValueError, ActionError) as exc:
        print_json({"ok": False, **error_envelope(str(exc), "job_run_error")})
        return 1
    print(json.dumps(result.job, indent=2, sort_keys=True))
    return 0 if result.job["state"] not in {"failed", "cancelled"} else 1


def show_job(args: Namespace) -> int:
    path = args.runtime_root / "jobs" / f"{args.job_id}.json"
    if not path.exists():
        print_json({"ok": False, **error_envelope(f"Job not found: {args.job_id}", "job_not_found")})
        return 1
    print(path.read_text().strip())
    return 0


def cancel_action_job(args: Namespace) -> int:
    try:
        job = cancel_job(args.runtime_root, args.job_id)
    except ActionError as exc:
        print_json({"ok": False, **error_envelope(str(exc), "job_cancel_error")})
        return 1
    print(json.dumps(job, indent=2, sort_keys=True))
    return 0
