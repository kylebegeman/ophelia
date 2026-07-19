"""Versioned HTTP/JSON protocol over an authenticated Unix socket."""

from __future__ import annotations

import json
import os
import re
import socket
import socketserver
import stat
import struct
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..domain import Actor, OperationState
from ..execution import IdempotencyConflict, OperationConflict
from .config import PROTOCOL_VERSION
from .service import OpheliaDaemon
from .systemd import notify_systemd


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_ENTITY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64


def serve_unix(daemon: OpheliaDaemon) -> None:
    server = create_unix_server(daemon)
    path = daemon.config.socket_path
    daemon.start()
    try:
        notify_systemd("READY=1\nSTATUS=Ophelia host authority is ready")
        server.serve_forever(poll_interval=0.25)
    finally:
        try:
            notify_systemd("STOPPING=1\nSTATUS=Ophelia host authority is stopping")
        finally:
            server.server_close()
            daemon.stop()
            if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()


def create_unix_server(daemon: OpheliaDaemon) -> UnixHTTPServer:
    path = daemon.config.socket_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        _remove_stale_socket(path)

    class Handler(DaemonAPIHandler):
        daemon_service = daemon

    server = UnixHTTPServer(str(path), Handler)
    os.chmod(path, 0o600)
    return server


class DaemonAPIHandler(BaseHTTPRequestHandler):
    daemon_service: OpheliaDaemon
    server_version = "opheliad"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        # Request paths can contain caller data. Structured daemon logging is
        # emitted elsewhere and never writes raw HTTP lines by default.
        return

    def _dispatch(self, method: str) -> None:
        request_id = self.headers.get("X-Request-ID", "")
        try:
            self._validate_protocol(request_id)
            actor = self._actor()
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if method == "GET":
                status, payload = self._get(parsed.path, query)
            else:
                idempotency_key = self._idempotency_key()
                body = self._read_json()
                status, payload = self._post(parsed.path, body, actor, idempotency_key)
            self._json(status, request_id, payload)
        except KeyError:
            self._error(404, request_id, "not_found", "The requested Ophelia resource was not found.")
        except PermissionError as exc:
            self._error(403, request_id, "forbidden", str(exc))
        except IdempotencyConflict as exc:
            self._error(409, request_id, "idempotency_conflict", str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, request_id, "invalid_request", str(exc))
        except OperationConflict as exc:
            self._error(409, request_id, "operation_conflict", str(exc))
        except Exception:
            self._error(
                500,
                request_id,
                "internal_error",
                "The host authority could not complete the request; inspect daemon health.",
            )

    def _get(self, path: str, query: Dict[str, list[str]]):
        service = self.daemon_service
        if path == "/v1/health":
            return 200, service.health()
        if path == "/v1/capabilities":
            return 200, service.capabilities()
        if path == "/v1/hosts/self":
            return 200, service.store.host(service.config.host_id)
        if path == "/v1/apps":
            return 200, service.apps()
        if path.startswith("/v1/apps/"):
            parts = path.split("/")
            if len(parts) != 5:
                raise KeyError(path)
            return 200, service.app(_entity(parts[3]), _entity(parts[4]))
        if path == "/v1/operations":
            state_text = _one(query, "state")
            state = None if state_text is None else OperationState(state_text)
            return 200, {
                "operations": list(
                    service.journal.list_operations(
                        limit=_query_int(query, "limit", 100, maximum=1000),
                        state=state,
                    )
                )
            }
        if path.startswith("/v1/operations/"):
            parts = path.split("/")
            if len(parts) == 4:
                return 200, service.operation(parts[3])
        if path == "/v1/events":
            cursor = _query_int(query, "cursor", 0, maximum=2**63 - 1)
            events = service.journal.host_events_after(
                cursor,
                limit=_query_int(query, "limit", 250, maximum=1000),
            )
            return 200, {
                "events": list(events),
                "next_cursor": cursor if not events else events[-1]["cursor"],
            }
        if path == "/v1/workload-runs":
            return 200, {
                "workload_runs": [
                    item.to_dict()
                    for item in service.store.list_runs(
                        limit=_query_int(query, "limit", 100, maximum=1000)
                    )
                ]
            }
        if path.startswith("/v1/workload-runs/") and len(path.split("/")) == 4:
            return 200, service.store.run(path.split("/")[3]).to_dict()
        raise KeyError(path)

    def _post(
        self,
        path: str,
        body: Dict[str, Any],
        actor: Actor,
        idempotency_key: str,
    ):
        service = self.daemon_service
        if path == "/v1/plans":
            _exact_keys(body, {"manifest_path"})
            return 201, service.plan(
                Path(_text(body.get("manifest_path"), "manifest_path")),
                actor=actor,
                idempotency_key=idempotency_key,
            )
        if path == "/v1/operations":
            _exact_keys(body, {"plan_id", "confirmation"})
            return 202, service.submit_operation(
                _text(body.get("plan_id"), "plan_id"),
                _text(body.get("confirmation"), "confirmation"),
                actor=actor,
            )
        if path.startswith("/v1/operations/") and path.endswith("/cancel"):
            parts = path.split("/")
            if len(parts) != 5:
                raise KeyError(path)
            _exact_keys(body, set())
            return 202, service.cancel_operation(parts[3], actor=actor)
        if path == "/v1/workload-runs":
            _exact_keys(body, {"app", "environment", "workload"})
            return 202, service.accept_task(
                actor=actor,
                app=_entity(body.get("app")),
                environment=_entity(body.get("environment")),
                workload_name=_entity(body.get("workload")),
                idempotency_key=idempotency_key,
            )
        if path.startswith("/v1/workload-runs/") and path.endswith("/cancel"):
            parts = path.split("/")
            if len(parts) != 5:
                raise KeyError(path)
            _exact_keys(body, set())
            return 202, service.store.request_cancellation(parts[3], actor).to_dict()
        if path == "/v1/events/acknowledge":
            _exact_keys(body, {"consumer_id", "cursor"})
            cursor = body.get("cursor")
            if isinstance(cursor, bool) or not isinstance(cursor, int):
                raise ValueError("cursor must be an integer.")
            acknowledged = service.journal.acknowledge_host_events(
                _text(body.get("consumer_id"), "consumer_id"), cursor
            )
            return 200, {"acknowledged_cursor": acknowledged}
        if path == "/v1/host/drain":
            _exact_keys(body, {"enabled"})
            return 200, service.store.set_host_control(
                service.config.host_id,
                drained=_boolean(body.get("enabled"), "enabled"),
            )
        if path == "/v1/host/maintenance":
            _exact_keys(body, {"enabled"})
            return 200, service.store.set_host_control(
                service.config.host_id,
                maintenance_mode=_boolean(body.get("enabled"), "enabled"),
            )
        raise KeyError(path)

    def _validate_protocol(self, request_id: str) -> None:
        if _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("X-Request-ID is required and must be a bounded identifier.")
        value = self.headers.get("X-Ophelia-Protocol-Version")
        if value != str(PROTOCOL_VERSION):
            raise ValueError(
                "Unsupported protocol version; this host requires version %d."
                % PROTOCOL_VERSION
            )

    def _idempotency_key(self) -> str:
        value = self.headers.get("Idempotency-Key", "")
        if not value or len(value) > 255 or "\x00" in value:
            raise ValueError("Idempotency-Key is required for mutation requests.")
        return value

    def _read_json(self) -> Dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length is invalid.") from exc
        if length < 0 or length > self.daemon_service.config.max_request_bytes:
            raise ValueError("Request body exceeds the configured size limit.")
        if length == 0:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Mutation requests require application/json.")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Request body is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request JSON must be an object.")
        return value

    def _actor(self) -> Actor:
        uid = _peer_uid(self.connection)
        if uid not in self.daemon_service.config.allowed_uids:
            raise PermissionError("Unix peer is not authorized by host policy.")
        return Actor(
            actor_id="actor_unix-%d" % uid,
            source="opheliad-unix-api",
            authenticated_by="unix-peer-credentials",
        )

    def _json(self, status: int, request_id: str, payload: Any) -> None:
        envelope = {
            "schema_version": 1,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "ok": status < 400,
            "data": payload,
        }
        data = (json.dumps(envelope, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, request_id: str, code: str, message: str) -> None:
        self._json(status, request_id, {"error": {"code": code, "message": message}})


def _peer_uid(connection: socket.socket) -> int:
    if hasattr(connection, "getpeereid"):
        return int(connection.getpeereid()[0])
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", credentials)
        return int(uid)
    if hasattr(socket, "LOCAL_PEERCRED"):
        # Darwin xucred begins with cr_version (uint32) and cr_uid (uid_t).
        credentials = connection.getsockopt(0, socket.LOCAL_PEERCRED, 12)
        if len(credentials) < 8:
            raise PermissionError("Unix peer credentials are incomplete.")
        version, uid = struct.unpack_from("II", credentials)
        if version != 0:
            raise PermissionError("Unix peer credential version is unsupported.")
        return int(uid)
    raise PermissionError("This platform cannot authenticate Unix peers.")


def _remove_stale_socket(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError("Refusing to replace an unsafe daemon socket path.")
    path.unlink()


def _exact_keys(value: Dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("Request fields must be exactly: %s." % ", ".join(sorted(expected)))


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("%s must be bounded non-empty text." % field)
    return value


def _entity(value: object) -> str:
    if not isinstance(value, str) or _ENTITY.fullmatch(value) is None:
        raise ValueError("Resource identifier is invalid.")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("%s must be boolean." % field)
    return value


def _one(query: Dict[str, list[str]], name: str) -> Optional[str]:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise ValueError("Query parameter %s must occur exactly once." % name)
    return values[0]


def _query_int(
    query: Dict[str, list[str]],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    value = _one(query, name)
    if value is None:
        return default
    if not value.isdigit() or int(value) > maximum:
        raise ValueError("Query parameter %s is invalid." % name)
    return int(value)
