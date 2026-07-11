from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import ReceiptOutcome, RevisionState
from ophelia.execution import JournaledExecutor, SQLiteOperationJournal
from ophelia.execution.legacy_adapter import build_legacy_static_execution
from ophelia.execution.staging import find_confirmed_staging
from ophelia.execution.static_backend import StaticRuntimeBackend
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan
from ophelia.runtime import DeployMetadata


class StaticExecutorIntegrationTests(unittest.TestCase):
    def test_confirmed_static_candidate_runs_through_the_canonical_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("canonical static\n")
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(
                """version: 1
app: canonical-static
environment: production
kind: static
static_root: public
routes:
  - domain: canonical-static.example.com
"""
            )
            metadata = DeployMetadata(
                release_id="release-canonical",
                commit_sha="abc123",
                build_time="2026-07-11T17:00:00Z",
            )
            plan = deploy_plan(
                load_manifest(manifest_path),
                manifest_path,
                runtime_root,
                deploy_metadata=metadata,
            )
            token = str(plan["confirmation_token"])
            confirmed = find_confirmed_staging(
                runtime_root, "canonical-static", token
            )
            manifest = load_manifest(confirmed.manifest_path)
            bundle = build_legacy_static_execution(
                confirmed,
                manifest,
                token,
                host_id="host_static-integration",
                actor_uid=501,
            )
            locked_metadata = DeployMetadata(
                **confirmed.binding["deploy_metadata"], locked=True
            )
            journal = SQLiteOperationJournal.beneath_runtime_root(runtime_root)

            def backend_factory(execution_input, operation, fence):
                return StaticRuntimeBackend(
                    manifest=manifest,
                    manifest_path=confirmed.manifest_path,
                    candidate_root=confirmed.staging.candidate,
                    generated_files=list(confirmed.generated_files),
                    runtime_root=runtime_root,
                    deploy_metadata=locked_metadata,
                    expected_candidate_digest=confirmed.candidate_digest,
                    expected_bundle_hash=confirmed.rendered_bundle_hash,
                    expected_baseline_digest=confirmed.baseline_digest,
                    host_id=execution_input.plan.host_id,
                    operation_id=operation.operation_id,
                    owner_id=fence.owner_id,
                    fencing_token=fence.fencing_token,
                )

            executor = JournaledExecutor(
                journal=journal,
                backend_factory=backend_factory,
            )
            operation = executor.submit(
                bundle.actor,
                bundle.request,
                bundle.approval,
                execution_input=bundle.execution_input,
            )

            receipt = executor.run(
                operation.operation_id, owner_id="worker-static"
            )

            self.assertEqual(ReceiptOutcome.SUCCEEDED, receipt.outcome)
            self.assertEqual(bundle.revision.revision_id, receipt.active_revision_id)
            self.assertEqual(
                b"canonical static\n",
                (
                    runtime_root
                    / "static"
                    / manifest.app
                    / "current"
                    / "index.html"
                ).read_bytes(),
            )
            self.assertEqual(
                RevisionState.ACTIVE,
                journal.revision_history(operation.operation_id)[-1].state,
            )
            self.assertNotIn(token.encode(), journal.database_path.read_bytes())
            journal.integrity_check()


if __name__ == "__main__":
    unittest.main()
