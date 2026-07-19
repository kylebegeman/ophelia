from __future__ import annotations

import os
import http.client
import json
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Set
from unittest import mock

from ophelia.cron import CronExpressionError, cron_matches, validate_cron_expression
from ophelia.daemon.config import DaemonConfig, DaemonConfigError, load_daemon_config
from ophelia.daemon.api import create_unix_server
from ophelia.daemon.service import OpheliaDaemon
from ophelia.daemon.store import DaemonStore
from ophelia.daemon.install import daemon_install_plan
from ophelia.daemon.systemd import notify_systemd
from ophelia.domain import Actor
from ophelia.execution import IdempotencyConflict, SQLiteOperationJournal
from ophelia.execution.subprocesses import ProcessResult


PINNED_IMAGE = "ghcr.io/example/daemon@sha256:" + "d" * 64


class FakeRunner:
    def __init__(self) -> None:
        self.commands: List[List[str]] = []
        self.running: Dict[str, Set[str]] = {}

    def run(self, argv: Sequence[str], **_: object) -> ProcessResult:
        command = list(argv)
        self.commands.append(command)
        project = _option(command, "-p")
        stdout = ""
        exit_code = 0
        reason = "success"
        if command[:3] == ["docker", "image", "inspect"]:
            stdout = PINNED_IMAGE + "\n"
        elif command[:2] == ["docker", "inspect"]:
            exit_code = 1
            reason = "nonzero_exit"
        elif "up" in command:
            arguments = command[command.index("up") + 1 :]
            services = [item for item in arguments if not item.startswith("-") and "=" not in item]
            self.running.setdefault(project, set()).update(services)
        elif "stop" in command:
            self.running.setdefault(project, set()).difference_update(command[command.index("stop") + 1 :])
        elif "down" in command:
            self.running.pop(project, None)
        elif "ps" in command and "--services" in command:
            stdout = "\n".join(sorted(self.running.get(project, set()))) + "\n"
        return ProcessResult(tuple(command), exit_code, reason, stdout, "", False, False, 1)


class CronTests(unittest.TestCase):
    def test_validates_ranges_steps_and_utc_matching(self) -> None:
        value = datetime(2026, 7, 19, 3, 17, tzinfo=timezone.utc)
        self.assertTrue(cron_matches("17 3 * * 0", value))
        self.assertTrue(cron_matches("*/17 3 1-31/2 * 0,7", value))
        self.assertFalse(cron_matches("18 3 * * *", value))
        with self.assertRaises(CronExpressionError):
            validate_cron_expression("61 * * * *")


class DaemonConfigTests(unittest.TestCase):
    def test_defaults_are_typed_and_explicit_overrides_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_daemon_config(
                root / "missing.toml",
                runtime_root=root / "runtime",
                socket_path=root / "opheliad.sock",
            )
        self.assertEqual((root / "runtime").resolve(), config.runtime_root)
        self.assertEqual((os.geteuid(),), config.allowed_uids)

    def test_unknown_keys_and_writable_config_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.toml"
            path.write_text("mystery = true\n")
            with self.assertRaisesRegex(DaemonConfigError, "Unknown"):
                load_daemon_config(path)
            path.chmod(0o666)
            with self.assertRaisesRegex(DaemonConfigError, "writable"):
                load_daemon_config(path)


class DaemonInstallTests(unittest.TestCase):
    def test_plan_distinguishes_complete_and_incomplete_versioned_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
            install_root = root / "install"
            release = install_root / "releases" / "0.5.0"
            release.mkdir(parents=True)
            config = root / "agent.toml"
            unit = root / "opheliad.service"
            with mock.patch(
                "ophelia.daemon.install.shutil.which", return_value="/usr/bin/tool"
            ):
                incomplete = daemon_install_plan(
                    source_root=source,
                    install_root=install_root,
                    config_path=config,
                    unit_path=unit,
                )
                executable = release / "bin" / "opheliad"
                executable.parent.mkdir()
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
                (release / "ophelia-install.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "ophelia.daemon-install",
                            "version": "0.5.0",
                            "source_digest": incomplete["observations"]["source_digest"],
                        }
                    )
                )
                complete = daemon_install_plan(
                    source_root=source,
                    install_root=install_root,
                    config_path=config,
                    unit_path=unit,
                )

        self.assertTrue(incomplete["observations"]["release_present"])
        self.assertFalse(incomplete["observations"]["release_valid"])
        self.assertTrue(complete["observations"]["release_valid"])


class SystemdNotificationTests(unittest.TestCase):
    def test_notify_uses_supplied_unix_datagram_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notify.sock"
            receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            receiver.bind(str(path))
            receiver.settimeout(2)
            try:
                with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": str(path)}):
                    self.assertTrue(notify_systemd("READY=1"))
                self.assertEqual(b"READY=1", receiver.recv(4096))
            finally:
                receiver.close()


class DaemonStoreTests(unittest.TestCase):
    def test_workload_runs_are_idempotent_fenced_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteOperationJournal.beneath_runtime_root(Path(directory))
            now = [1_774_111_200.0]
            store = DaemonStore(journal, clock=lambda: now[0])
            store.register_host(
                host_id="host_fixture-1",
                capabilities={"manifest_versions": [2]},
                agent_version="0.6.0",
                protocol_version=1,
            )
            actor = Actor("actor_fixture-1", "test", "test")
            arguments = dict(
                actor=actor,
                host_id="host_fixture-1",
                app="demo",
                environment="staging",
                revision_id="rev_fixture-1",
                revision_digest="sha256:" + "1" * 64,
                workload_name="cleanup",
                workload_kind="task",
                trigger_kind="task",
                idempotency_key="task-1",
                concurrency_policy="forbid",
            )
            first = store.accept_run(**arguments)
            retry = store.accept_run(**arguments)
            self.assertEqual(first.run_id, retry.run_id)
            with self.assertRaises(IdempotencyConflict):
                store.accept_run(**{**arguments, "app": "different"})

            claimed = store.acquire_run("worker-1")
            self.assertEqual(first.run_id, claimed.run_id)
            self.assertTrue(
                store.renew_run_lease(
                    claimed.run_id,
                    owner_id="worker-1",
                    fencing_token=claimed.fencing_token,
                )
            )
            now[0] += 301
            replacement = store.acquire_run("worker-2")
            self.assertEqual(claimed.fencing_token + 1, replacement.fencing_token)
            self.assertFalse(
                store.renew_run_lease(
                    claimed.run_id,
                    owner_id="worker-1",
                    fencing_token=claimed.fencing_token,
                )
            )
            terminal = store.complete_run(
                replacement.run_id,
                owner_id="worker-2",
                fencing_token=replacement.fencing_token,
                state="succeeded",
                result_digest="sha256:" + "2" * 64,
                exit_code=0,
                exit_reason="success",
            )
            events = journal.host_events_after(0)
            acknowledged = journal.acknowledge_host_events("lumen", events[-1]["cursor"])

            self.assertEqual("succeeded", terminal.state)
            self.assertEqual(4, len(events))
            self.assertEqual(events[-1]["cursor"], acknowledged)
            journal.integrity_check()


class DaemonServiceTests(unittest.TestCase):
    def test_plan_submit_recover_and_task_use_one_host_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            manifest = root / "app.ophelia.yml"
            manifest.write_text(
                f"""
version: 2
app: daemon-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  worker: {{kind: worker, artifact: app}}
  maintenance: {{kind: task, artifact: app, command: ["./maintenance"]}}
routes: []
update: {{strategy: recreate}}
"""
            )
            config = DaemonConfig(
                host_id="host_fixture-1",
                runtime_root=runtime,
                socket_path=root / "opheliad.sock",
                allowed_uids=(os.geteuid(),),
                allowed_manifest_roots=(root,),
                require_edge_runtime=False,
            )
            runner = FakeRunner()
            daemon = OpheliaDaemon(config, runner=runner)
            daemon.store.register_host(
                host_id=config.host_id,
                capabilities=daemon.capabilities(),
                agent_version="0.6.0",
                protocol_version=1,
            )
            actor = Actor("actor_unix-501", "test", "unix-peer-credentials")
            linked_manifest = root / "linked.ophelia.yml"
            linked_manifest.symlink_to(manifest)
            with self.assertRaises(PermissionError):
                daemon.plan(linked_manifest, actor=actor, idempotency_key="linked")
            plan = daemon.plan(manifest, actor=actor, idempotency_key="deploy-1")
            retry = daemon.plan(manifest, actor=actor, idempotency_key="deploy-1")
            self.assertEqual(plan, retry)
            manifest.write_text(manifest.read_text().replace("./maintenance", "./repair"))
            with self.assertRaises(IdempotencyConflict):
                daemon.plan(manifest, actor=actor, idempotency_key="deploy-1")
            manifest.write_text(manifest.read_text().replace("./repair", "./maintenance"))
            accepted = daemon.submit_operation(
                plan["plan_id"], plan["confirmation_token"], actor=actor
            )
            operation_id = accepted["operation"]["operation_id"]
            receipt = daemon.executor.run(operation_id, owner_id="test-worker")

            task = daemon.accept_task(
                actor=actor,
                app="daemon-demo",
                environment="staging",
                workload_name="maintenance",
                idempotency_key="task-1",
            )
            claimed = daemon.store.acquire_run("task-worker")
            terminal = daemon.workloads.execute(claimed, owner_id="task-worker")

            self.assertEqual("succeeded", receipt.outcome.value)
            self.assertEqual(task["run_id"], terminal.run_id)
            self.assertEqual("succeeded", terminal.state)
            self.assertTrue(any("run" in command and "maintenance" in command for command in runner.commands))

    def test_unix_api_derives_peer_identity_and_enforces_protocol_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = DaemonConfig(
                host_id="host_fixture-1",
                runtime_root=root / "runtime",
                socket_path=root / "opheliad.sock",
                allowed_uids=(os.geteuid(),),
                allowed_manifest_roots=(root,),
                require_edge_runtime=False,
            )
            daemon = OpheliaDaemon(config, runner=FakeRunner())
            daemon.store.register_host(
                host_id=config.host_id,
                capabilities=daemon.capabilities(),
                agent_version="0.6.0",
                protocol_version=1,
            )
            server = create_unix_server(daemon)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = UnixHTTPConnection(config.socket_path)
                connection.request(
                    "GET",
                    "/v1/capabilities",
                    headers={
                        "X-Request-ID": "request-api-1",
                        "X-Ophelia-Protocol-Version": "1",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()

                self.assertEqual(200, response.status)
                self.assertTrue(payload["ok"])
                self.assertEqual(1, payload["protocol_version"])

                connection = UnixHTTPConnection(config.socket_path)
                connection.request(
                    "POST",
                    "/v1/host/drain",
                    body=json.dumps({"enabled": True}),
                    headers={
                        "Content-Type": "application/json",
                        "X-Request-ID": "request-api-2",
                        "X-Ophelia-Protocol-Version": "1",
                        "Idempotency-Key": "drain-1",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()

                self.assertEqual(200, response.status)
                self.assertTrue(payload["data"]["drained"])

                manifest = root / "app.ophelia.yml"
                manifest.write_text(
                    f"""
version: 2
app: api-demo
environment: staging
artifacts: {{app: {{image: "{PINNED_IMAGE}"}}}}
workloads:
  worker: {{kind: worker, artifact: app}}
routes: []
update: {{strategy: recreate}}
"""
                )
                headers = {
                    "Content-Type": "application/json",
                    "X-Request-ID": "request-api-plan-1",
                    "X-Ophelia-Protocol-Version": "1",
                    "Idempotency-Key": "plan-api-1",
                }
                connection = UnixHTTPConnection(config.socket_path)
                connection.request(
                    "POST",
                    "/v1/plans",
                    body=json.dumps({"manifest_path": str(manifest)}),
                    headers=headers,
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(201, response.status)

                manifest.write_text(manifest.read_text().replace("worker:", "processor:"))
                headers["X-Request-ID"] = "request-api-plan-2"
                connection = UnixHTTPConnection(config.socket_path)
                connection.request(
                    "POST",
                    "/v1/plans",
                    body=json.dumps({"manifest_path": str(manifest)}),
                    headers=headers,
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(409, response.status)
                self.assertEqual("idempotency_conflict", payload["data"]["error"]["code"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.socket_path))


def _option(command: Sequence[str], name: str) -> str:
    try:
        return command[command.index(name) + 1]
    except (ValueError, IndexError):
        return ""


if __name__ == "__main__":
    unittest.main()
