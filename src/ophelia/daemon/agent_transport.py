"""Outbound mutually authenticated control-plane transport."""

from __future__ import annotations

import http.client
import json
import ssl
import uuid
from pathlib import Path
from typing import Any, Dict, Protocol
from urllib.parse import quote, urlparse

from .config import DaemonConfig, PROTOCOL_VERSION


class AgentTransportError(RuntimeError):
    pass


class AgentAuthenticationError(AgentTransportError):
    pass


class AgentTransport(Protocol):
    def exchange(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...

    def rotate_identity(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...


class HTTPSAgentTransport:
    def __init__(self, config: DaemonConfig, *, timeout_seconds: float = 35.0) -> None:
        if not config.agent_enabled or config.control_plane_url is None:
            raise ValueError("Outbound agent transport is not configured.")
        if any(
            path is None
            for path in (
                config.control_plane_ca_path,
                config.host_certificate_path,
                config.host_private_key_path,
            )
        ):
            raise ValueError("Outbound agent TLS identity is incomplete.")
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.parsed = urlparse(config.control_plane_url)
        # The Lumen CA authenticates host client certificates. The public edge
        # may use an ordinary publicly trusted server certificate, so preserve
        # system roots and add the Lumen CA instead of replacing system trust.
        self.context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        self.context.load_verify_locations(cafile=str(config.control_plane_ca_path))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(
            certfile=str(config.host_certificate_path),
            keyfile=str(config.host_private_key_path),
        )

    def exchange(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = self.parsed.path.rstrip("/")
        path = "%s/api/ophelia/v1/hosts/%s/exchange" % (
            base,
            quote(self.config.host_id, safe=""),
        )
        return self._post(path, payload)

    def rotate_identity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        base = self.parsed.path.rstrip("/")
        path = "%s/api/ophelia/v1/hosts/%s/certificate/rotate" % (
            base,
            quote(self.config.host_id, safe=""),
        )
        return self._post(path, payload)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > self.config.agent_exchange_bytes:
            raise AgentTransportError("Outbound agent exchange exceeds its size limit.")
        connection = http.client.HTTPSConnection(
            self.parsed.hostname,
            self.parsed.port,
            context=self.context,
            timeout=self.timeout_seconds,
        )
        request_id = "request_agent-" + uuid.uuid4().hex
        try:
            connection.request(
                "POST",
                path,
                body=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                    "X-Request-ID": request_id,
                    "X-Ophelia-Protocol-Version": str(PROTOCOL_VERSION),
                },
            )
            response = connection.getresponse()
            body = response.read(self.config.agent_exchange_bytes + 1)
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise AgentTransportError("Outbound control-plane exchange failed.") from exc
        finally:
            connection.close()
        if len(body) > self.config.agent_exchange_bytes:
            raise AgentTransportError("Control-plane response exceeds its size limit.")
        if response.status in {401, 403}:
            raise AgentAuthenticationError(
                "Control-plane authentication rejected this host identity."
            )
        if response.status != 200:
            raise AgentTransportError(
                "Control-plane exchange returned HTTP %d." % response.status
            )
        if response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
            raise AgentTransportError("Control-plane response is not JSON.")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise AgentTransportError("Control-plane response JSON is invalid.") from exc
        if not isinstance(document, dict):
            raise AgentTransportError("Control-plane response must be an object.")
        return document
