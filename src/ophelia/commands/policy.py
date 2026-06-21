from __future__ import annotations

import json
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..command_catalog import CommandDescriptor, register_cli_descriptor
from ..config import DEFAULT_RUNTIME_ROOT
from ..policy import (
    PolicyError,
    evaluate_policy,
    explain_policy,
    load_policy,
    validate_policy,
)


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("policy", help="Inspect and evaluate the deterministic safety policy")
    policy_subparsers = parser.add_subparsers(dest="policy_command")

    validate_parser = policy_subparsers.add_parser("validate", help="Structurally validate the resolved policy")
    validate_parser.add_argument("--policy", type=Path, help="Explicit policy file (overrides resolution order)")
    validate_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    validate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate_parser.set_defaults(handler=run_validate)

    explain_parser = policy_subparsers.add_parser("explain", help="Dump the resolved policy rules (read-only)")
    explain_parser.add_argument("--policy", type=Path, help="Explicit policy file (overrides resolution order)")
    explain_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    explain_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    explain_parser.set_defaults(handler=run_explain)

    evaluate_parser = policy_subparsers.add_parser("evaluate", help="Evaluate an operation against the policy")
    evaluate_parser.add_argument("--operation", required=True, help="Operation id (e.g. app.traffic.apply)")
    evaluate_parser.add_argument("--app", help="App id for the evaluation context")
    evaluate_parser.add_argument("--environment", choices=["dev", "staging", "production"])
    evaluate_parser.add_argument("--policy", type=Path, help="Explicit policy file (overrides resolution order)")
    evaluate_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    # Optional fact flags. Anything not supplied is treated as not-yet-satisfied,
    # so an evaluation with no facts shows what WOULD block.
    evaluate_parser.add_argument("--has-target-health-check", action="store_true", help="Context: a target health check is present")
    evaluate_parser.add_argument("--has-rollback", action="store_true", help="Context: a rollback path is available")
    evaluate_parser.add_argument("--confirmation-required", action="store_true", help="Context: a confirmation token is required")
    evaluate_parser.add_argument("--plan-exists", action="store_true", help="Context: a plan was produced before apply")
    evaluate_parser.add_argument("--json-receipts", action="store_true", help="Context: JSON receipts are emitted")
    evaluate_parser.add_argument("--image-digest-pinned", action="store_true", help="Context: images are sha256-pinned")
    evaluate_parser.add_argument("--provider-config-validated", action="store_true", help="Context: provider config validated")
    evaluate_parser.add_argument("--restore-drill-present", action="store_true", help="Context: a restore drill receipt exists")
    evaluate_parser.add_argument("--readiness-clean", action="store_true", help="Context: readiness has no blockers")
    evaluate_parser.add_argument("--backup-fresh", action="store_true", help="Context: a fresh backup exists")
    evaluate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    evaluate_parser.set_defaults(handler=run_evaluate)


def run_validate(args: Namespace) -> int:
    try:
        policy = load_policy(args.policy, runtime_root=args.runtime_root)
    except PolicyError as exc:
        return _print_policy_error(exc, args.json)
    report = validate_policy(policy)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        print(f"  source: {policy.get('_source')}")
        for blocker in report["blockers"]:
            print(f"  x {blocker['code']}: {blocker['message']}")
        for warning in report["warnings"]:
            print(f"  ! {warning['code']}: {warning['message']}")
    return 1 if report["status"] == "blocked" else 0


def run_explain(args: Namespace) -> int:
    try:
        policy = load_policy(args.policy, runtime_root=args.runtime_root)
    except PolicyError as exc:
        return _print_policy_error(exc, args.json)
    explanation = explain_policy(policy)
    if args.json:
        print(json.dumps(explanation, indent=2, sort_keys=True))
    else:
        print(explanation["summary"])
        for rule in explanation["rules"]:
            env = rule.get("environment") or "any"
            print(f"  - {rule['id']} [{rule['severity']}] {rule['operation']} ({env})")
            for condition in rule["requires"]:
                marker = "" if condition["evaluable"] else " (UNKNOWN -> fail closed)"
                print(f"      requires {condition['condition']}={condition['expected']}{marker}")
    return 0


def run_evaluate(args: Namespace) -> int:
    try:
        policy = load_policy(args.policy, runtime_root=args.runtime_root)
    except PolicyError as exc:
        return _print_policy_error(exc, args.json)
    context = {
        "target_health_check": args.has_target_health_check,
        "rollback_available": args.has_rollback,
        "confirmation_required": args.confirmation_required,
        "plan_exists": args.plan_exists,
        "json_receipts": args.json_receipts,
        "image_digest_pinned": args.image_digest_pinned,
        "provider_config_validated": args.provider_config_validated,
        "restore_drill_present": args.restore_drill_present,
        "readiness_clean": args.readiness_clean,
        "backup_fresh": args.backup_fresh,
    }
    result = evaluate_policy(args.operation, args.app, args.environment, context, policy=policy)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result["summary"])
        for rule in result["rules"]:
            print(f"  [{rule['status']}] {rule['id']} ({rule['severity']}): {rule['message']}")
        for blocker in result["blockers"]:
            print(f"  x {blocker['code']}: {blocker['message']}")
        for warning in result["warnings"]:
            print(f"  ! {warning['code']}: {warning['message']}")
    return 1 if result["status"] == "blocked" else 0


def _print_policy_error(exc: PolicyError, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": str(exc), "kind": "ophelia.error"}, indent=2, sort_keys=True))
    else:
        print(f"policy error: {exc}")
    return 1


_ARGS_BASE = {
    "policy": {"type": "string"},
    "runtime_root": {"type": "string"},
    "json": {"type": "boolean"},
}

register_cli_descriptor(
    CommandDescriptor(
        command="ship policy validate",
        operation="policy.validate",
        summary="Structurally validate the resolved deterministic safety policy.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.policy_validation",
        args_schema={
            "type": "object",
            "properties": dict(_ARGS_BASE),
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.policy_validation.v1",
        artifacts=[],
        safety_notes=["Read-only policy validation. No mutation."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship policy explain",
        operation="policy.explain",
        summary="Dump the resolved policy rules and which conditions are evaluable.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.policy_explanation",
        args_schema={
            "type": "object",
            "properties": dict(_ARGS_BASE),
            "required": [],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.policy_explanation.v1",
        artifacts=[],
        safety_notes=["Read-only policy inspection. No mutation."],
    )
)

register_cli_descriptor(
    CommandDescriptor(
        command="ship policy evaluate",
        operation="policy.evaluate",
        summary="Evaluate an operation against the policy with optional context facts.",
        risk="low",
        mutates_state=False,
        requires_confirmation=False,
        plan_command=None,
        apply_command=None,
        json_kind="ophelia.policy_result",
        args_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string"},
                "app": {"type": "string"},
                "environment": {"type": "string"},
                "has_target_health_check": {"type": "boolean"},
                "has_rollback": {"type": "boolean"},
                "confirmation_required": {"type": "boolean"},
                "plan_exists": {"type": "boolean"},
                "json_receipts": {"type": "boolean"},
                "image_digest_pinned": {"type": "boolean"},
                "provider_config_validated": {"type": "boolean"},
                "restore_drill_present": {"type": "boolean"},
                "readiness_clean": {"type": "boolean"},
                "backup_fresh": {"type": "boolean"},
                **_ARGS_BASE,
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
        output_schema_ref="ophelia.policy_result.v1",
        artifacts=[],
        safety_notes=["Read-only policy evaluation. No mutation; never executes the operation."],
    )
)
