"""Deterministic, typed safety-policy engine for Ophelia operations.

This module loads a declarative policy file (``config/ophelia-policy.yml`` by
default) and evaluates an operation's context against the rules it declares.
There is no expression language: every rule names a single operation, an
optional environment, and a ``require`` mapping whose keys are condition names
the engine knows how to evaluate. This keeps the policy auditable and the engine
deterministic.

Two safety stances drive the design:

* **Fail closed for unknown required conditions.** If a rule requires a
  condition key the engine cannot evaluate, the rule is treated as a *blocker*
  regardless of its declared severity, because the engine cannot prove the
  safety property the operator intended. The finding code is
  ``policy_unknown_required_condition``.
* **Fail open for unknown advisory/top-level keys.** Unknown top-level keys and
  unknown advisory keys inside an otherwise valid rule become *warnings* during
  validation, never blockers, so the policy schema can grow without breaking
  older engines.

All outputs are plain JSON-serializable dicts and never carry secret values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import DEFAULT_RUNTIME_ROOT, REPO_ROOT
from .findings import SEVERITIES as KNOWN_SEVERITIES
from .operation_schema import SCHEMA_VERSION, issue

POLICY_VALIDATION_KIND = "ophelia.policy_validation"
POLICY_RESULT_KIND = "ophelia.policy_result"
POLICY_EXPLANATION_KIND = "ophelia.policy_explanation"

#: Filename of the policy document, used for runtime-root and repo resolution.
POLICY_FILENAME = "ophelia-policy.yml"

#: Severities a rule may declare. ``blocker`` fails the operation, ``warning``
#: surfaces a non-blocking concern, ``info`` is advisory only. Imported from
#: :mod:`ophelia.findings` so the canonical severity vocabulary lives in one place.

#: Top-level keys the engine understands. Anything else is an advisory key and
#: produces a validation warning (fail open).
KNOWN_TOP_LEVEL_KEYS = ("version", "defaults", "rules")

#: Keys understood inside a single rule. Unknown rule keys are advisory.
KNOWN_RULE_KEYS = ("id", "operation", "environment", "require", "severity", "description")

#: Keys understood inside the top-level defaults block. Defaults are enforced as
#: implicit production rules for mutating operations.
KNOWN_DEFAULT_KEYS = (
    "production_requires_plan",
    "production_requires_confirmation",
    "require_json_receipts",
)


# --------------------------------------------------------------------------- #
# Condition evaluators
# --------------------------------------------------------------------------- #
#
# Each evaluator receives (expected, context) and returns True when the context
# satisfies the requirement. ``expected`` is the value declared in the rule's
# ``require`` mapping (almost always ``True``). The context is a flat mapping of
# facts derived from a plan or supplied on the CLI. Evaluators only read facts;
# a missing fact is treated as "not satisfied" so a policy can describe what
# *would* block before the facts are known.


def _truthy(value: Any) -> bool:
    return bool(value)


def _condition_target_health_check(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("target_health_check")) == _truthy(expected)


def _condition_rollback_available(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("rollback_available")) == _truthy(expected)


def _condition_confirmation_required(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("confirmation_required")) == _truthy(expected)


def _condition_plan_exists(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("plan_exists")) == _truthy(expected)


def _condition_json_receipts(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("json_receipts")) == _truthy(expected)


def _condition_image_digest_pinned(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("image_digest_pinned")) == _truthy(expected)


def _condition_provider_config_validated(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("provider_config_validated")) == _truthy(expected)


def _condition_restore_drill_present(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("restore_drill_present")) == _truthy(expected)


def _condition_readiness_clean(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("readiness_clean")) == _truthy(expected)


def _condition_backup_fresh(expected: Any, context: Dict[str, Any]) -> bool:
    return _truthy(context.get("backup_fresh")) == _truthy(expected)


#: Mapping of every condition key the engine can evaluate. A ``require`` key not
#: present here is unknown -> fail closed.
CONDITION_EVALUATORS: Dict[str, Callable[[Any, Dict[str, Any]], bool]] = {
    "target_health_check": _condition_target_health_check,
    "rollback_available": _condition_rollback_available,
    "confirmation_required": _condition_confirmation_required,
    "plan_exists": _condition_plan_exists,
    "json_receipts": _condition_json_receipts,
    "image_digest_pinned": _condition_image_digest_pinned,
    "provider_config_validated": _condition_provider_config_validated,
    "restore_drill_present": _condition_restore_drill_present,
    "readiness_clean": _condition_readiness_clean,
    "backup_fresh": _condition_backup_fresh,
}

KNOWN_CONDITION_KEYS = tuple(sorted(CONDITION_EVALUATORS))


class PolicyError(Exception):
    """Raised when an explicit policy path is missing or cannot be parsed."""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def policy_search_paths(
    path: Optional[Path] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> List[Path]:
    """Ordered list of candidate policy locations.

    Resolution order: explicit ``path`` (when given), then
    ``<runtime_root>/policy/ophelia-policy.yml``, then the repo default
    ``config/ophelia-policy.yml``.
    """
    candidates: List[Path] = []
    if path is not None:
        candidates.append(Path(path).expanduser())
    candidates.append(Path(runtime_root) / "policy" / POLICY_FILENAME)
    candidates.append(REPO_ROOT / "config" / POLICY_FILENAME)
    return candidates


def load_policy(
    path: Optional[Path] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    """Load and parse a policy document.

    When ``path`` is given it must exist and parse; a missing or invalid
    explicit path raises :class:`PolicyError` with a clear message rather than
    silently falling through to a default. When ``path`` is ``None`` the engine
    tries the runtime-root policy then the repo default, returning the first one
    that exists. Returns the parsed mapping with a ``_source`` key recording the
    resolved file.
    """
    if path is not None:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise PolicyError(f"Policy file not found: {resolved}")
        return _parse_policy_file(resolved)

    for candidate in policy_search_paths(None, runtime_root)[1:]:
        if candidate.exists():
            return _parse_policy_file(candidate)

    raise PolicyError(
        "No policy file found. Expected one of: "
        f"{Path(runtime_root) / 'policy' / POLICY_FILENAME}, "
        f"{REPO_ROOT / 'config' / POLICY_FILENAME}."
    )


def _parse_policy_file(resolved: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - PyYAML is a dependency
        raise PolicyError(
            "PyYAML is required to read the policy file. Install it with "
            "`python3 -m pip install PyYAML`."
        ) from exc
    try:
        raw = yaml.safe_load(resolved.read_text())
    except OSError as exc:
        raise PolicyError(f"Could not read policy file {resolved}: {exc}") from exc
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        raise PolicyError(f"Invalid YAML in policy file {resolved}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"Policy root must be a mapping in {resolved}.")
    raw = dict(raw)
    raw["_source"] = str(resolved)
    return raw


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Structurally validate a parsed policy document.

    Blockers are hard structural problems (missing/invalid ``version``, ``rules``
    not a list, a rule missing ``id``/``operation`` or carrying an invalid
    ``severity``, or a ``require`` that is not a mapping). Warnings are advisory:
    unknown top-level keys, unknown rule keys, and ``require`` conditions that
    reference a key the engine cannot evaluate (fail open at validation time; the
    same condition fails *closed* at evaluation time).
    """
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    rules_checked = 0

    if not isinstance(policy, dict):
        blockers.append(issue("policy_not_mapping", "Policy root must be a mapping."))
        return _validation_result(blockers, warnings, rules_checked)

    version = policy.get("version")
    if version is None:
        blockers.append(issue("policy_version_missing", "Policy is missing a top-level `version`.", "version"))
    elif not isinstance(version, int) or isinstance(version, bool):
        blockers.append(issue("policy_version_invalid", "Policy `version` must be an integer.", "version"))

    for key in policy:
        if key == "_source":
            continue
        if key not in KNOWN_TOP_LEVEL_KEYS:
            warnings.append(
                issue("policy_unknown_top_level_key", f"Unknown top-level policy key `{key}` is ignored.", key)
            )

    defaults = policy.get("defaults")
    if defaults is not None:
        if not isinstance(defaults, dict):
            blockers.append(issue("policy_defaults_not_mapping", "Policy `defaults` must be a mapping.", "defaults"))
        else:
            for key in defaults:
                if key not in KNOWN_DEFAULT_KEYS:
                    warnings.append(
                        issue(
                            "policy_unknown_default_key",
                            f"Unknown policy default `{key}` is ignored.",
                            f"defaults.{key}",
                        )
                    )

    rules = policy.get("rules")
    if rules is None:
        blockers.append(issue("policy_rules_missing", "Policy is missing a `rules` list.", "rules"))
        return _validation_result(blockers, warnings, rules_checked)
    if not isinstance(rules, list):
        blockers.append(issue("policy_rules_not_list", "Policy `rules` must be a list.", "rules"))
        return _validation_result(blockers, warnings, rules_checked)

    seen_ids: Dict[str, int] = {}
    for index, rule in enumerate(rules):
        rules_checked += 1
        path = f"rules[{index}]"
        if not isinstance(rule, dict):
            blockers.append(issue("policy_rule_not_mapping", f"Rule at {path} must be a mapping.", path))
            continue

        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            blockers.append(issue("policy_rule_missing_id", f"Rule at {path} is missing a string `id`.", f"{path}.id"))
        else:
            if rule_id in seen_ids:
                warnings.append(
                    issue(
                        "policy_rule_duplicate_id",
                        f"Duplicate rule id `{rule_id}`; both rules are evaluated and emit findings with the same code.",
                        f"{path}.id",
                    )
                )
            seen_ids[rule_id] = index

        if not isinstance(rule.get("operation"), str) or not rule.get("operation"):
            blockers.append(
                issue("policy_rule_missing_operation", f"Rule at {path} is missing a string `operation`.", f"{path}.operation")
            )

        severity = rule.get("severity")
        if severity not in KNOWN_SEVERITIES:
            blockers.append(
                issue(
                    "policy_rule_invalid_severity",
                    f"Rule at {path} has severity `{severity}`; expected one of {', '.join(KNOWN_SEVERITIES)}.",
                    f"{path}.severity",
                )
            )

        environment = rule.get("environment")
        if environment is not None and not isinstance(environment, str):
            blockers.append(
                issue("policy_rule_invalid_environment", f"Rule at {path} `environment` must be a string when set.", f"{path}.environment")
            )

        require = rule.get("require")
        if require is None:
            warnings.append(
                issue("policy_rule_no_conditions", f"Rule at {path} declares no `require` conditions; it can never fail.", f"{path}.require")
            )
        elif not isinstance(require, dict):
            blockers.append(
                issue("policy_rule_require_not_mapping", f"Rule at {path} `require` must be a mapping.", f"{path}.require")
            )
        else:
            for condition_key in require:
                if condition_key not in CONDITION_EVALUATORS:
                    warnings.append(
                        issue(
                            "policy_unknown_condition_key",
                            f"Rule at {path} requires unknown condition `{condition_key}`; "
                            "it will FAIL CLOSED (treated as a blocker) at evaluation time.",
                            f"{path}.require.{condition_key}",
                        )
                    )

        for key in rule:
            if key not in KNOWN_RULE_KEYS:
                warnings.append(
                    issue("policy_unknown_rule_key", f"Unknown rule key `{key}` at {path} is ignored.", f"{path}.{key}")
                )

    return _validation_result(blockers, warnings, rules_checked)


def _validation_result(
    blockers: List[Dict[str, str]],
    warnings: List[Dict[str, str]],
    rules_checked: int,
) -> Dict[str, Any]:
    status = "blocked" if blockers else ("warn" if warnings else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": POLICY_VALIDATION_KIND,
        "status": status,
        "rules_checked": rules_checked,
        "blockers": blockers,
        "warnings": warnings,
        "summary": (
            f"Policy validation: {len(blockers)} blocker(s), {len(warnings)} warning(s) "
            f"across {rules_checked} rule(s)."
        ),
    }


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _select_rules(policy: Dict[str, Any], operation: str, environment: Optional[str]) -> List[Dict[str, Any]]:
    """Rules matching ``operation`` exactly and an unset-or-equal environment.

    Sorted by ``id`` for deterministic output. Malformed rules (non-mapping or
    missing id) are skipped here; structural problems are surfaced by
    :func:`validate_policy`, not the evaluator.
    """
    rules = policy.get("rules")
    if not isinstance(rules, list):
        return []
    selected: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("operation") != operation:
            continue
        rule_environment = rule.get("environment")
        if rule_environment is not None and rule_environment != environment:
            continue
        if not isinstance(rule.get("id"), str):
            continue
        selected.append(rule)
    return sorted(selected, key=lambda item: str(item.get("id")))


def _implicit_default_rules(policy: Dict[str, Any], operation: str, environment: Optional[str]) -> List[Dict[str, Any]]:
    defaults = policy.get("defaults")
    if not isinstance(defaults, dict):
        return []
    if environment != "production" or not _is_mutating_operation(operation):
        return []

    rules: List[Dict[str, Any]] = []
    if defaults.get("production_requires_plan") is True:
        rules.append(
            {
                "id": "default-production-requires-plan",
                "operation": operation,
                "environment": environment,
                "severity": "blocker",
                "require": {"plan_exists": True},
                "description": "Production mutating operations require a plan.",
            }
        )
    if defaults.get("production_requires_confirmation") is True:
        rules.append(
            {
                "id": "default-production-requires-confirmation",
                "operation": operation,
                "environment": environment,
                "severity": "blocker",
                "require": {"confirmation_required": True},
                "description": "Production mutating operations require operator confirmation.",
            }
        )
    if defaults.get("require_json_receipts") is True:
        rules.append(
            {
                "id": "default-require-json-receipts",
                "operation": operation,
                "environment": environment,
                "severity": "blocker",
                "require": {"json_receipts": True},
                "description": "Production mutating operations require JSON receipts.",
            }
        )
    return rules


def _is_mutating_operation(operation: str) -> bool:
    return operation.endswith(".apply") or operation.endswith(".create")


def evaluate_policy(
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    context: Dict[str, Any],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate ``operation`` against the policy and a flat fact ``context``.

    Returns an ``ophelia.policy_result`` dict. ``status`` is ``blocked`` when any
    rule fails as a blocker (including any unknown required condition, which
    always fails closed), ``warn`` when only warnings remain, otherwise ``ok``.
    An operation with no matching rules is ``ok`` with an empty ``rules`` list:
    the policy intentionally does not assert anything about unlisted operations.

    ``app`` is recorded for traceability only and does not participate in rule
    matching; rules match on operation + environment.
    """
    if policy is None:
        policy = load_policy()
    context = dict(context or {})

    selected = _select_rules(policy, operation, environment)
    selected.extend(_implicit_default_rules(policy, operation, environment))
    selected = sorted(selected, key=lambda item: str(item.get("id")))
    rule_results: List[Dict[str, str]] = []
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    for rule in selected:
        rule_id = str(rule.get("id"))
        severity = rule.get("severity")
        if severity not in KNOWN_SEVERITIES:
            severity = "blocker"  # an invalid severity is itself unsafe; fail closed
        require = rule.get("require") if isinstance(rule.get("require"), dict) else {}

        unknown_conditions: List[str] = []
        failed_conditions: List[str] = []
        for condition_key, expected in require.items():
            evaluator = CONDITION_EVALUATORS.get(condition_key)
            if evaluator is None:
                unknown_conditions.append(condition_key)
                continue
            if not evaluator(expected, context):
                failed_conditions.append(condition_key)

        if unknown_conditions:
            # Fail closed: we cannot prove the safety property, so block.
            message = (
                f"Rule `{rule_id}` requires condition(s) the engine cannot evaluate: "
                f"{', '.join(sorted(unknown_conditions))}. Failing closed."
            )
            finding = issue("policy_unknown_required_condition", message, rule_id)
            blockers.append(finding)
            rule_results.append({"id": rule_id, "status": "blocked", "severity": "blocker", "message": message})
            continue

        if failed_conditions:
            message = (
                f"Rule `{rule_id}` not satisfied: "
                f"{', '.join(sorted(failed_conditions))}."
            )
            finding = issue(f"policy_{rule_id.replace('-', '_')}", message, rule_id)
            if severity == "blocker":
                blockers.append(finding)
                rule_results.append({"id": rule_id, "status": "blocked", "severity": "blocker", "message": message})
            elif severity == "warning":
                warnings.append(finding)
                rule_results.append({"id": rule_id, "status": "warn", "severity": "warning", "message": message})
            else:  # info: recorded, never blocks or warns the overall status
                rule_results.append({"id": rule_id, "status": "info", "severity": "info", "message": message})
            continue

        rule_results.append(
            {
                "id": rule_id,
                "status": "ok",
                "severity": str(severity),
                "message": f"Rule `{rule_id}` satisfied.",
            }
        )

    status = "blocked" if blockers else ("warn" if warnings else "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": POLICY_RESULT_KIND,
        "operation": operation,
        "app": app,
        "environment": environment,
        "status": status,
        "rules": rule_results,
        "blockers": blockers,
        "warnings": warnings,
        "summary": (
            f"Policy result for {operation}"
            + (f" ({environment})" if environment else "")
            + f": {status}, {len(selected)} matching rule(s)."
        ),
    }


# --------------------------------------------------------------------------- #
# Explanation
# --------------------------------------------------------------------------- #


def explain_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only dump of the resolved rules (no evaluation).

    Describes what each rule requires, for which operation/environment, and which
    required conditions the engine can actually evaluate. Useful for operators
    and agents to audit the active policy before running anything.
    """
    rules = policy.get("rules") if isinstance(policy.get("rules"), list) else []
    explained: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        require = rule.get("require") if isinstance(rule.get("require"), dict) else {}
        conditions = [
            {
                "condition": key,
                "expected": require[key],
                "evaluable": key in CONDITION_EVALUATORS,
            }
            for key in sorted(require)
        ]
        explained.append(
            {
                "id": rule.get("id"),
                "operation": rule.get("operation"),
                "environment": rule.get("environment"),
                "severity": rule.get("severity"),
                "description": rule.get("description"),
                "requires": conditions,
            }
        )
    explained.sort(key=lambda item: (str(item.get("operation")), str(item.get("id"))))
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": POLICY_EXPLANATION_KIND,
        "source": policy.get("_source"),
        "version": policy.get("version"),
        "defaults": policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {},
        "known_condition_keys": list(KNOWN_CONDITION_KEYS),
        "rules": explained,
        "summary": f"Resolved policy with {len(explained)} rule(s) from {policy.get('_source')}.",
    }


# --------------------------------------------------------------------------- #
# Best-effort plan integration
# --------------------------------------------------------------------------- #


def policy_check_entry(
    operation: str,
    app: Optional[str],
    environment: Optional[str],
    context: Dict[str, Any],
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    policy_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build a plan ``checks`` entry from a policy evaluation, best-effort.

    Never raises: a missing or broken policy file yields a non-blocking check
    with a warning rather than crashing the plan. The returned entry is purely
    additive and is appended to a plan's ``checks`` list; it does not contribute
    to the plan's top-level blockers/status.
    """
    try:
        policy = load_policy(policy_path, runtime_root=runtime_root)
        result = evaluate_policy(operation, app, environment, context, policy=policy)
        return {
            "name": "policy",
            "ok": result["status"] != "blocked",
            "kind": POLICY_RESULT_KIND,
            "result": result,
        }
    except Exception as exc:  # best-effort: a policy problem must not break a plan
        return {
            "name": "policy",
            "ok": True,
            "kind": POLICY_RESULT_KIND,
            "message": f"Policy evaluation skipped: {exc}",
            "result": None,
        }
