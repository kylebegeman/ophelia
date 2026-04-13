from __future__ import annotations

import argparse
import sys

from .commands import register_commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ship", description="Ophelia deployment control plane")
    subparsers = parser.add_subparsers(dest="command")
    register_commands(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
