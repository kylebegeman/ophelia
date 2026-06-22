from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from ..live_readiness import live_readiness_report


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "live-readiness",
        help="Run the read-only live-readiness lane across real runtime data",
    )
    live_subparsers = parser.add_subparsers(dest="live_readiness_command")

    run_parser = live_subparsers.add_parser(
        "run",
        help="Aggregate read-only staging/prod readiness checks without mutation",
    )
    run_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    run_parser.add_argument("--manifests-dir", type=Path, default=REPO_ROOT / "manifests")
    run_parser.add_argument("--ophelia-root", type=Path, default=REPO_ROOT)
    run_parser.add_argument("--app", help="Limit checks to one app")
    run_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    run_parser.add_argument("--manifest", type=Path, help="Explicit app manifest path")
    run_parser.add_argument("--host-config", type=Path, help="Optional host inventory config JSON/YAML")
    run_parser.add_argument("--provider-config", type=Path, help="Optional integrations config JSON/YAML")
    run_parser.add_argument("--from", dest="source_host", help="Optional source host id for placement locality")
    run_parser.add_argument("--to", dest="target_host", help="Optional target host id for placement comparison")
    run_parser.add_argument("--probe-http", action="store_true", help="Opt in to bounded read-only HTTP health probes")
    run_parser.add_argument("--check-docker", action="store_true", help="Opt in to bounded read-only docker status checks")
    run_parser.add_argument("--http-timeout", type=float, default=5.0)
    run_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    run_parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    report = live_readiness_report(
        runtime_root=args.runtime_root,
        manifests_dir=args.manifests_dir,
        ophelia_root=args.ophelia_root,
        app=args.app,
        environment=args.environment,
        manifest_path=args.manifest,
        host_config=args.host_config,
        provider_config=args.provider_config,
        source_host=args.source_host,
        target_host=args.target_host,
        probe_http=args.probe_http,
        check_docker=args.check_docker,
        http_timeout=args.http_timeout,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"Status: {report['status']}")
        print(f"Apps: {report['totals']['app_count']}")
        print(f"Probes: http={report['probe_policy']['http']['enabled']} docker={report['probe_policy']['docker']['enabled']}")
        for entry in report.get("apps", []):
            if isinstance(entry, dict):
                print(
                    f"  - {entry.get('app')}/{entry.get('environment')}: "
                    f"{entry.get('status')} drift={'yes' if entry.get('drift_detected') else 'no'}"
                )
        _print_items("Blockers", report.get("blockers", []))
        _print_items("Warnings", report.get("warnings", []))
    return 0 if report.get("status") != "blocked" else 1


def _print_items(label: str, value: object) -> None:
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
        command="ship live-readiness run",
        operation="live.readiness.run",
        summary="Aggregate read-only live/staging readiness checks across runtime, manifests, providers, hosts, placement, observability, and drift.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.live_readiness_report",
        args_schema={
            "type": "object",
            "properties": {
                "runtime_root": {"type": "string"},
                "manifests_dir": {"type": "string"},
                "ophelia_root": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"enum": ["dev", "staging", "production"]},
                "manifest": {"type": "string"},
                "host_config": {"type": "string"},
                "provider_config": {"type": "string"},
                "source_host": {"type": "string"},
                "target_host": {"type": "string"},
                "probe_http": {"type": "boolean"},
                "check_docker": {"type": "boolean"},
                "http_timeout": {"type": "number"},
                "json": {"type": "boolean"},
            },
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.live_readiness_report.v1",
        artifacts=[],
        safety_notes=[
            "Read-only aggregate. Does not call apply/create/rebuild/refresh operations or write artifacts.",
            "HTTP and Docker checks are disabled by default and must be opted in explicitly.",
            "All child reports are swept through deep redaction before output.",
        ],
    )
)

