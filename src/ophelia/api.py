from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .actions import ActionError, action_catalog, cancel_job, run_job
from .command_catalog import catalog as command_catalog
from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .lumen_adapter import (
    action_descriptors as lumen_action_descriptors,
    app_readiness as lumen_app_readiness,
    app_timeline as lumen_app_timeline,
    capabilities as lumen_capabilities,
    dashboard_data as lumen_dashboard_data,
)
from .operator_reports import host_inventory, manifest_registry, release_registry
from .operation_schema import error_envelope
from .operations import list_operations, run_operation
from .schema_export import manifest_json_schema
from .state_db import SQLITE_AVAILABLE, query_receipts, state_db_path, state_status, state_summary
from .workflows import list_workflow_templates, show_workflow


def serve(host: str = "127.0.0.1", port: int = 8765, runtime_root: Path = DEFAULT_RUNTIME_ROOT) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Ophelia API may only bind to 127.0.0.1/localhost by default.")

    class Handler(OpheliaHandler):
        runtime_root_value = runtime_root

    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


class OpheliaHandler(BaseHTTPRequestHandler):
    runtime_root_value = DEFAULT_RUNTIME_ROOT

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(
                {
                    "ok": True,
                    "service": "ophelia",
                    "api_bind": "local-only",
                    "runtime_root": str(self.runtime_root_value),
                    "ophelia_commit": _git_sha(),
                }
            )
            return
        if parsed.path == "/actions":
            try:
                self._json({"actions": action_catalog()})
            except ValueError as exc:
                self._error(str(exc), status=500, code="actions_config_invalid")
            return
        if parsed.path == "/commands":
            self._json({"commands": command_catalog()})
            return
        if parsed.path == "/schema/manifest":
            self._json(manifest_json_schema())
            return
        if parsed.path == "/host/inventory":
            self._json(host_inventory(self.runtime_root_value, REPO_ROOT))
            return
        if parsed.path == "/registry/manifests":
            self._json(manifest_registry(REPO_ROOT / "manifests", self.runtime_root_value))
            return
        if parsed.path == "/registry/releases":
            self._json(release_registry(self.runtime_root_value))
            return
        if parsed.path == "/operations":
            self._json(list_operations())
            return
        if parsed.path == "/workflows":
            self._json(list_workflow_templates())
            return
        if parsed.path.startswith("/workflows/"):
            workflow_id = parsed.path.split("/", 2)[2]
            # Missing graph -> HTTP 200 with a workflow_not_found blocker, never 500.
            self._json(show_workflow(workflow_id, self.runtime_root_value))
            return
        if parsed.path == "/state/status":
            self._json(state_status(self.runtime_root_value))
            return
        if parsed.path == "/state/summary":
            self._json(state_summary(self.runtime_root_value))
            return
        if parsed.path == "/state/apps":
            self._json(self._state_table("apps", "app"))
            return
        if parsed.path == "/state/receipts":
            self._json(query_receipts(self.runtime_root_value))
            return
        if parsed.path == "/state/routes":
            self._json(self._state_table("routes", "domain"))
            return
        if parsed.path == "/state/backups":
            self._json(self._state_table("backups", "backup_id"))
            return
        if parsed.path == "/lumen/capabilities":
            self._json(lumen_capabilities(self.runtime_root_value))
            return
        if parsed.path == "/lumen/dashboard-data":
            self._json(lumen_dashboard_data(self.runtime_root_value, REPO_ROOT / "manifests"))
            return
        if parsed.path == "/lumen/action-descriptors":
            self._json(lumen_action_descriptors())
            return
        if parsed.path == "/lumen/apps":
            self._json(self._lumen_apps())
            return
        if parsed.path.startswith("/lumen/apps/"):
            self._lumen_app_subresource(parsed)
            return
        if parsed.path.startswith("/jobs/") and parsed.path.endswith("/events"):
            job_id = parsed.path.split("/")[2]
            self._events(job_id)
            return
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.split("/")[2]
            path = self.runtime_root_value / "jobs" / f"{job_id}.json"
            if not path.exists():
                self._error("job not found", status=404, code="job_not_found")
                return
            try:
                self._json(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                self._error(f"job record is unreadable: {job_id}", status=409, code="job_unreadable")
            return
        self._error("not found", status=404, code="not_found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/jobs":
            try:
                body = self._read_json()
                result = run_job(
                    body.get("action_id"),
                    body.get("inputs") or {},
                    self.runtime_root_value,
                    requested_by=body.get("requested_by") or "api",
                    source=body.get("source") or "api",
                    idempotency_key=body.get("idempotency_key"),
                )
            except (ActionError, ValueError, TypeError) as exc:
                self._error(str(exc), status=400, code="invalid_job_request")
                return
            self._json(result.job, status=201)
            return
        if parsed.path.startswith("/jobs/") and parsed.path.endswith("/cancel"):
            job_id = parsed.path.split("/")[2]
            try:
                self._json(cancel_job(self.runtime_root_value, job_id))
            except ActionError as exc:
                self._error(str(exc), status=404, code="job_cancel_failed")
            return
        if parsed.path == "/operations/run":
            try:
                body = self._read_json()
                result = run_operation(
                    body.get("name"),
                    body.get("confirm"),
                    Path(body["manifest_dir"]) if body.get("manifest_dir") else REPO_ROOT / "manifests",
                    self.runtime_root_value,
                )
            except (TypeError, ValueError) as exc:
                self._error(str(exc), status=400, code="invalid_operation_request")
                return
            self._json(result)
            return
        self._error("not found", status=404, code="not_found")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object.")
        return body

    def _json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, message: str, status: int, code: str) -> None:
        self._json(error_envelope(message, code), status=status)

    def _events(self, job_id: str) -> None:
        path = self.runtime_root_value / "jobs" / f"{job_id}.events.ndjson"
        if not path.exists():
            self._error("events not found", status=404, code="events_not_found")
            return
        try:
            lines = path.read_text().splitlines()
        except OSError:
            self._error(f"job events are unreadable: {job_id}", status=409, code="job_events_unreadable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in lines:
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))

    def _state_table(self, table: str, order_by: str) -> dict:
        """Read one indexed table read-only.

        Returns ``available:false``/``needs_rebuild:true`` (HTTP 200) when the
        local index is missing or unavailable, never a 500. Never rebuilds on a
        GET. Stored ``payload_json`` is already redacted at index time.
        """
        db_path = state_db_path(self.runtime_root_value)
        base = {
            "schema_version": 1,
            "kind": "ophelia.state_query",
            "query": table,
            "available": SQLITE_AVAILABLE,
            "db_path": str(db_path),
            "needs_rebuild": True,
            table: [],
        }
        if not SQLITE_AVAILABLE:
            base["status"] = "unavailable"
            base["summary"] = "sqlite3 is unavailable; cannot query the local state index."
            return base
        if not db_path.exists():
            base["status"] = "missing"
            base["summary"] = "No local state index. Run `ship state rebuild` to create it."
            return base
        import sqlite3

        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(f"SELECT payload_json FROM {table} ORDER BY {order_by}")
            rows = [json.loads(row["payload_json"]) for row in cursor.fetchall() if row["payload_json"]]
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
            base["status"] = "error"
            base["summary"] = f"Local state index is unreadable: {exc}. Run `ship state rebuild`."
            return base
        finally:
            connection.close()
        base["needs_rebuild"] = False
        base["status"] = "ok"
        base[table] = rows
        base["summary"] = f"{len(rows)} {table} record(s) from the local state index."
        return base

    def _lumen_apps(self) -> dict:
        """List apps/environments from the manifest registry, framed for Lumen.

        Degrades gracefully: a missing manifests directory yields an empty
        ``apps`` list (HTTP 200), never a 500.
        """
        registry = manifest_registry(REPO_ROOT / "manifests", self.runtime_root_value)
        apps = [
            {
                "app": entry.get("app"),
                "environment": entry.get("environment"),
                "manifest_path": entry.get("manifest_path"),
                "domains": entry.get("domains", []),
            }
            for entry in registry.get("manifests", [])
            if isinstance(entry, dict)
        ]
        return {
            "schema_version": 1,
            "kind": "ophelia.lumen.apps",
            "apps": apps,
            "errors": registry.get("errors", []),
            "summary": f"{len(apps)} app/environment(s) from the manifest registry.",
        }

    def _lumen_app_subresource(self, parsed) -> None:
        """Handle ``/lumen/apps/<app>/readiness`` and ``/lumen/apps/<app>/timeline``.

        ``environment`` may be supplied as a query parameter. Unknown
        subresources return a 404 error envelope; data sub-reports degrade
        gracefully (the readiness/timeline reports surface their own blockers
        rather than raising), so there is no 500.
        """
        # Path shape: /lumen/apps/<app>/<subresource>
        remainder = parsed.path[len("/lumen/apps/"):]
        parts = [segment for segment in remainder.split("/") if segment]
        if len(parts) != 2:
            self._error("not found", status=404, code="not_found")
            return
        app = unquote(parts[0])
        subresource = parts[1]
        query = parse_qs(parsed.query)
        environment = query.get("environment", [None])[0]
        if subresource == "readiness":
            self._json(lumen_app_readiness(app, environment, self.runtime_root_value))
            return
        if subresource == "timeline":
            self._json(lumen_app_timeline(app, environment, self.runtime_root_value))
            return
        self._error("not found", status=404, code="not_found")

    def log_message(self, format, *args):  # noqa: A003
        return


def _git_sha() -> str | None:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None
