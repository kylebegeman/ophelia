from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import socket
import sys
import tempfile
import time
import unittest
import urllib.request
from copy import deepcopy
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.product_bundle import (
    ProductBundleError,
    load_product_operations_bundle,
    verify_product_artifact,
)
from ophelia.execution import SQLiteOperationJournal
from ophelia.product_execution import (
    ProductExecutionError,
    apply_product_release,
    product_release_plan,
)
from ophelia.execution.process_backend import reap_product_children
from ophelia.product_recovery import (
    apply_product_restore_drill,
    create_product_backup,
    product_backup_plan,
    product_restore_drill_plan,
)


SHARED_LINKLET_BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "forge"
    / "examples"
    / "linklet"
    / ".product"
    / "operations"
)


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _forge_digest(value, field: str) -> str:
    copied = deepcopy(value)
    copied[field] = ""
    raw = json.dumps(copied, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    return _sha(raw)


def _write(path: Path, value) -> str:
    raw = json.dumps(value, indent=2, ensure_ascii=False).encode() + b"\n"
    path.write_bytes(raw)
    return _sha(raw)


def _free_port() -> int:
    with socket.socket() as value:
        value.bind(("127.0.0.1", 0))
        return value.getsockname()[1]


def _host_arch() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )


def _fixture(
    root: Path,
    release: str,
    public_port: int,
    *,
    required_secret: bool = False,
) -> tuple[Path, Path]:
    bundle_root = root / release / "operations"
    bundle_root.mkdir(parents=True)
    artifact = root / release / "server"
    artifact.write_text(
        """#!/usr/bin/env python3
import argparse, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p=argparse.ArgumentParser(); p.add_argument('-addr'); p.add_argument('-db'); a=p.parse_args()
host, port=a.addr.rsplit(':', 1)
db=sqlite3.connect(a.db); db.execute('create table if not exists fixture (value text)'); db.commit(); db.close()
class H(BaseHTTPRequestHandler):
  def do_GET(self):
    if self.path == '/healthz': body=b'ok'
    elif self.path == '/version': body=b'RELEASE_VALUE'
    else: self.send_response(404); self.end_headers(); return
    self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
  def log_message(self, *args): pass
ThreadingHTTPServer((host, int(port)), H).serve_forever()
""".replace("RELEASE_VALUE", release),
        encoding="utf-8",
    )
    artifact.chmod(0o700)
    artifact_digest = _sha(artifact.read_bytes())
    zero = "sha256:" + "0" * 64
    stack = {
        "id": "fixture-stack",
        "version": "1.0.0",
        "manifest_digest": _sha(b"stack"),
        "blueprint_digest": _sha(b"blueprint"),
    }
    dataset = {
        "id": "product-primary",
        "owner": "stack",
        "kind": "sqlite",
        "path": "product.db",
        "binding": "stack-owned",
        "consistency": "physical",
        "consistency_group": "product-primary",
        "backup": "required",
        "quiescence": "application-stop",
        "backup_order": 10,
        "restore_validations": ["sqlite-integrity", "application-start", "health-probe"],
    }
    runtime = {
        "schema_version": "product.runtime-requirements/v1",
        "kind": "product.runtime-requirements",
        "contract_digest": zero,
        "product_id": "fixture-product",
        "stack": stack,
        "composition_digest": _sha(b"composition"),
        "processes": [{
            "id": "web", "artifact_id": "server",
            "argv": ["./server", "-addr", f"0.0.0.0:{public_port}", "-db", "product.db"],
            "replicas": 1, "shutdown_seconds": 2,
        }],
        "ports": [{
            "id": "http", "process_id": "web", "protocol": "http",
            "port": public_port, "exposure": "loopback",
        }],
        "health": [{
            "id": "http-live", "process_id": "web", "kind": "liveness",
            "path": "/healthz", "port_id": "http", "interval_seconds": 2,
            "timeout_seconds": 1,
        }],
        "data": [dataset],
        "lifecycle": {
            "startup": "single-process", "shutdown_signal": "SIGTERM",
            "shutdown_timeout_seconds": 2,
        },
    }
    if required_secret:
        runtime["environment"] = [{
            "name": "APP_SECRET",
            "owner": "fixture-product",
            "purpose": "Exercise host-owned secret delivery.",
            "required": False,
            "secret": True,
        }]
    runtime["contract_digest"] = _forge_digest(runtime, "contract_digest")
    release_manifest = {
        "schema_version": "product.release-manifest/v1",
        "kind": "product.release-manifest",
        "manifest_digest": zero,
        "release_id": release,
        "product_id": "fixture-product",
        "stack": stack,
        "provenance": {
            "plan_digest": _sha(b"composition"),
            "product_spec_digest": _sha(b"spec"),
            "registry_digest": _sha(b"registry"),
        },
        "artifacts": [{
            "id": "server", "kind": "executable", "path": "bin/server",
            "digest": artifact_digest, "size_bytes": artifact.stat().st_size,
            "build_digest": _sha(("build-" + release).encode()),
            "platform": {
                "os": platform.system().lower(), "arch": _host_arch(),
                "cgo_enabled": False, "toolchain": "python-fixture",
            },
        }],
        "capabilities": [],
        "compatibility": {
            "runtime_requirements": "product.runtime-requirements/v1",
            "release_manifest": "product.release-manifest/v1",
            "recovery_contract": "product.recovery-contract/v1",
        },
        "rollout": {
            "strategy": "replace", "migration_behavior": "forward-only-at-startup",
            "rollback": "artifact-only", "preconditions": ["health-probe"],
        },
    }
    if required_secret:
        release_manifest["configuration"] = [{
            "name": "APP_SECRET",
            "required": True,
            "secret": True,
        }]
    release_manifest["manifest_digest"] = _forge_digest(release_manifest, "manifest_digest")
    recovery = {
        "schema_version": "product.recovery-contract/v1",
        "kind": "product.recovery-contract",
        "contract_digest": zero,
        "product_id": "fixture-product",
        "composition_digest": _sha(b"composition"),
        "objectives": {"rpo_hours": 24, "rto_hours": 4, "retention_days": 30},
        "datasets": [dataset],
        "backup_order": ["product-primary"],
        "restore_order": ["product-primary"],
        "validation_order": [{
            "dataset_id": "product-primary",
            "checks": ["sqlite-integrity", "application-start", "health-probe"],
        }],
    }
    recovery["contract_digest"] = _forge_digest(recovery, "contract_digest")
    documents = []
    for kind, name, value in (
        ("recovery-contract", "recovery-contract.json", recovery),
        ("release-manifest", "release-manifest.json", release_manifest),
        ("runtime-requirements", "runtime-requirements.json", runtime),
    ):
        documents.append({"kind": kind, "path": name, "digest": _write(bundle_root / name, value)})
    bundle = {
        "schema_version": "product.operations-bundle/v1",
        "kind": "product.operations-bundle",
        "bundle_digest": zero,
        "product_id": "fixture-product",
        "release_id": release,
        "composition_digest": _sha(b"composition"),
        "documents": documents,
    }
    bundle["bundle_digest"] = _forge_digest(bundle, "bundle_digest")
    _write(bundle_root / "bundle.json", bundle)
    return bundle_root, artifact


class ProductOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.port = _free_port()

    def tearDown(self) -> None:
        for path in self.runtime.glob("apps/*/environments/*/revisions/*/ophelia-process.json"):
            try:
                pid = int(json.loads(path.read_text())["pid"])
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        for path in self.runtime.glob("apps/*/environments/*/traffic/proxy.pid"):
            try:
                os.kill(int(path.read_text().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        reap_product_children()
        self.temporary.cleanup()

    @unittest.skipUnless(
        SHARED_LINKLET_BUNDLE.is_dir(),
        "optional sibling Forge checkout is unavailable",
    )
    def test_shared_linklet_fixture_validates_without_forge_import(self) -> None:
        bundle = load_product_operations_bundle(SHARED_LINKLET_BUNDLE)
        self.assertEqual("linklet-reference", bundle.product_id)
        self.assertEqual(
            "sha256:64146640565c3b8437d2e89869c035b8e8ba1be5594cd05f8871767481b6d15b",
            bundle.bundle_digest,
        )

    def test_artifact_bytes_and_platform_fail_closed(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-v1", self.port)
        bundle = load_product_operations_bundle(bundle_root)
        verified = verify_product_artifact(bundle, "server", artifact)
        self.assertTrue(verified.compatible)
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ProductBundleError):
            verify_product_artifact(bundle, "server", artifact)

    def test_required_secret_is_planned_but_never_persisted(self) -> None:
        bundle_root, artifact = _fixture(
            self.root, "fixture-secret", self.port, required_secret=True
        )
        blocked = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        self.assertFalse(blocked["can_apply"])
        self.assertIn(
            "product_configuration_missing",
            {item["code"] for item in blocked["blockers"]},
        )

        canary = "canary-secret-that-must-not-be-persisted"
        plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={"APP_SECRET": canary},
        )
        receipt = apply_product_release(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={"APP_SECRET": canary},
            confirm=plan["confirmation_token"],
        )
        self.assertEqual("succeeded", receipt["status"])

        backup_without_secret = product_backup_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="secret-backup",
            dataset_bindings={},
            environment_values={},
        )
        self.assertFalse(backup_without_secret["can_apply"])
        for path in self.runtime.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(canary.encode(), path.read_bytes(), path)

    def test_post_drain_commit_failure_restarts_exact_predecessor(self) -> None:
        first_root, first_artifact = _fixture(self.root, "fixture-v1", self.port)
        second_root, second_artifact = _fixture(self.root, "fixture-v2", self.port)
        first_plan = product_release_plan(
            first_root, first_artifact, runtime_root=self.runtime, environment_values={}
        )
        first = apply_product_release(
            first_root,
            first_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=first_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", first["status"])
        second_plan = product_release_plan(
            second_root, second_artifact, runtime_root=self.runtime, environment_values={}
        )
        with mock.patch.object(
            SQLiteOperationJournal,
            "commit_success",
            side_effect=RuntimeError("injected commit failure"),
        ):
            failed = apply_product_release(
                second_root,
                second_artifact,
                runtime_root=self.runtime,
                environment_values={},
                confirm=second_plan["confirmation_token"],
            )
        self.assertEqual("failed_compensated", failed["status"])
        self.assertEqual(
            b"fixture-v1",
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read(),
        )

    def test_active_pointer_must_reconcile_with_journal(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-v1", self.port)
        plan = product_release_plan(
            bundle_root, artifact, runtime_root=self.runtime, environment_values={}
        )
        apply_product_release(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=plan["confirmation_token"],
        )
        active_path = next(self.runtime.glob("apps/*/environments/*/traffic/active.json"))
        active = json.loads(active_path.read_text())
        active["revision_digest"] = "sha256:" + "0" * 64
        active_path.write_text(json.dumps(active), encoding="utf-8")
        with self.assertRaises(ProductExecutionError):
            product_release_plan(
                bundle_root,
                artifact,
                runtime_root=self.runtime,
                environment_values={},
            )

    def test_deploy_upgrade_and_rollback_use_health_and_atomic_proxy(self) -> None:
        first_root, first_artifact = _fixture(self.root, "fixture-v1", self.port)
        second_root, second_artifact = _fixture(self.root, "fixture-v2", self.port)

        first_plan = product_release_plan(
            first_root, first_artifact, runtime_root=self.runtime, environment_values={}
        )
        first = apply_product_release(
            first_root,
            first_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=first_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(b"fixture-v1", urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read())

        backup_plan = product_backup_plan(
            first_root,
            runtime_root=self.runtime,
            backup_id="backup-v1",
            dataset_bindings={},
        )
        backup = create_product_backup(
            first_root,
            runtime_root=self.runtime,
            backup_id="backup-v1",
            dataset_bindings={},
            environment_values={},
            confirm=backup_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", backup["status"])
        self.assertEqual("backup.apply", backup["operation"])
        self.assertEqual(b"fixture-v1", urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read())
        correlated_backup = (
            self.runtime
            / "receipts"
            / "product"
            / f"{backup['ophelia_receipt']['receipt_id']}.json"
        )
        correlated_backup.unlink()
        replayed_backup = create_product_backup(
            first_root,
            runtime_root=self.runtime,
            backup_id="backup-v1",
            dataset_bindings={},
            environment_values={},
            confirm=backup_plan["confirmation_token"],
        )
        self.assertEqual(backup["ophelia_receipt_digest"], replayed_backup["ophelia_receipt_digest"])
        self.assertTrue(correlated_backup.is_file())

        drill_plan = product_restore_drill_plan(
            first_root,
            runtime_root=self.runtime,
            backup_id="backup-v1",
            drill_id="drill-v1",
        )
        drill = apply_product_restore_drill(
            first_root,
            runtime_root=self.runtime,
            backup_id="backup-v1",
            drill_id="drill-v1",
            environment_values={},
            confirm=drill_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", drill["status"])
        self.assertEqual("restore-drill.apply", drill["operation"])
        self.assertTrue(Path(drill["restore_path"]).joinpath("restore-report.json").is_file())
        self.assertEqual(b"fixture-v1", urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read())

        second_plan = product_release_plan(
            second_root, second_artifact, runtime_root=self.runtime, environment_values={}
        )
        second = apply_product_release(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=second_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", second["status"])
        self.assertEqual(b"fixture-v2", urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read())

        rollback_plan = product_release_plan(
            first_root,
            first_artifact,
            runtime_root=self.runtime,
            environment_values={},
            operation="rollback.apply",
        )
        rollback = apply_product_release(
            first_root,
            first_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=rollback_plan["confirmation_token"],
            operation="rollback.apply",
        )
        self.assertEqual("succeeded", rollback["status"])
        self.assertEqual(b"fixture-v1", urllib.request.urlopen(f"http://127.0.0.1:{self.port}/version").read())
        self.assertEqual(first["bundle_digest"], rollback["bundle_digest"])
        self.assertTrue(rollback["inputs_redacted"])


if __name__ == "__main__":
    unittest.main()
