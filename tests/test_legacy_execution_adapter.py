from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import AuthorizationKind, PlanPhase, WorkloadKind
from ophelia.execution.legacy_adapter import (
    build_legacy_static_execution,
    local_host_id,
)
from ophelia.execution.staging import StagingError, find_confirmed_staging
from ophelia.manifest import load_manifest
from ophelia.planning import deploy_plan


class LegacyExecutionAdapterTests(unittest.TestCase):
    def test_reviewed_static_plan_maps_to_exact_secret_free_kernel_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("reviewed\n")
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("static-kernel"))
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            token = str(plan["confirmation_token"])
            confirmed = find_confirmed_staging(runtime_root, "static-kernel", token)

            bundle = build_legacy_static_execution(
                confirmed,
                load_manifest(confirmed.manifest_path),
                token,
                host_id="host_fixture-1",
                actor_uid=501,
            )

            self.assertEqual("actor_local-501", bundle.actor.actor_id)
            self.assertEqual(bundle.revision.revision_id, bundle.request.revision_id)
            self.assertEqual(bundle.revision.content_digest(), bundle.request.revision_digest)
            self.assertEqual(bundle.plan.plan_digest(), bundle.approval.plan_digest)
            self.assertTrue(bundle.approval.matches(bundle.plan))
            self.assertEqual(
                AuthorizationKind.LEGACY_CONFIRMATION_ADAPTER,
                bundle.approval.authorization_kind,
            )
            self.assertEqual(WorkloadKind.STATIC, bundle.revision.workloads[0].workload_kind)
            self.assertEqual(
                [
                    PlanPhase.STAGE,
                    PlanPhase.PREFLIGHT,
                    PlanPhase.START_CANDIDATE,
                    PlanPhase.READINESS_VERIFY,
                    PlanPhase.SWITCH_TRAFFIC,
                    PlanPhase.EXTERNAL_VERIFY,
                    PlanPhase.DRAIN_PREVIOUS,
                    PlanPhase.COMMIT,
                    PlanPhase.EMIT_RECEIPT,
                ],
                [step.phase for step in bundle.plan.steps],
            )
            self.assertEqual(
                Path(plan["staging"]["candidate"]).resolve(),
                (runtime_root / bundle.artifact_relative_root).resolve(),
            )
            encoded = json.dumps(
                {
                    "actor": bundle.actor.to_dict(),
                    "request": bundle.request.to_dict(),
                    "plan": bundle.plan.to_dict(),
                    "approval": bundle.approval.to_dict(),
                    "revision": bundle.revision.to_dict(),
                },
                sort_keys=True,
            )
            self.assertNotIn(token, encoded)
            self.assertIn("nonce_digest", encoded)

    def test_adapter_is_deterministic_and_host_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            public = root / "public"
            public.mkdir()
            (public / "index.html").write_text("reviewed\n")
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("host-bound"))
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            token = str(plan["confirmation_token"])
            confirmed = find_confirmed_staging(runtime_root, "host-bound", token)
            manifest = load_manifest(confirmed.manifest_path)

            first = build_legacy_static_execution(
                confirmed, manifest, token, host_id="host_fixture-1", actor_uid=1
            )
            retry = build_legacy_static_execution(
                confirmed, manifest, token, host_id="host_fixture-1", actor_uid=1
            )
            other_host = build_legacy_static_execution(
                confirmed, manifest, token, host_id="host_fixture-2", actor_uid=1
            )

            self.assertEqual(first.request, retry.request)
            self.assertEqual(first.plan, retry.plan)
            self.assertNotEqual(first.request.request_id, other_host.request.request_id)
            self.assertNotEqual(first.plan.plan_digest(), other_host.plan.plan_digest())
            self.assertEqual(local_host_id("fixture-machine"), local_host_id("fixture-machine"))

    def test_adapter_rejects_wrong_token_and_non_static_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            public = root / "public"
            public.mkdir()
            manifest_path = root / "app.ophelia.yml"
            manifest_path.write_text(_manifest("adapter-reject"))
            plan = deploy_plan(load_manifest(manifest_path), manifest_path, runtime_root)
            token = str(plan["confirmation_token"])
            confirmed = find_confirmed_staging(runtime_root, "adapter-reject", token)

            with self.assertRaisesRegex(StagingError, "does not match"):
                build_legacy_static_execution(
                    confirmed,
                    load_manifest(confirmed.manifest_path),
                    "wrong-token",
                    host_id="host_fixture-1",
                )


def _manifest(app: str) -> str:
    return f"""
version: 1
app: {app}
environment: production
kind: static
static_root: public
routes:
  - domain: {app}.example.com
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
