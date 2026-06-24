from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..api import serve
from ..config import DEFAULT_RUNTIME_ROOT


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("api", help="Run the local-only Ophelia action API")
    api_subparsers = parser.add_subparsers(dest="api_command")
    serve_parser = api_subparsers.add_parser("serve", help="Serve the local API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    serve_parser.set_defaults(handler=run_serve)


def run_serve(args: Namespace) -> int:
    serve(args.host, args.port, args.runtime_root)
    return 0
