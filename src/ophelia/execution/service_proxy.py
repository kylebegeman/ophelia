"""Small reload-free TCP switch for product release candidates.

The proxy reads Ophelia's atomically replaced active-state document for every
new connection.  Existing connections drain naturally while new connections
move to the selected loopback candidate.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import socket
import socketserver
from pathlib import Path
from typing import Tuple


MAX_STATE_BYTES = 16 * 1024
BUFFER_BYTES = 64 * 1024


def _upstream(path: Path) -> Tuple[str, int]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("active state is unavailable")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_object,
    )
    required = {
        "schema_version",
        "revision_id",
        "revision_digest",
        "upstream_host",
        "upstream_port",
        "artifact_digest",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema_version") != 1
    ):
        raise ValueError("active state is malformed")
    host, port = value.get("upstream_host"), value.get("upstream_port")
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("active upstream must be loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("active upstream port is invalid")
    return host, port


def _strict_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"active state repeats object key: {key}")
        value[key] = item
    return value


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, ProductTCPProxy):
            return
        try:
            target = socket.create_connection(
                _upstream(server.state_path), timeout=server.connect_timeout
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return
        try:
            self.request.setblocking(False)
            target.setblocking(False)
            selector = selectors.DefaultSelector()
            selector.register(self.request, selectors.EVENT_READ, target)
            selector.register(target, selectors.EVENT_READ, self.request)
            while True:
                events = selector.select(timeout=server.idle_timeout)
                if not events:
                    return
                for key, _ in events:
                    destination = key.data
                    try:
                        data = key.fileobj.recv(BUFFER_BYTES)
                    except (BlockingIOError, ConnectionError, OSError):
                        return
                    if not data:
                        return
                    try:
                        destination.sendall(data)
                    except (ConnectionError, OSError):
                        return
        finally:
            target.close()


class ProductTCPProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        state_path: Path,
        *,
        connect_timeout: float = 3.0,
        idle_timeout: float = 60.0,
    ) -> None:
        self.state_path = state_path
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        super().__init__(address, _ProxyHandler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--pid-file", required=True, type=Path)
    args = parser.parse_args(argv)
    args.pid_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(args.pid_file, flags, 0o600)
    try:
        os.write(descriptor, (str(os.getpid()) + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        with ProductTCPProxy((args.host, args.port), args.state) as server:
            server.serve_forever(poll_interval=0.25)
    finally:
        try:
            args.pid_file.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
