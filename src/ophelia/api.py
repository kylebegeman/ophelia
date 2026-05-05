from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .actions import action_catalog, cancel_job, run_job
from .config import DEFAULT_RUNTIME_ROOT


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
        if parsed.path == "/actions":
            self._json({"actions": action_catalog()})
            return
        if parsed.path.startswith("/jobs/") and parsed.path.endswith("/events"):
            job_id = parsed.path.split("/")[2]
            self._events(job_id)
            return
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.split("/")[2]
            path = self.runtime_root_value / "jobs" / f"{job_id}.json"
            if not path.exists():
                self._json({"error": "job not found"}, status=404)
                return
            self._json(json.loads(path.read_text()))
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/jobs":
            body = self._read_json()
            result = run_job(
                body.get("action_id"),
                body.get("inputs") or {},
                self.runtime_root_value,
                requested_by=body.get("requested_by") or "api",
                source=body.get("source") or "api",
                idempotency_key=body.get("idempotency_key"),
            )
            self._json(result.job, status=201)
            return
        if parsed.path.startswith("/jobs/") and parsed.path.endswith("/cancel"):
            job_id = parsed.path.split("/")[2]
            self._json(cancel_job(self.runtime_root_value, job_id))
            return
        self._json({"error": "not found"}, status=404)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _events(self, job_id: str) -> None:
        path = self.runtime_root_value / "jobs" / f"{job_id}.events.ndjson"
        if not path.exists():
            self._json({"error": "events not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in path.read_text().splitlines():
            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A003
        return
