from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from copy import deepcopy
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.product_bundle import (
    ProductBundleError,
    load_product_operations_bundle,
    verify_product_artifact,
)
from ophelia.execution import OperationStoreError, SQLiteOperationJournal
from ophelia.commands import product as product_commands
from ophelia.product_execution import (
    ProductExecutionError,
    apply_product_release,
    product_release_plan,
)
from ophelia.execution.process_backend import reap_product_children
from ophelia.product_recovery import (
    ProductRecoveryError,
    _backup_postgresql,
    _isolated_postgres_target,
    _path_digest,
    _restore_postgresql,
    apply_product_restore_drill,
    create_product_backup,
    product_backup_plan,
    product_restore_drill_plan,
)


SHARED_FORGE_ROOT = Path(__file__).resolve().parents[2] / "forge" / "examples"
FORGE_COMPATIBILITY_LOCK = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "compatibility" / "forge-products.json").read_text()
)
SHARED_FORGE_PROFILES = FORGE_COMPATIBILITY_LOCK["profiles"]


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
    replicas: int = 1,
    release_preconditions: tuple[str, ...] = ("health-probe",),
    with_migration: bool = False,
    provider_dataset: bool = False,
    rollback_policy: str = "artifact-only",
) -> tuple[Path, Path]:
    bundle_root = root / release / "operations"
    bundle_root.mkdir(parents=True)
    artifact = root / release / "server"
    artifact.write_text(
        """#!/usr/bin/env python3
import argparse, os, sqlite3
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p=argparse.ArgumentParser(); p.add_argument('-addr'); p.add_argument('-db'); a=p.parse_args()
host, port=a.addr.rsplit(':', 1)
db=sqlite3.connect(a.db); db.execute('pragma journal_mode=WAL'); db.execute('create table if not exists fixture (value text)'); db.commit(); db.close()
class H(BaseHTTPRequestHandler):
  def do_GET(self):
    if self.path == '/healthz': body=b'ok'
    elif self.path == '/version': body=b'RELEASE_VALUE'
    elif self.path == '/instance': body=str(os.getpid()).encode()
    elif self.path == '/object':
      root=os.environ.get('OBJECT_STORAGE_LOCAL_ROOT', '')
      probe=Path(root) / 'probe.txt'
      body=probe.read_bytes() if probe.is_file() else b'missing'
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
            "replicas": replicas, "shutdown_seconds": 2,
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
    provider = {
        "id": "storage.object.private",
        "owner": "storage.object",
        "kind": "object-storage",
        "binding": "provider-selected",
        "consistency": "physical",
        "consistency_group": "storage.object.private",
        "backup": "required",
        "quiescence": "provider-snapshot",
        "backup_order": 20,
        "restore_validations": ["provider-restore", "application-validation"],
    }
    if provider_dataset:
        runtime["data"].append(provider)
        runtime["environment"] = [
            {
                "name": "OBJECT_STORAGE_PROVIDER", "owner": "storage.object",
                "purpose": "Select the object provider.", "required": False, "secret": False,
            },
            {
                "name": "OBJECT_STORAGE_LOCAL_ROOT", "owner": "storage.object",
                "purpose": "Bind local restored objects.", "required": False, "secret": False,
            },
        ]
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
            "rollback": rollback_policy, "preconditions": list(release_preconditions),
        },
    }
    if with_migration:
        release_manifest["migrations"] = [{
            "id": "fixture.startup.v1",
            "capability_id": "fixture.startup",
            "digest": _sha(b"fixture-startup-v1"),
        }]
    if required_secret:
        release_manifest["configuration"] = [{
            "name": "APP_SECRET",
            "required": True,
            "secret": True,
        }]
    if provider_dataset:
        release_manifest.setdefault("configuration", []).extend([
            {"name": "OBJECT_STORAGE_LOCAL_ROOT", "required": False, "secret": False},
            {"name": "OBJECT_STORAGE_PROVIDER", "required": False, "secret": False},
        ])
    release_manifest["manifest_digest"] = _forge_digest(release_manifest, "manifest_digest")
    recovery = {
        "schema_version": "product.recovery-contract/v1",
        "kind": "product.recovery-contract",
        "contract_digest": zero,
        "product_id": "fixture-product",
        "composition_digest": _sha(b"composition"),
        "objectives": {"rpo_hours": 24, "rto_hours": 4, "retention_days": 30},
        "datasets": [dataset] + ([provider] if provider_dataset else []),
        "backup_order": ["product-primary"] + (["storage.object.private"] if provider_dataset else []),
        "restore_order": (["storage.object.private"] if provider_dataset else []) + ["product-primary"],
        "validation_order": [{
            "dataset_id": "product-primary",
            "checks": ["sqlite-integrity", "application-start", "health-probe"],
        }] + ([{
            "dataset_id": "storage.object.private",
            "checks": ["provider-restore", "application-validation"],
        }] if provider_dataset else []),
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
                record = json.loads(path.read_text())
                replicas = record.get("replicas", [record])
                for replica in replicas:
                    os.kill(int(replica["pid"]), signal.SIGKILL)
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
        SHARED_FORGE_ROOT.is_dir(),
        "optional sibling Forge checkout is unavailable",
    )
    def test_shared_forge_profiles_validate_without_forge_import(self) -> None:
        self.assertEqual(
            {
                "product.runtime-requirements/v1",
                "product.release-manifest/v1",
                "product.recovery-contract/v1",
                "product.operations-bundle/v1",
            },
            set(FORGE_COMPATIBILITY_LOCK["contracts"]),
        )
        self.assertEqual(
            {"linklet", "linklet-postgres", "linklet-react"},
            set(SHARED_FORGE_PROFILES),
        )
        forge_root = SHARED_FORGE_ROOT.parent
        actual_commit = subprocess.check_output(
            ["git", "-C", str(forge_root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        reviewed_commit = FORGE_COMPATIBILITY_LOCK["producer"]["reviewed_commit"]
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(forge_root),
                "merge-base",
                "--is-ancestor",
                reviewed_commit,
                actual_commit,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(
            0,
            ancestry.returncode,
            "Forge checkout must descend from the explicitly reviewed compatibility commit",
        )
        for profile, expected in SHARED_FORGE_PROFILES.items():
            with self.subTest(profile=profile):
                bundle = load_product_operations_bundle(
                    SHARED_FORGE_ROOT / profile / ".product" / "operations"
                )
                self.assertEqual(expected["product_id"], bundle.product_id)
                self.assertEqual(expected["release_id"], bundle.release_id)
                self.assertEqual(expected["stack_id"], bundle.runtime["stack"]["id"])
                self.assertEqual(
                    set(expected["facets"]),
                    {item["id"] for item in bundle.runtime["stack"]["facets"]},
                )
                self.assertEqual(
                    expected["replicas"],
                    bundle.runtime["processes"][0]["replicas"],
                )
                self.assertEqual(
                    set(expected["recoverable_provider_datasets"]),
                    {
                        item["id"]
                        for item in bundle.recovery["datasets"]
                        if item["binding"] == "provider-selected"
                        and item["backup"] != "excluded"
                    },
                )
                self.assertEqual(
                    expected["composition_digest"], bundle.composition_digest
                )
                self.assertEqual(expected["bundle_digest"], bundle.bundle_digest)
                artifact_declaration = bundle.artifact(
                    str(bundle.runtime["processes"][0]["artifact_id"])
                )
                artifact_path = (
                    SHARED_FORGE_ROOT
                    / profile
                    / str(artifact_declaration["path"])
                )
                if not artifact_path.is_file():
                    continue
                verified = verify_product_artifact(
                    bundle,
                    str(artifact_declaration["id"]),
                    artifact_path,
                    require_compatible=False,
                )
                self.assertEqual(artifact_declaration["digest"], verified.digest)
                if verified.compatible:
                    environment_values = {
                        str(item["name"]): "reviewed-fixture-value"
                        for item in bundle.release.get("configuration", [])
                        if item["required"]
                    }
                    plan = product_release_plan(
                        SHARED_FORGE_ROOT
                        / profile
                        / ".product"
                        / "operations",
                        artifact_path,
                        runtime_root=self.runtime / profile,
                        environment_values=environment_values,
                    )
                    self.assertTrue(plan["can_apply"], plan["blockers"])

    def test_product_cli_wraps_kernel_failures_in_json(self) -> None:
        args = Namespace(
            bundle=self.root / "operations",
            artifact=self.root / "server",
            runtime_root=self.runtime,
            env_file=None,
            host_id=None,
            product_operation="deploy.apply",
            evidence=[],
            json=True,
        )
        output = io.StringIO()
        with mock.patch.object(
            product_commands,
            "product_release_plan",
            side_effect=OperationStoreError("operation journal is unavailable"),
        ), contextlib.redirect_stdout(output):
            result = product_commands.run_release_plan(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(1, result)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            "product_operation_failed", payload["blockers"][0]["code"]
        )
        self.assertNotIn("Traceback", output.getvalue())

    def test_multi_facet_process_must_bind_a_known_facet(self) -> None:
        bundle_root, _ = _fixture(self.root, "fixture-facets", self.port)
        runtime_path = bundle_root / "runtime-requirements.json"
        runtime = json.loads(runtime_path.read_text())
        runtime["stack"]["facets"] = [
            {"id": "server", "kind": "server", "root": "server", "toolchain": "go"},
            {"id": "web", "kind": "web", "root": "web", "toolchain": "node"},
        ]
        runtime["contract_digest"] = _forge_digest(runtime, "contract_digest")
        runtime_digest = _write(runtime_path, runtime)
        release_path = bundle_root / "release-manifest.json"
        release = json.loads(release_path.read_text())
        release["stack"] = runtime["stack"]
        release["manifest_digest"] = _forge_digest(release, "manifest_digest")
        release_digest = _write(release_path, release)
        bundle_path = bundle_root / "bundle.json"
        bundle = json.loads(bundle_path.read_text())
        next(
            item for item in bundle["documents"] if item["kind"] == "runtime-requirements"
        )["digest"] = runtime_digest
        next(
            item for item in bundle["documents"] if item["kind"] == "release-manifest"
        )["digest"] = release_digest
        bundle["bundle_digest"] = _forge_digest(bundle, "bundle_digest")
        _write(bundle_path, bundle)

        with self.assertRaisesRegex(ProductBundleError, "must declare a facet"):
            load_product_operations_bundle(bundle_root)

        runtime["processes"][0]["facet"] = "server"
        runtime["contract_digest"] = _forge_digest(runtime, "contract_digest")
        runtime_digest = _write(runtime_path, runtime)
        next(
            item for item in bundle["documents"] if item["kind"] == "runtime-requirements"
        )["digest"] = runtime_digest
        bundle["bundle_digest"] = _forge_digest(bundle, "bundle_digest")
        _write(bundle_path, bundle)
        load_product_operations_bundle(bundle_root)

    def test_bundle_paths_and_recovery_semantics_match_forge_canonical_rules(self) -> None:
        unsafe_root, _ = _fixture(
            self.root, "fixture-unsafe-contract-path", self.port
        )
        bundle_path = unsafe_root / "bundle.json"
        bundle = json.loads(bundle_path.read_text())
        bundle["documents"][0]["path"] = "nested//recovery-contract.json"
        _write(bundle_path, bundle)
        with self.assertRaisesRegex(ProductBundleError, "unsafe"):
            load_product_operations_bundle(unsafe_root)

        mismatch_root, _ = _fixture(
            self.root, "fixture-recovery-mismatch", self.port
        )
        runtime_path = mismatch_root / "runtime-requirements.json"
        runtime = json.loads(runtime_path.read_text())
        runtime["data"][0]["quiescence"] = "none"
        runtime["contract_digest"] = _forge_digest(runtime, "contract_digest")
        runtime_digest = _write(runtime_path, runtime)
        bundle_path = mismatch_root / "bundle.json"
        bundle = json.loads(bundle_path.read_text())
        next(
            item
            for item in bundle["documents"]
            if item["kind"] == "runtime-requirements"
        )["digest"] = runtime_digest
        bundle["bundle_digest"] = _forge_digest(bundle, "bundle_digest")
        _write(bundle_path, bundle)
        with self.assertRaisesRegex(
            ProductBundleError, "kinds or quiescence policies differ"
        ):
            load_product_operations_bundle(mismatch_root)

    def test_artifact_bytes_and_platform_fail_closed(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-v1", self.port)
        bundle = load_product_operations_bundle(bundle_root)
        verified = verify_product_artifact(bundle, "server", artifact)
        self.assertTrue(verified.compatible)
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ProductBundleError):
            verify_product_artifact(bundle, "server", artifact)

    def test_bundle_contract_and_artifact_symlinks_fail_closed(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-symlink", self.port)
        bundle = load_product_operations_bundle(bundle_root)

        artifact_target = artifact.with_name("artifact-target")
        artifact.rename(artifact_target)
        artifact.symlink_to(artifact_target)
        with self.assertRaisesRegex(ProductBundleError, "real regular file"):
            verify_product_artifact(bundle, "server", artifact)

        runtime_path = bundle_root / "runtime-requirements.json"
        runtime_target = bundle_root / "runtime-target.json"
        runtime_path.rename(runtime_target)
        runtime_path.symlink_to(runtime_target)
        with self.assertRaisesRegex(ProductBundleError, "symlink|real regular file"):
            load_product_operations_bundle(bundle_root)

    def test_copy_time_artifact_tampering_fails_before_execution(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-copy-race", self.port)
        plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        copied_artifact = False
        original_copyfile = shutil.copyfile

        def tampering_copyfile(source, destination, *, follow_symlinks=True):
            nonlocal copied_artifact
            result = original_copyfile(
                source, destination, follow_symlinks=follow_symlinks
            )
            destination_path = Path(destination)
            if destination_path.parts[-2:] == ("bin", "server") and not copied_artifact:
                destination_path.write_text("tampered", encoding="utf-8")
                copied_artifact = True
            return result

        with mock.patch(
            "ophelia.product_execution.shutil.copyfile",
            side_effect=tampering_copyfile,
        ):
            with self.assertRaisesRegex(ProductExecutionError, "failed verification"):
                apply_product_release(
                    bundle_root,
                    artifact,
                    runtime_root=self.runtime,
                    environment_values={},
                    confirm=plan["confirmation_token"],
                )
        self.assertFalse(list(self.runtime.glob("apps/*/environments/*/traffic/active.json")))

    def test_revision_copy_tampering_cannot_reach_the_process(self) -> None:
        bundle_root, artifact = _fixture(self.root, "fixture-revision-race", self.port)
        plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        original_copyfile = shutil.copyfile

        def tampering_copyfile(source, destination, *, follow_symlinks=True):
            result = original_copyfile(
                source, destination, follow_symlinks=follow_symlinks
            )
            destination_path = Path(destination)
            if (
                "revisions" in destination_path.parts
                and destination_path.parts[-2:] == ("bin", "server")
            ):
                destination_path.write_text("tampered", encoding="utf-8")
            return result

        with mock.patch(
            "ophelia.execution.process_backend.shutil.copyfile",
            side_effect=tampering_copyfile,
        ):
            receipt = apply_product_release(
                bundle_root,
                artifact,
                runtime_root=self.runtime,
                environment_values={},
                confirm=plan["confirmation_token"],
            )
        self.assertEqual("failed_compensated", receipt["status"])
        self.assertFalse(list(self.runtime.glob("apps/*/environments/*/traffic/active.json")))

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

        first_secret = "first-secret-that-must-not-be-persisted"
        second_secret = "second-secret-that-must-not-be-persisted"
        first_secret_plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={"APP_SECRET": first_secret},
        )
        second_secret_plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={"APP_SECRET": second_secret},
        )
        self.assertNotEqual(
            first_secret_plan["confirmation_token"],
            second_secret_plan["confirmation_token"],
        )
        with self.assertRaisesRegex(
            ProductExecutionError, "does not match"
        ):
            apply_product_release(
                bundle_root,
                artifact,
                runtime_root=self.runtime,
                environment_values={"APP_SECRET": second_secret},
                confirm=first_secret_plan["confirmation_token"],
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
                self.assertNotIn(first_secret.encode(), path.read_bytes(), path)
                self.assertNotIn(second_secret.encode(), path.read_bytes(), path)

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

    def test_multi_replica_release_health_checks_and_balances_the_full_set(self) -> None:
        bundle_root, artifact = _fixture(
            self.root, "fixture-replicated", self.port, replicas=3
        )
        plan = product_release_plan(
            bundle_root, artifact, runtime_root=self.runtime, environment_values={}
        )
        receipt = apply_product_release(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=plan["confirmation_token"],
        )
        self.assertEqual("succeeded", receipt["status"])
        active_path = next(self.runtime.glob("apps/*/environments/*/traffic/active.json"))
        active = json.loads(active_path.read_text())
        self.assertEqual(2, active["schema_version"])
        self.assertEqual(3, len(active["upstreams"]))
        instances = {
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/instance"
            ).read()
            for _ in range(9)
        }
        self.assertEqual(3, len(instances))

    def test_startup_migrations_and_release_preconditions_use_current_evidence(self) -> None:
        preconditions = ("backup-complete", "expand-contract-compatible", "health-probe")
        first_root, first_artifact = _fixture(
            self.root,
            "fixture-evidence-v1",
            self.port,
            release_preconditions=preconditions,
            with_migration=True,
        )
        first_plan = product_release_plan(
            first_root, first_artifact, runtime_root=self.runtime, environment_values={}
        )
        self.assertTrue(first_plan["can_apply"])
        self.assertEqual(1, first_plan["migration_count"])
        self.assertEqual(
            {"not-required-initial-release", "verified-during-execution"},
            {item["status"] for item in first_plan["preconditions"]},
        )
        apply_product_release(
            first_root,
            first_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=first_plan["confirmation_token"],
        )

        second_root, second_artifact = _fixture(
            self.root,
            "fixture-evidence-v2",
            self.port,
            release_preconditions=preconditions,
            with_migration=True,
        )
        blocked = product_release_plan(
            second_root, second_artifact, runtime_root=self.runtime, environment_values={}
        )
        self.assertFalse(blocked["can_apply"])
        self.assertEqual(
            {"backup-complete", "expand-contract-compatible"},
            {
                item["precondition"]
                for item in blocked["blockers"]
                if item["code"] == "product_release_precondition_missing"
            },
        )

        backup_plan = product_backup_plan(
            first_root,
            runtime_root=self.runtime,
            backup_id="before-evidence-v2",
            dataset_bindings={},
        )
        backup = create_product_backup(
            first_root,
            runtime_root=self.runtime,
            backup_id="before-evidence-v2",
            dataset_bindings={},
            environment_values={},
            confirm=backup_plan["confirmation_token"],
        )
        backup_root = Path(backup["backup_path"])
        manifest_path = backup_root / "backup-manifest.json"
        original_manifest = manifest_path.read_bytes()
        manifest = json.loads(original_manifest)
        forged_root = backup_root.with_name("forged-without-receipt")
        backup_root.rename(forged_root)
        forged_manifest_path = forged_root / "backup-manifest.json"
        forged_manifest = deepcopy(manifest)
        forged_manifest["backup_id"] = "forged-without-receipt"
        _write(forged_manifest_path, forged_manifest)
        forged_plan = product_release_plan(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        self.assertIn(
            "backup-complete",
            {
                item["precondition"]
                for item in forged_plan["blockers"]
                if item["code"] == "product_release_precondition_missing"
            },
        )
        forged_manifest_path.write_bytes(original_manifest)
        forged_root.rename(backup_root)

        dataset_path = backup_root / manifest["datasets"][0]["relative_path"]
        dataset_bytes = dataset_path.read_bytes()
        dataset_path.write_bytes(dataset_bytes + b"tampered")
        tampered_plan = product_release_plan(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        self.assertIn(
            "backup-complete",
            {
                item["precondition"]
                for item in tampered_plan["blockers"]
                if item["code"] == "product_release_precondition_missing"
            },
        )
        dataset_path.write_bytes(dataset_bytes)

        evidence = self.root / "expand-contract-reviewed.json"
        evidence.write_text('{"compatible":true}\n', encoding="utf-8")
        planned = product_release_plan(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
            precondition_evidence={"expand-contract-compatible": evidence},
        )
        self.assertTrue(planned["can_apply"])
        self.assertEqual(
            {"verified", "verified-during-execution"},
            {item["status"] for item in planned["preconditions"]},
        )
        receipt = apply_product_release(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
            precondition_evidence={"expand-contract-compatible": evidence},
            confirm=planned["confirmation_token"],
        )
        self.assertEqual("succeeded", receipt["status"])

    def test_provider_filesystem_backup_and_isolated_restore_are_executable(self) -> None:
        bundle_root, artifact = _fixture(
            self.root,
            "fixture-provider",
            self.port,
            provider_dataset=True,
        )
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
        provider_root = self.root / "provider-objects"
        provider_root.mkdir()
        (provider_root / "probe.txt").write_text("restored-provider", encoding="utf-8")
        bindings = {"storage.object.private": provider_root}
        backup_plan = product_backup_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="provider-backup",
            dataset_bindings=bindings,
        )
        self.assertTrue(backup_plan["can_apply"])
        backup = create_product_backup(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="provider-backup",
            dataset_bindings=bindings,
            environment_values={},
            confirm=backup_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", backup["status"])
        drill_plan = product_restore_drill_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="provider-backup",
            drill_id="provider-drill",
        )
        self.assertTrue(drill_plan["can_apply"])
        drill = apply_product_restore_drill(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="provider-backup",
            drill_id="provider-drill",
            environment_values={},
            confirm=drill_plan["confirmation_token"],
        )
        restored = (
            Path(drill["restore_path"])
            / "providers"
            / "storage-object-private"
            / "probe.txt"
        )
        self.assertEqual("restored-provider", restored.read_text(encoding="utf-8"))

    def test_postgres_provider_adapter_keeps_credentials_out_of_argv(self) -> None:
        target = self.root / "postgres.dump"
        database_url = "postgresql://operator:secret@example.com/fixture_ophelia_drill_drill_v1"
        contract = {
            "id": "product-primary",
            "kind": "postgresql",
            "restore_validations": ["postgres-restore", "migration-version"],
        }
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "/tools/pg_dump":
                Path(command[command.index("--file") + 1]).write_bytes(b"pgdump")
                return SimpleNamespace(returncode=0, stdout=None)
            if command[0] == "/tools/psql":
                return SimpleNamespace(returncode=0, stdout="63\n")
            return SimpleNamespace(returncode=0, stdout=None)

        with mock.patch(
            "ophelia.product_recovery.shutil.which",
            side_effect=lambda name: "/tools/" + name,
        ), mock.patch("ophelia.product_recovery.subprocess.run", side_effect=run):
            record = _backup_postgresql(contract, database_url, target)
            self.assertEqual("postgresql", record["kind"])
            self.assertTrue(_isolated_postgres_target(database_url, "drill-v1"))
            _restore_postgresql(target, database_url, "drill-v1")

        self.assertTrue(calls)
        self.assertNotIn(database_url, repr(calls))
        self.assertFalse(
            _isolated_postgres_target(
                "postgresql://operator:secret@example.com/production", "drill-v1"
            )
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
        for field in (
            "ophelia_request_digest",
            "ophelia_plan_digest",
            "ophelia_approval_digest",
            "ophelia_operation_digest",
            "ophelia_verification_digest",
            "ophelia_receipt_digest",
        ):
            self.assertTrue(first[field].startswith("sha256:"), field)
        self.assertEqual(
            first["ophelia_plan_digest"],
            first["ophelia_receipt"]["plan_digest"],
        )
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

        redeploy_plan = product_release_plan(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        redeployed = apply_product_release(
            second_root,
            second_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=redeploy_plan["confirmation_token"],
        )
        self.assertEqual("succeeded", redeployed["status"])
        self.assertEqual(
            b"fixture-v2",
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/version"
            ).read(),
        )

    def test_rollback_is_governed_by_the_active_release_policy(self) -> None:
        target_root, target_artifact = _fixture(
            self.root, "fixture-rollback-target", self.port
        )
        no_active = product_release_plan(
            target_root,
            target_artifact,
            runtime_root=self.runtime,
            environment_values={},
            operation="rollback.apply",
        )
        self.assertFalse(no_active["can_apply"])
        self.assertIn(
            "product_rollback_without_active_revision",
            {item["code"] for item in no_active["blockers"]},
        )

        active_root, active_artifact = _fixture(
            self.root,
            "fixture-active-restore-required",
            self.port,
            rollback_policy="restore-required",
        )
        active_plan = product_release_plan(
            active_root,
            active_artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        apply_product_release(
            active_root,
            active_artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=active_plan["confirmation_token"],
        )
        blocked = product_release_plan(
            target_root,
            target_artifact,
            runtime_root=self.runtime,
            environment_values={},
            operation="rollback.apply",
        )
        self.assertFalse(blocked["can_apply"])
        self.assertIn(
            "product_rollback_policy_unsupported",
            {item["code"] for item in blocked["blockers"]},
        )

    def test_recovery_evidence_replay_rejects_tampering_and_contract_drift(self) -> None:
        bundle_root, artifact = _fixture(
            self.root, "fixture-evidence-integrity", self.port
        )
        release_plan = product_release_plan(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
        )
        apply_product_release(
            bundle_root,
            artifact,
            runtime_root=self.runtime,
            environment_values={},
            confirm=release_plan["confirmation_token"],
        )
        backup_plan = product_backup_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            dataset_bindings={},
        )
        backup = create_product_backup(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            dataset_bindings={},
            environment_values={},
            confirm=backup_plan["confirmation_token"],
        )
        for field in (
            "runtime_contract_digest",
            "runtime_document_digest",
            "release_manifest_digest",
            "release_document_digest",
            "recovery_contract_digest",
            "recovery_document_digest",
            "ophelia_request_digest",
            "ophelia_plan_digest",
            "ophelia_approval_digest",
            "ophelia_operation_digest",
            "ophelia_verification_digest",
            "ophelia_receipt_digest",
        ):
            self.assertTrue(backup[field].startswith("sha256:"), field)

        backup_root = Path(backup["backup_path"])
        manifest_path = backup_root / "backup-manifest.json"
        original_manifest = manifest_path.read_bytes()
        original = json.loads(original_manifest)

        def assert_manifest_blocked(mutated) -> None:
            manifest_path.write_text(
                json.dumps(mutated, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            blocked = product_restore_drill_plan(
                bundle_root,
                runtime_root=self.runtime,
                backup_id="integrity-backup",
                drill_id="integrity-drill",
            )
            self.assertFalse(blocked["can_apply"])
            self.assertIn(
                "product_backup_invalid",
                {item["code"] for item in blocked["blockers"]},
            )
            manifest_path.write_bytes(original_manifest)

        substituted = deepcopy(original)
        substituted["revision_digest"] = "sha256:" + "0" * 64
        assert_manifest_blocked(substituted)

        unsafe = deepcopy(original)
        unsafe["datasets"][0]["relative_path"] = "../../outside"
        assert_manifest_blocked(unsafe)

        missing = deepcopy(original)
        missing["datasets"] = []
        assert_manifest_blocked(missing)

        duplicated = deepcopy(original)
        duplicated["datasets"].append(deepcopy(duplicated["datasets"][0]))
        assert_manifest_blocked(duplicated)

        dataset_path = backup_root / original["datasets"][0]["relative_path"]
        external = self.root / "substituted-backup-dataset"
        dataset_path.rename(external)
        dataset_path.symlink_to(external)
        blocked = product_restore_drill_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            drill_id="integrity-drill",
        )
        self.assertFalse(blocked["can_apply"])
        self.assertIn(
            "product_backup_invalid",
            {item["code"] for item in blocked["blockers"]},
        )
        dataset_path.unlink()
        external.rename(dataset_path)

        stale_plan = product_restore_drill_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            drill_id="integrity-drill",
        )
        dataset_bytes = dataset_path.read_bytes()
        dataset_path.write_bytes(dataset_bytes + b"substituted-snapshot")
        replaced = deepcopy(original)
        replaced["datasets"][0]["digest"] = _path_digest(dataset_path)
        manifest_path.write_text(
            json.dumps(replaced, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        replaced_plan = product_restore_drill_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            drill_id="integrity-drill",
        )
        self.assertFalse(replaced_plan["can_apply"])
        self.assertIn(
            "product_backup_invalid",
            {item["code"] for item in replaced_plan["blockers"]},
        )
        with self.assertRaisesRegex(ProductRecoveryError, "does not match"):
            apply_product_restore_drill(
                bundle_root,
                runtime_root=self.runtime,
                backup_id="integrity-backup",
                drill_id="integrity-drill",
                environment_values={},
                confirm=stale_plan["confirmation_token"],
            )
        dataset_path.write_bytes(dataset_bytes)
        manifest_path.write_bytes(original_manifest)

        drill_plan = product_restore_drill_plan(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            drill_id="integrity-drill",
        )
        drill = apply_product_restore_drill(
            bundle_root,
            runtime_root=self.runtime,
            backup_id="integrity-backup",
            drill_id="integrity-drill",
            environment_values={},
            confirm=drill_plan["confirmation_token"],
        )
        correlated_path = (
            self.runtime
            / "receipts"
            / "product"
            / f"{drill['ophelia_receipt']['receipt_id']}.json"
        )
        correlated = json.loads(correlated_path.read_text())
        correlated["tampered"] = True
        correlated_path.write_text(
            json.dumps(correlated, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ProductRecoveryError, "differs from terminal evidence"
        ):
            apply_product_restore_drill(
                bundle_root,
                runtime_root=self.runtime,
                backup_id="integrity-backup",
                drill_id="integrity-drill",
                environment_values={},
                confirm=drill_plan["confirmation_token"],
            )


if __name__ == "__main__":
    unittest.main()
