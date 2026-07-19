"""Outbound fleet agent with replayable events and durable command execution."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from ..domain import Actor, canonical_digest
from ..domain._contracts import parse_utc
from ..manifest_v2_execution import load_manifest_v2_plan
from ..version import package_version
from .agent_store import AgentCommand, AgentStore
from .agent_transport import AgentAuthenticationError, AgentTransport
from .config import PROTOCOL_VERSION
from .decisions import (
    approval_from_dict,
    verify_lumen_decision,
    verify_signed_lumen_document,
)
from .identity import rotate_host_certificate
from .upgrades import stage_agent_upgrade

if TYPE_CHECKING:
    from .service import OpheliaDaemon


_COMMAND_ID = re.compile(r"^command_[A-Za-z0-9_-]{1,247}$")
_COMMAND_FIELDS = {
    "schema_version",
    "kind",
    "command_id",
    "sequence",
    "operation",
    "actor_id",
    "audience",
    "scopes",
    "idempotency_key",
    "issued_at",
    "expires_at",
    "payload",
    "signature",
}
_RESPONSE_FIELDS = {
    "schema_version",
    "kind",
    "protocol_version",
    "exchange_id",
    "host_id",
    "acknowledged_event_cursor",
    "acknowledged_command_sequence",
    "commands",
    "poll_after_seconds",
}
_OPERATION_SCOPES = {
    "host.drain": "ophelia:host:drain",
    "host.maintenance": "ophelia:host:maintenance",
    "workload.run": "ophelia:workload:run",
    "operation.cancel": "ophelia:operation:cancel",
    "manifest.plan": "ophelia:plan:create",
    "deploy.apply": "ophelia:deploy:apply",
    "host.certificate.rotate": "ophelia:host:identity:rotate",
    "host.upgrade": "ophelia:host:upgrade",
}


class OutboundHostAgent:
    def __init__(
        self,
        service: "OpheliaDaemon",
        transport: AgentTransport,
        *,
        command_verifier: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self.service = service
        self.transport = transport
        self._command_verifier = command_verifier or self._verify_command_signature
        self.store = AgentStore(service.journal)
        self.store.register(service.config.host_id)

    def run_once(self) -> float:
        self._execute_pending()
        host_id = self.service.config.host_id
        event_cursor = self.service.journal.host_event_acknowledgement("control-plane")
        events = self.service.journal.host_events_after(
            event_cursor,
            limit=self.service.config.agent_event_batch,
        )
        command_results = self.store.pending_results(
            host_id,
            limit=self.service.config.agent_command_batch,
        )
        request = {
            "schema_version": 1,
            "kind": "ophelia.agent-exchange",
            "protocol_version": PROTOCOL_VERSION,
            "exchange_id": "exchange_" + uuid.uuid4().hex,
            "host_id": host_id,
            "agent_version": package_version(),
            "capabilities": self.service.capabilities(),
            "host": self.service.store.host(host_id),
            "health": self.service.health(),
            "event_cursor": event_cursor,
            "events": list(events),
            "command_results": list(command_results),
        }
        try:
            response = self.transport.exchange(request)
            self._accept_response(
                response,
                exchange_id=request["exchange_id"],
                event_cursor=event_cursor,
                events=events,
                command_results=command_results,
            )
            self._execute_pending()
            self.store.record_connection(host_id, connected=True)
        except Exception as exc:
            self.store.record_connection(
                host_id,
                connected=False,
                revoked=isinstance(exc, AgentAuthenticationError),
                error_type=type(exc).__name__,
            )
            raise
        return float(response["poll_after_seconds"])

    def _accept_response(
        self,
        response: Dict[str, Any],
        *,
        exchange_id: str,
        event_cursor: int,
        events,
        command_results,
    ) -> None:
        if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS:
            raise ValueError("Control-plane exchange response fields are invalid.")
        if (
            response.get("schema_version") != 1
            or response.get("kind") != "ophelia.agent-exchange-response"
            or response.get("protocol_version") != PROTOCOL_VERSION
            or response.get("exchange_id") != exchange_id
            or response.get("host_id") != self.service.config.host_id
        ):
            raise ValueError("Control-plane exchange identity is invalid.")
        poll = response.get("poll_after_seconds")
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0.25 <= poll <= 300:
            raise ValueError("Control-plane poll interval is invalid.")
        acknowledged_event = _nonnegative_int(
            response.get("acknowledged_event_cursor"), "acknowledged_event_cursor"
        )
        highest_event = event_cursor if not events else events[-1]["cursor"]
        if acknowledged_event > highest_event:
            raise ValueError("Control plane acknowledged an event that was not delivered.")
        acknowledged_command = _nonnegative_int(
            response.get("acknowledged_command_sequence"),
            "acknowledged_command_sequence",
        )
        state = self.store.state(self.service.config.host_id)
        highest_result = state["acknowledged_command_sequence"]
        if command_results:
            highest_result = command_results[-1]["sequence"]
        if acknowledged_command > highest_result:
            raise ValueError("Control plane acknowledged a result that was not delivered.")
        self.service.journal.acknowledge_host_events(
            "control-plane", acknowledged_event
        )
        self.store.acknowledge_results(
            self.service.config.host_id, acknowledged_command
        )
        commands = response.get("commands")
        if not isinstance(commands, list) or len(commands) > self.service.config.agent_command_batch:
            raise ValueError("Control-plane command batch is invalid.")
        for raw in commands:
            command = self._validate_command(raw)
            preparation_error = None
            try:
                prepared = self._prepare_payload(command)
            except Exception as exc:
                preparation_error = type(exc).__name__
                prepared = {
                    "preparation_failed": True,
                    "payload_digest": canonical_digest(command["payload"]),
                    "error_type": preparation_error,
                }
            accepted = self.store.accept(
                command_id=command["command_id"],
                sequence=command["sequence"],
                operation=command["operation"],
                actor_id=command["actor_id"],
                idempotency_key=command["idempotency_key"],
                envelope_digest=command["_envelope_digest"],
                payload=prepared,
            )
            if preparation_error is not None:
                self.store.complete(
                    accepted.command_id,
                    succeeded=False,
                    result={
                        "schema_version": 1,
                        "kind": "ophelia.agent-command-result",
                        "command_id": accepted.command_id,
                        "status": "failed",
                        "error_code": "command_rejected",
                        "error_type": preparation_error,
                    },
                )

    def _validate_command(self, value: object) -> Dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _COMMAND_FIELDS:
            raise ValueError("Control-plane command fields are invalid.")
        if value.get("schema_version") != 1 or value.get("kind") != "lumen.ophelia-command":
            raise ValueError("Control-plane command version or kind is invalid.")
        if not isinstance(value.get("command_id"), str) or _COMMAND_ID.fullmatch(value["command_id"]) is None:
            raise ValueError("Control-plane command id is invalid.")
        _nonnegative_int(value.get("sequence"), "sequence", positive=True)
        if value.get("audience") != self.service.config.host_id:
            raise ValueError("Control-plane command audience is invalid.")
        for field, maximum in (("operation", 128), ("actor_id", 255), ("idempotency_key", 255)):
            item = value.get(field)
            if not isinstance(item, str) or not item or len(item) > maximum or "\x00" in item:
                raise ValueError("Control-plane command %s is invalid." % field)
        if not value["actor_id"].startswith("actor_") or not isinstance(value.get("payload"), dict):
            raise ValueError("Control-plane command actor or payload is invalid.")
        scopes = value.get("scopes")
        if (
            not isinstance(scopes, list)
            or len(scopes) > 64
            or len(scopes) != len(set(scopes))
            or any(
                not isinstance(scope, str)
                or not scope
                or len(scope) > 255
                or "\x00" in scope
                for scope in scopes
            )
        ):
            raise ValueError("Control-plane command scopes are invalid.")
        required_scope = _OPERATION_SCOPES.get(value["operation"])
        if required_scope is not None and required_scope not in scopes:
            raise PermissionError("Control-plane command is missing its operation scope.")
        now = datetime.now(timezone.utc)
        issued_at = parse_utc(_text(value.get("issued_at"), "issued_at"))
        expires_at = parse_utc(_text(value.get("expires_at"), "expires_at"))
        if (
            issued_at > now + timedelta(minutes=2)
            or expires_at <= now
            or expires_at <= issued_at
            or expires_at > issued_at + timedelta(minutes=15)
        ):
            raise ValueError("Control-plane command timing is invalid or expired.")
        envelope_digest = self._command_verifier(value)
        return {**value, "_envelope_digest": envelope_digest}

    def _verify_command_signature(self, command: Dict[str, Any]) -> str:
        key = self.service.config.decision_public_key_path
        if key is None:
            raise ValueError("Lumen command verification key is unavailable.")
        return verify_signed_lumen_document(
            command,
            public_key_path=key,
            runtime_root=self.service.config.runtime_root,
        )

    def _prepare_payload(self, command: Dict[str, Any]) -> Dict[str, Any]:
        payload = command["payload"]
        operation = command["operation"]
        if operation == "manifest.plan":
            return self._materialize_manifest_bundle(command["command_id"], payload)
        if operation == "host.upgrade":
            return self._materialize_upgrade_bundle(command["command_id"], payload)
        if operation == "deploy.apply":
            if set(payload) != {"plan_id", "decision"}:
                raise ValueError("Deploy command payload fields are invalid.")
            plan_id = _text(payload.get("plan_id"), "plan_id")
            loaded = load_manifest_v2_plan(self.service.config.runtime_root, plan_id)
            actor = self._actor(command["actor_id"])
            key = self.service.config.decision_public_key_path
            if key is None:
                raise ValueError("Lumen decision verification key is unavailable.")
            approval = verify_lumen_decision(
                payload.get("decision"),
                plan=loaded["plan"],
                actor=actor,
                public_key_path=key,
                runtime_root=self.service.config.runtime_root,
            )
            return {"plan_id": plan_id, "approval": approval.to_dict()}
        if operation in {"host.drain", "host.maintenance"}:
            return {"enabled": _boolean_only(payload, "enabled")}
        if operation == "workload.run":
            if set(payload) != {"app", "environment", "workload"}:
                raise ValueError("Workload-run command fields are invalid.")
            return {
                "app": _text(payload.get("app"), "app"),
                "environment": _text(payload.get("environment"), "environment"),
                "workload": _text(payload.get("workload"), "workload"),
            }
        if operation == "operation.cancel":
            if set(payload) != {"operation_id"}:
                raise ValueError("Operation-cancel command fields are invalid.")
            return {"operation_id": _text(payload.get("operation_id"), "operation_id")}
        if operation == "host.certificate.rotate":
            if payload:
                raise ValueError("Certificate-rotation command payload must be empty.")
            return {}
        raise ValueError("Agent command operation is not supported by this host.")

    def _materialize_upgrade_bundle(
        self, command_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        if set(payload) != {"version", "source_archive_base64", "sha256"}:
            raise ValueError("Agent-upgrade bundle fields are invalid.")
        version = _text(payload.get("version"), "version")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]{0,64})?", version) is None:
            raise ValueError("Agent-upgrade version is invalid.")
        content = self._decode_bounded_base64(
            payload.get("source_archive_base64"),
            "source_archive_base64",
            maximum_bytes=self.service.config.agent_exchange_bytes,
        )
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if payload.get("sha256") != digest:
            raise ValueError("Agent-upgrade source archive digest is invalid.")
        root = self.service.config.runtime_root / "inbox" / command_id
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Agent-upgrade inbox path is unsafe.")
        archive = root / "ophelia-source.tar.gz"
        _write_or_verify(archive, content)
        return {
            "version": version,
            "archive_path": str(archive),
            "source_digest": digest,
        }

    def _decode_bounded_base64(
        self, value: object, field: str, *, maximum_bytes: Optional[int] = None
    ) -> bytes:
        maximum = maximum_bytes or self.service.config.max_request_bytes
        encoded_limit = ((maximum + 2) // 3) * 4
        if (
            not isinstance(value, str)
            or not value
            or len(value) > encoded_limit
            or "\x00" in value
        ):
            raise ValueError("%s is invalid or too large." % field)
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("%s is not valid base64." % field) from exc
        if len(decoded) > maximum:
            raise ValueError("%s exceeds the host size limit." % field)
        return decoded

    def _materialize_manifest_bundle(
        self, command_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        if set(payload) != {"manifest", "files"}:
            raise ValueError("Manifest-plan bundle fields are invalid.")
        manifest = payload.get("manifest")
        files = payload.get("files")
        if not isinstance(manifest, str) or not manifest or len(manifest.encode("utf-8")) > 262144:
            raise ValueError("Manifest-plan document is invalid or too large.")
        if not isinstance(files, list) or len(files) > 256:
            raise ValueError("Manifest-plan source file list is invalid.")
        values: Dict[Path, bytes] = {Path("manifest.ophelia.yml"): manifest.encode("utf-8")}
        total = len(values[Path("manifest.ophelia.yml")])
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "content_base64", "sha256"}:
                raise ValueError("Manifest-plan source file entry is invalid.")
            relative = _safe_relative(_text(item.get("path"), "path"))
            content = self._decode_bounded_base64(
                item.get("content_base64"), "content_base64"
            )
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            if item.get("sha256") != digest or relative in values:
                raise ValueError("Manifest-plan source digest or path is invalid.")
            total += len(content)
            if total > self.service.config.max_request_bytes:
                raise ValueError("Manifest-plan bundle exceeds the host size limit.")
            values[relative] = content
        root = self.service.config.runtime_root / "inbox" / command_id
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Manifest-plan inbox path is unsafe.")
        members = []
        for relative, content in sorted(values.items(), key=lambda item: item[0].as_posix()):
            target = root / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_or_verify(target, content)
            members.append(
                {"path": relative.as_posix(), "sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
            )
        bundle_digest = canonical_digest({"members": members})
        return {
            "manifest_path": str(root / "manifest.ophelia.yml"),
            "bundle_digest": bundle_digest,
        }

    def _execute_pending(self) -> None:
        for command in self.store.pending(limit=self.service.config.agent_command_batch):
            running = self.store.mark_running(command.command_id)
            try:
                result = self._execute(running)
            except Exception as exc:
                self.store.complete(
                    running.command_id,
                    succeeded=False,
                    result={
                        "schema_version": 1,
                        "kind": "ophelia.agent-command-result",
                        "command_id": running.command_id,
                        "status": "failed",
                        "error_code": "command_failed",
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                self.store.complete(running.command_id, succeeded=True, result=result)
                if result.get("result", {}).get("restart_required") is True:
                    self.service.request_restart()
                    break

    def _execute(self, command: AgentCommand) -> Dict[str, Any]:
        actor = self._actor(command.actor_id)
        payload = command.payload
        if command.operation == "host.drain":
            result = self.service.store.set_host_control(
                self.service.config.host_id,
                drained=_boolean_only(payload, "enabled"),
            )
        elif command.operation == "host.maintenance":
            result = self.service.store.set_host_control(
                self.service.config.host_id,
                maintenance_mode=_boolean_only(payload, "enabled"),
            )
        elif command.operation == "workload.run":
            if set(payload) != {"app", "environment", "workload"}:
                raise ValueError("Workload-run command fields are invalid.")
            result = self.service.accept_task(
                actor=actor,
                app=_text(payload.get("app"), "app"),
                environment=_text(payload.get("environment"), "environment"),
                workload_name=_text(payload.get("workload"), "workload"),
                idempotency_key="agent:" + command.command_id,
            )
        elif command.operation == "operation.cancel":
            if set(payload) != {"operation_id"}:
                raise ValueError("Operation-cancel command fields are invalid.")
            result = self.service.cancel_operation(
                _text(payload.get("operation_id"), "operation_id"), actor=actor
            )
        elif command.operation == "manifest.plan":
            if set(payload) != {"manifest_path", "bundle_digest"}:
                raise ValueError("Prepared manifest-plan command fields are invalid.")
            result = self.service.plan_agent_manifest(
                Path(payload["manifest_path"]),
                actor=actor,
                idempotency_key="agent:" + command.command_id,
            )
            result = dict(result)
            result.pop("confirmation_token", None)
        elif command.operation == "deploy.apply":
            if set(payload) != {"plan_id", "approval"}:
                raise ValueError("Prepared deploy command fields are invalid.")
            result = self.service.submit_approved_operation(
                payload["plan_id"],
                actor=actor,
                approval=approval_from_dict(payload["approval"]),
            )
        elif command.operation == "host.certificate.rotate":
            if payload:
                raise ValueError("Prepared certificate-rotation payload is invalid.")
            result = rotate_host_certificate(
                self.service.config,
                self.transport,
                command_id=command.command_id,
            )
        elif command.operation == "host.upgrade":
            result = stage_agent_upgrade(
                self.service.config,
                command_id=command.command_id,
                envelope_digest=command.envelope_digest,
                payload=payload,
            )
        else:
            raise ValueError("Agent command operation is not supported by this host.")
        return {
            "schema_version": 1,
            "kind": "ophelia.agent-command-result",
            "command_id": command.command_id,
            "status": "succeeded",
            "operation": command.operation,
            "result": result,
        }

    @staticmethod
    def _actor(actor_id: str) -> Actor:
        return Actor(
            actor_id=actor_id,
            source="lumen-control-plane",
            authenticated_by="mutual-tls-host-agent",
        )


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or path == Path(".") or ".." in path.parts:
        raise ValueError("Manifest-plan source path must be safe and relative.")
    if len(path.parts) > 32 or any(len(part) > 255 for part in path.parts):
        raise ValueError("Manifest-plan source path exceeds its bounds.")
    return path


def _write_or_verify(path: Path, value: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != value:
            raise ValueError("Existing manifest-plan inbox content does not match replay.")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _nonnegative_int(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("%s must be an integer of at least %d." % (field, minimum))
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 65536 or "\x00" in value:
        raise ValueError("%s must be bounded non-empty text." % field)
    return value


def _boolean_only(payload: Dict[str, Any], field: str) -> bool:
    if set(payload) != {field} or not isinstance(payload.get(field), bool):
        raise ValueError("Host-control command requires one boolean %s field." % field)
    return payload[field]
