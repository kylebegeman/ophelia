from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.domain import (
    ApprovedPlanRef,
    AuthorizationKind,
    CheckStatus,
    CompensationAction,
    CompensationResult,
    CompensationStatus,
    ContractValidationError,
    KERNEL_CONTRACT_SCHEMA_VERSION,
    ObservedRevision,
    ObservedWorkload,
    OperationEvent,
    OperationPlan,
    OperationRef,
    OperationRequest,
    OperationState,
    PlanPhase,
    PlanStep,
    ReceiptEffect,
    ReceiptOutcome,
    Revision,
    RevisionState,
    TerminalReceipt,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
    Workload,
    WorkloadKind,
    canonical_digest,
    canonical_json,
)


D1 = "sha256:" + ("1" * 64)
D2 = "sha256:" + ("2" * 64)
D3 = "sha256:" + ("3" * 64)
D4 = "sha256:" + ("4" * 64)
D5 = "sha256:" + ("5" * 64)
D6 = "sha256:" + ("6" * 64)
CREATED = "2026-07-11T17:00:00Z"
APPROVED = "2026-07-11T17:05:00Z"
EXPIRES = "2026-07-11T17:20:00Z"


def _request() -> OperationRequest:
    return OperationRequest(
        request_id="request_example-1",
        operation="deploy.apply",
        host_id="host_example-1",
        app="demo-service",
        environment="production",
        revision_id="rev_example-1",
        revision_digest=D1,
        idempotency_key="deploy-demo-service-rev-example-1",
    )


def _steps() -> tuple[PlanStep, ...]:
    return (
        PlanStep(1, PlanPhase.STAGE, True, D1, CompensationAction.DELETE_CANDIDATE),
        PlanStep(2, PlanPhase.PREFLIGHT, False, D2, CompensationAction.NONE),
        PlanStep(3, PlanPhase.START_CANDIDATE, True, D3, CompensationAction.STOP_CANDIDATE),
        PlanStep(4, PlanPhase.READINESS_VERIFY, False, D4, CompensationAction.NONE),
        PlanStep(5, PlanPhase.SWITCH_TRAFFIC, True, D5, CompensationAction.RESTORE_TRAFFIC),
        PlanStep(6, PlanPhase.EXTERNAL_VERIFY, False, D6, CompensationAction.NONE),
        PlanStep(7, PlanPhase.COMMIT, True, D6, CompensationAction.RECOVER_FROM_JOURNAL),
    )


def _plan() -> OperationPlan:
    return OperationPlan.create(
        request=_request(),
        manifest_digest=D2,
        artifact_digests=(D3, D4),
        observed_state_digest=D5,
        policy_digest=D6,
        steps=_steps(),
        blocker_codes=(),
        created_at=CREATED,
    )


def _approval() -> ApprovedPlanRef:
    return ApprovedPlanRef.bind(
        _plan(),
        actor_id="actor_kyle",
        decision_id="decision_example-1",
        authorization_kind=AuthorizationKind.LUMEN_DECISION,
        issuer="lumen-control-plane",
        audience="host_example-1",
        approved_at=APPROVED,
        expires_at=EXPIRES,
        approval_nonce="one-time-super-secret-nonce",
    )


def _observed_workload() -> ObservedWorkload:
    return ObservedWorkload(
        workload_id="worker",
        workload_kind=WorkloadKind.WORKER,
        runtime_state="ready",
        artifact_digest=D1,
        ready=True,
        fencing_token_digest=D6,
    )


def _observed_revision() -> ObservedRevision:
    return ObservedRevision(
        host_id="host_example-1",
        app="demo-service",
        environment="production",
        revision_id="rev_example-1",
        revision_digest=D1,
        state=RevisionState.READY,
        workloads=(_observed_workload(),),
        observed_at=CREATED,
    )


def _verification() -> VerificationResult:
    return VerificationResult(
        status=VerificationStatus.PASSED,
        observed_revision_digest=D1,
        observed_state_digest=D2,
        observed_at=CREATED,
        checks=(
            VerificationCheck(
                "external",
                CheckStatus.PASSED,
                D3,
                "Observed healthy.",
            ),
        ),
    )


def _receipt() -> TerminalReceipt:
    plan = _plan()
    return TerminalReceipt(
        receipt_id="receipt_example-1",
        operation_id="operation_example-1",
        operation="deploy.apply",
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest(),
        decision_id="decision_example-1",
        host_id="host_example-1",
        app="demo-service",
        environment="production",
        previous_revision_id="rev_previous",
        desired_revision_id="rev_example-1",
        desired_revision_digest=D1,
        active_revision_id="rev_example-1",
        active_revision_digest=D1,
        artifact_digests=(D3,),
        verification=_verification(),
        compensation=CompensationResult(
            attempted=False,
            status=CompensationStatus.NOT_NEEDED,
        ),
        effect=ReceiptEffect.RUNTIME_ACTIVATED,
        outcome=ReceiptOutcome.SUCCEEDED,
        started_at=CREATED,
        completed_at="2026-07-11T17:10:00Z",
    )


class KernelContractTests(unittest.TestCase):
    def test_contracts_are_deeply_immutable_by_shape(self) -> None:
        plan = _plan()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.app = "other"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.steps[0].order = 9  # type: ignore[misc]
        self.assertIsInstance(plan.steps, tuple)
        self.assertIsInstance(plan.artifact_digests, tuple)

    def test_revision_content_digest_is_deterministic_and_excludes_identity_time(self) -> None:
        workload = Workload(
            workload_id="web",
            workload_kind=WorkloadKind.WEB,
            artifact_digest=D1,
            route_ids=("public",),
        )
        first = Revision(
            revision_id="rev_first",
            app="demo-service",
            environment="production",
            manifest_digest=D2,
            artifact_digests=(D1,),
            renderer_version="ophelia-0.4.4",
            workloads=(workload,),
            created_at="2026-07-11T17:00:00Z",
        )
        second = Revision(
            revision_id="rev_second",
            app="demo-service",
            environment="production",
            manifest_digest=D2,
            artifact_digests=(D1,),
            renderer_version="ophelia-0.4.4",
            workloads=(workload,),
            created_at="2026-07-11T18:00:00Z",
        )

        self.assertEqual(first.content_digest(), second.content_digest())
        self.assertEqual(first.canonical_json(), canonical_json(first))
        self.assertEqual(canonical_digest(first), first.digest())

    def test_plan_digest_normalizes_retry_identity(self) -> None:
        first = _plan()
        retry_request = dataclasses.replace(
            _request(),
            request_id="request_example-2",
            idempotency_key="retry-key",
        )
        retry = OperationPlan.create(
            request=retry_request,
            manifest_digest=D2,
            artifact_digests=(D3, D4),
            observed_state_digest=D5,
            policy_digest=D6,
            steps=_steps(),
            blocker_codes=(),
            created_at="2026-07-11T18:00:00Z",
        )

        self.assertEqual(first.request_digest, retry.request_digest)
        self.assertEqual(first.plan_digest(), retry.plan_digest())
        self.assertEqual(first.plan_id, retry.plan_id)

    def test_approval_binds_exact_plan_claims_without_raw_nonce(self) -> None:
        plan = _plan()
        approved = _approval()
        encoded = json.dumps(approved.to_dict(), sort_keys=True)

        self.assertTrue(approved.matches(plan))
        self.assertTrue(approved.verifies_nonce("one-time-super-secret-nonce"))
        self.assertFalse(approved.verifies_nonce("different"))
        self.assertEqual(plan.plan_id, approved.plan_id)
        self.assertEqual(plan.plan_digest(), approved.plan_digest)
        self.assertEqual(plan.observed_state_digest, approved.observed_state_digest)
        self.assertEqual(plan.policy_digest, approved.policy_digest)
        self.assertNotIn("one-time-super-secret-nonce", encoded)
        self.assertNotIn('"nonce"', encoded)
        self.assertIn("nonce_digest", approved.to_dict())

        changed = dataclasses.replace(plan, observed_state_digest=D1)
        self.assertFalse(approved.matches(changed))
        with self.assertRaisesRegex(ContractValidationError, "audience"):
            dataclasses.replace(approved, audience="host_other")

    def test_schema_version_and_internal_kind_are_explicit(self) -> None:
        payload = _plan().to_dict()

        self.assertEqual(1, KERNEL_CONTRACT_SCHEMA_VERSION)
        self.assertEqual(KERNEL_CONTRACT_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual("ophelia.kernel.operation_plan", payload["kind"])
        self.assertEqual(
            KERNEL_CONTRACT_SCHEMA_VERSION,
            payload["steps"][0]["schema_version"],
        )

    def test_workload_kinds_encode_distinct_activation_semantics(self) -> None:
        self.assertEqual("blue_green", WorkloadKind.WEB.activation_semantics)
        self.assertEqual("serial_replace", WorkloadKind.INTERNAL.activation_semantics)
        self.assertEqual("fenced_handoff", WorkloadKind.WORKER.activation_semantics)
        self.assertEqual("fenced_singleton", WorkloadKind.CRON.activation_semantics)
        self.assertEqual("idempotent_once", WorkloadKind.TASK.activation_semantics)
        self.assertEqual("exactly_once", WorkloadKind.MIGRATION.activation_semantics)
        self.assertEqual("atomic_pointer", WorkloadKind.STATIC.activation_semantics)
        self.assertTrue(WorkloadKind.WEB.overlap_allowed_by_default)
        self.assertTrue(WorkloadKind.INTERNAL.long_running)
        self.assertFalse(WorkloadKind.INTERNAL.receives_traffic)
        self.assertFalse(WorkloadKind.INTERNAL.overlap_allowed_by_default)
        self.assertFalse(WorkloadKind.WORKER.overlap_allowed_by_default)
        self.assertTrue(WorkloadKind.TASK.requires_idempotency_key)
        self.assertTrue(WorkloadKind.STATIC.receives_traffic)

        with self.assertRaises(ContractValidationError):
            Workload(
                workload_id="worker",
                workload_kind=WorkloadKind.WORKER,
                artifact_digest=D1,
                route_ids=("public",),
            )

    def test_revision_requires_every_workload_artifact(self) -> None:
        workload = Workload(
            workload_id="internal",
            workload_kind=WorkloadKind.INTERNAL,
            artifact_digest=D1,
        )

        with self.assertRaisesRegex(ContractValidationError, "artifact_digest"):
            Revision(
                revision_id="rev_missing-artifact",
                app="demo-service",
                environment="production",
                manifest_digest=D2,
                artifact_digests=(D3,),
                renderer_version="ophelia-0.4.4",
                workloads=(workload,),
                created_at=CREATED,
            )

    def test_fencing_token_digest_is_preserved_but_raw_value_is_redacted(self) -> None:
        payload = _observed_workload().to_dict()

        self.assertEqual(D6, payload["fencing_token_digest"])
        unsafe = canonical_json({"fencing_token_digest": "raw-fencing-secret"})
        self.assertNotIn("raw-fencing-secret", unsafe)
        self.assertIn("<redacted>", unsafe)

    def test_raw_strings_are_rejected_for_every_enum_contract_field(self) -> None:
        event = OperationEvent(
            event_id="event_example-1",
            operation_id="operation_example-1",
            sequence=1,
            event_type="operation.started",
            occurred_at=CREATED,
            state=OperationState.EXECUTING,
            host_id="host_example-1",
            app="demo-service",
            environment="production",
        )
        valid_ref = OperationRef(
            operation_id="operation_example-1",
            request_id="request_example-1",
            state=OperationState.ACCEPTED,
        )
        valid_workload = Workload(
            workload_id="web",
            workload_kind=WorkloadKind.WEB,
            artifact_digest=D1,
        )
        cases = (
            ("operation_ref.state", lambda: dataclasses.replace(valid_ref, state="accepted")),
            ("event.state", lambda: dataclasses.replace(event, state="executing")),
            ("plan_step.phase", lambda: dataclasses.replace(_steps()[0], phase="stage")),
            (
                "plan_step.compensation",
                lambda: PlanStep(
                    1,
                    PlanPhase.STAGE,
                    True,
                    D1,
                    "none",
                ),
            ),
            (
                "approval.authorization_kind",
                lambda: dataclasses.replace(
                    _approval(),
                    authorization_kind="lumen_decision",
                ),
            ),
            (
                "workload.workload_kind",
                lambda: dataclasses.replace(valid_workload, workload_kind="web"),
            ),
            (
                "observed_workload.workload_kind",
                lambda: dataclasses.replace(
                    _observed_workload(),
                    workload_kind="worker",
                ),
            ),
            (
                "observed_revision.state",
                lambda: dataclasses.replace(_observed_revision(), state="ready"),
            ),
            (
                "verification_check.status",
                lambda: dataclasses.replace(
                    _verification().checks[0],
                    status="passed",
                ),
            ),
            (
                "verification.status",
                lambda: dataclasses.replace(_verification(), status="passed"),
            ),
            (
                "compensation.status",
                lambda: CompensationResult(
                    attempted=True,
                    status="not_needed",
                ),
            ),
            (
                "receipt.effect",
                lambda: dataclasses.replace(
                    _receipt(),
                    effect="runtime_activated",
                ),
            ),
            (
                "receipt.outcome",
                lambda: dataclasses.replace(
                    _receipt(),
                    outcome="succeeded",
                    active_revision_digest=D2,
                ),
            ),
        )

        for name, factory in cases:
            with self.subTest(name=name):
                with self.assertRaises(ContractValidationError):
                    factory()

    def test_secret_safe_serialization_redacts_values_and_excludes_output(self) -> None:
        payload = canonical_json(
            {
                "schema_version": 1,
                "api_token": "registered-secret",
                "database": "postgres://user:password@db.example.com/app",
                "nonce": "raw-nonce",
                "stdout": "unbounded process output",
                "output": "alternate unbounded output",
                "summary": "safe",
            }
        )

        self.assertNotIn("registered-secret", payload)
        self.assertNotIn("user:password", payload)
        self.assertNotIn("raw-nonce", payload)
        self.assertNotIn("unbounded process output", payload)
        self.assertNotIn("alternate unbounded output", payload)
        self.assertIn("<redacted>", payload)
        self.assertIn('"summary":"safe"', payload)

    def test_event_messages_are_bounded_and_chainable(self) -> None:
        event = OperationEvent(
            event_id="event_example-1",
            operation_id="operation_example-1",
            sequence=2,
            event_type="candidate.verified",
            occurred_at=CREATED,
            state=OperationState.EXECUTING,
            host_id="host_example-1",
            app="demo-service",
            environment="production",
            revision_id="rev_example-1",
            evidence_digests=(D1,),
            previous_event_digest=D2,
        )

        self.assertEqual(D2, event.to_dict()["previous_event_digest"])
        with self.assertRaises(ContractValidationError):
            dataclasses.replace(event, message="x" * 2049)

    def test_success_receipt_requires_desired_active_and_observed_match(self) -> None:
        receipt = _receipt()

        self.assertEqual("succeeded", receipt.to_dict()["outcome"])
        self.assertTrue(receipt.to_dict()["inputs_redacted"])
        with self.assertRaisesRegex(ContractValidationError, "desired, active, and observed"):
            dataclasses.replace(receipt, active_revision_digest=D2)

    def test_compensation_can_truthfully_restore_an_empty_first_deploy(self) -> None:
        compensation = CompensationResult(
            attempted=True,
            status=CompensationStatus.SUCCEEDED,
        )
        receipt = TerminalReceipt(
            receipt_id="receipt_first-deploy-failed",
            operation_id="operation_first-deploy-failed",
            operation="deploy.apply",
            plan_id=_plan().plan_id,
            plan_digest=_plan().plan_digest(),
            decision_id="decision_example-1",
            host_id="host_example-1",
            app="demo-service",
            environment="production",
            previous_revision_id=None,
            desired_revision_id="rev_example-1",
            desired_revision_digest=D1,
            active_revision_id=None,
            active_revision_digest=None,
            artifact_digests=(D3,),
            verification=VerificationResult(
                status=VerificationStatus.FAILED,
                observed_revision_digest=None,
                observed_state_digest=D2,
                observed_at=CREATED,
                checks=(VerificationCheck("readiness", CheckStatus.FAILED),),
            ),
            compensation=compensation,
            effect=ReceiptEffect.NONE,
            outcome=ReceiptOutcome.FAILED_COMPENSATED,
            started_at=CREATED,
            completed_at="2026-07-11T17:10:00Z",
        )

        self.assertIsNone(receipt.compensation.restored_revision_id)
        self.assertIsNone(receipt.active_revision_id)
        with self.assertRaisesRegex(ContractValidationError, "together"):
            dataclasses.replace(compensation, restored_revision_id="rev_partial")


if __name__ == "__main__":
    unittest.main()
