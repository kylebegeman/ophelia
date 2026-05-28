from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..config import DEFAULT_RUNTIME_ROOT
from ..runtime import list_deployments


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List locally deployed runtime bundles")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="Runtime root to inspect",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    deployments = list_deployments(args.runtime_root)
    if not deployments:
        print(f"No deployments found in {args.runtime_root}")
        return 0

    print("APP\tKIND\tENVIRONMENT\tLATEST\tACTIVE\tDEPLOYED AT\tRUNTIME PATH")
    for record in deployments:
        print(
            f"{record.app}\t{record.kind}\t{record.environment or 'unknown'}\t"
            f"{record.release_id or 'unknown'}\t{record.active_release_id or 'none'}\t"
            f"{record.deployed_at}\t{record.runtime_path}"
        )
    return 0
