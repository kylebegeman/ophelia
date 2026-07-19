"""``opheliad`` process entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from .api import serve_unix
from .config import DEFAULT_CONFIG_PATH, load_daemon_config
from .service import OpheliaDaemon


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opheliad",
        description="Ophelia's durable single-host runtime authority",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    args = parser.parse_args(argv)
    config = load_daemon_config(
        args.config,
        runtime_root=args.runtime_root,
        socket_path=args.socket,
    )
    if args.check_config or args.print_config:
        payload = {
            "ok": True,
            "schema_version": 1,
            "kind": "ophelia.daemon-config",
            "config": config.redacted_dict(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    serve_unix(OpheliaDaemon(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
