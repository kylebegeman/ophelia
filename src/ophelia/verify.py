from __future__ import annotations

import errno
import json
import re
import shlex
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest, VerificationCheck, VerificationPolicy
from .redaction import deep_redact, redact_command_string, redact_url


TLS_PHASE = "certificate_obtain"
ROUTE_PHASE = "external_route_verify"
DEFAULT_BACKOFF = 1.2
MAX_INTERVAL = 30.0
APP_OWNED_PATHS = frozenset({"/ophelia/health", "/ophelia/release"})
APP_OWNED_COMMAND_MARKERS = frozenset(
    {"ophelia:health", "ophelia:data:verify", "ophelia:release"}
)


def verification_checks(manifest: Manifest) -> List[VerificationCheck]:
    if manifest.verify:
        return manifest.verify

    if manifest.profile == "console":
        domains = [route.domain for route in manifest.routes]
        if manifest.console and manifest.console.admin_domain:
            domains.insert(0, manifest.console.admin_domain)
        if domains:
            primary = domains[0]
            checks = [VerificationCheck(name="health", url=f"https://{primary}/health")]
            if manifest.console and manifest.console.surface == "root":
                checks.extend(
                    [
                        VerificationCheck(name="landing", url=f"https://{primary}/"),
                        VerificationCheck(name="console-fallback", url=f"https://{primary}/console"),
                    ]
                )
            else:
                checks.append(VerificationCheck(name="console", url=f"https://{primary}/console"))
            return checks

    return []


def app_owned_verification_checks(manifest: Manifest) -> List[VerificationCheck]:
    return [check for check in verification_checks(manifest) if _is_app_owned_check(check)]


def run_app_owned_verifications(
    manifest: Manifest,
    timeout: Optional[float] = None,
    attempts: Optional[int] = 1,
    delay: Optional[float] = None,
    interval: Optional[float] = 0,
    failure_mode: Optional[str] = None,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    checks = app_owned_verification_checks(manifest)
    if not checks:
        return _verification_payload(
            ok=True,
            phase="app_owned_verify",
            attempt=0,
            policy=effective_verification_policy(
                manifest,
                timeout=timeout,
                attempts=attempts,
                delay=delay,
                interval=interval,
                failure_mode=failure_mode,
            ),
            results=[],
            tls_readiness=[],
        )
    scoped_manifest = replace(manifest, verify=checks)
    payload = run_verifications(
        scoped_manifest,
        timeout=timeout,
        attempts=attempts,
        delay=delay,
        interval=interval,
        failure_mode=failure_mode,
        wait_for_tls=False,
        runtime_root=runtime_root,
    )
    payload["phase"] = "app_owned_verify"
    return payload


def run_verifications(
    manifest: Manifest,
    timeout: Optional[float] = None,
    attempts: Optional[int] = None,
    delay: Optional[float] = None,
    interval: Optional[float] = None,
    failure_mode: Optional[str] = None,
    wait_for_tls: bool = True,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
) -> Dict[str, Any]:
    checks = verification_checks(manifest)
    http_checks = [check for check in checks if check.type == "http"]
    internal_checks = [check for check in checks if check.type == "internal"]
    command_checks = [check for check in checks if check.type == "command"]
    policy = effective_verification_policy(
        manifest,
        timeout=timeout,
        attempts=attempts,
        delay=delay,
        interval=interval,
        failure_mode=failure_mode,
    )

    ssl_context = ssl.create_default_context()
    payload: Dict[str, Any] = _verification_payload(
        ok=True,
        phase=ROUTE_PHASE,
        attempt=0,
        policy=policy,
        results=[],
        tls_readiness=[],
    )

    if not checks:
        return payload

    for attempt in range(1, policy["attempts"] + 1):
        results: List[Dict[str, Any]] = []
        tls_readiness: List[Dict[str, Any]] = []
        phase = ROUTE_PHASE
        ok = True

        if wait_for_tls:
            tls_readiness = _check_tls_readiness(http_checks, timeout=policy["timeout"], ssl_context=ssl_context)
            failed_tls = [item for item in tls_readiness if not item["ok"]]
            if failed_tls:
                phase = TLS_PHASE
                results = _tls_results_for_checks(http_checks, tls_readiness)
                results.extend(
                    _run_internal_check(manifest, check, runtime_root=runtime_root, timeout=policy["timeout"])
                    for check in internal_checks
                )
                results.extend(
                    _run_command_check(manifest, check, runtime_root=runtime_root, timeout=policy["timeout"])
                    for check in command_checks
                )
                ok = False

        if ok:
            results = [
                _run_check(check, timeout=policy["timeout"], ssl_context=ssl_context)
                for check in http_checks
            ]
            results.extend(
                _run_internal_check(manifest, check, runtime_root=runtime_root, timeout=policy["timeout"])
                for check in internal_checks
            )
            results.extend(
                _run_command_check(manifest, check, runtime_root=runtime_root, timeout=policy["timeout"])
                for check in command_checks
            )
            ok = all(result["ok"] for result in results)

        payload = _verification_payload(
            ok=ok,
            phase=phase,
            attempt=attempt,
            policy=policy,
            results=results,
            tls_readiness=tls_readiness,
        )
        if ok or attempt == policy["attempts"]:
            return payload
        time.sleep(_attempt_interval(policy, attempt))

    return payload


def _is_app_owned_check(check: VerificationCheck) -> bool:
    if check.type == "internal" and check.path in APP_OWNED_PATHS:
        return True
    if check.type != "command":
        return False
    command_text = " ".join(check.command or [])
    return any(marker in command_text for marker in APP_OWNED_COMMAND_MARKERS)


def effective_verification_policy(
    manifest: Manifest,
    timeout: Optional[float] = None,
    attempts: Optional[int] = None,
    delay: Optional[float] = None,
    interval: Optional[float] = None,
    failure_mode: Optional[str] = None,
) -> Dict[str, Any]:
    base = getattr(manifest, "verify_policy", VerificationPolicy())
    resolved_interval = interval if interval is not None else delay
    resolved_failure_mode = failure_mode if failure_mode is not None else base.failure_mode
    if resolved_failure_mode not in {"hard", "warn"}:
        raise ValueError("verification failure_mode must be `hard` or `warn`.")
    resolved_attempts = int(attempts if attempts is not None else base.attempts)
    resolved_interval_value = float(resolved_interval if resolved_interval is not None else base.interval)
    resolved_timeout = float(timeout if timeout is not None else base.timeout)
    if resolved_attempts < 1:
        raise ValueError("verification attempts must be at least 1.")
    if resolved_interval_value < 0:
        raise ValueError("verification interval must be 0 or greater.")
    if resolved_timeout <= 0:
        raise ValueError("verification timeout must be greater than 0.")
    return {
        "attempts": resolved_attempts,
        "interval": resolved_interval_value,
        "timeout": resolved_timeout,
        "failure_mode": resolved_failure_mode,
        "backoff": DEFAULT_BACKOFF,
        "max_interval": MAX_INTERVAL,
    }


def verification_blocks_release(payload: Dict[str, Any]) -> bool:
    return not bool(payload.get("ok")) and payload.get("failure_mode", "hard") == "hard"


def _verification_payload(
    ok: bool,
    phase: str,
    attempt: int,
    policy: Dict[str, Any],
    results: List[Dict[str, Any]],
    tls_readiness: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if ok:
        status = "passed"
    elif policy["failure_mode"] == "warn":
        status = "warning"
    else:
        status = "failed"
    return {
        "ok": ok,
        "status": status,
        "blocking": status == "failed",
        "verified": ok,
        "phase": phase,
        "count": len(results),
        "results": results,
        "attempt": attempt,
        "attempts": policy["attempts"],
        "interval": policy["interval"],
        "timeout": policy["timeout"],
        "failure_mode": policy["failure_mode"],
        "policy": dict(policy),
        "tls_readiness": tls_readiness,
    }


def _run_check(check: VerificationCheck, timeout: float, ssl_context: ssl.SSLContext) -> Dict[str, Any]:
    if check.url is None:
        return {
            "name": check.name or "http",
            "type": check.type,
            "phase": ROUTE_PHASE,
            "ok": False,
            "error": "HTTP verification check is missing a URL.",
            "error_kind": "manifest_invalid",
        }
    display_url = redact_url(check.url)
    name = check.name or display_url
    request = urllib.request.Request(
        check.url,
        headers={"User-Agent": "ophelia-verify/1.0"},
        method=check.method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            matched = status == check.expect_status
            if matched and check.contains:
                matched = check.contains in body
            json_assertions = _json_assertions(body, check)
            if json_assertions and not all(item["ok"] for item in json_assertions):
                matched = False

            return {
                "name": name,
                "type": "http",
                "url": display_url,
                "method": check.method,
                "phase": ROUTE_PHASE,
                "status_code": status,
                "expected_status": check.expect_status,
                "contains": check.contains,
                "json_assertions": json_assertions,
                "ok": matched,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        matched = exc.code == check.expect_status
        if matched and check.contains:
            matched = check.contains in body
        json_assertions = _json_assertions(body, check)
        if json_assertions and not all(item["ok"] for item in json_assertions):
            matched = False
        result = {
            "name": name,
            "type": "http",
            "url": display_url,
            "method": check.method,
            "phase": ROUTE_PHASE,
            "status_code": exc.code,
            "expected_status": check.expect_status,
            "contains": check.contains,
            "json_assertions": json_assertions,
            "ok": matched,
        }
        if not matched:
            result["error"] = body[:240]
            result["error_kind"] = "http_status"
        return result
    except Exception as exc:  # pragma: no cover - network failures vary by environment.
        return {
            "name": name,
            "type": "http",
            "url": display_url,
            "method": check.method,
            "phase": ROUTE_PHASE,
            "status_code": None,
            "expected_status": check.expect_status,
            "contains": check.contains,
            "ok": False,
            "error": str(exc),
            "error_kind": _classify_error(exc),
        }


def _run_command_check(
    manifest: Manifest,
    check: VerificationCheck,
    runtime_root: Path,
    timeout: float,
) -> Dict[str, Any]:
    app_root = runtime_root / "apps" / manifest.app
    compose_path = app_root / "compose.yml"
    service = _resolved_service_name(manifest, check.service)
    command = list(check.command or [])
    name = check.name or f"{service}:command"
    display_command = redact_command_string(shlex.join(command))
    if not compose_path.exists():
        return {
            "name": name,
            "type": "command",
            "service": service,
            "phase": "internal_service_verify",
            "command": display_command,
            "ok": False,
            "error": f"Compose file not found: {compose_path}",
            "error_kind": "compose_missing",
        }
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "exec", "-T", service, *command],
            cwd=app_root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "type": "command",
            "service": service,
            "phase": "internal_service_verify",
            "command": display_command,
            "ok": False,
            "error": f"Command timed out after {timeout:g}s.",
            "error_kind": "timeout",
        }
    except OSError as exc:
        return {
            "name": name,
            "type": "command",
            "service": service,
            "phase": "internal_service_verify",
            "command": display_command,
            "ok": False,
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    exit_ok = result.returncode == (0 if check.expect_exit is None else check.expect_exit)
    contains_ok = True if check.contains is None else check.contains in stdout
    json_assertions = _json_assertions(stdout, check)
    json_ok = all(item["ok"] for item in json_assertions)
    return {
        "name": name,
        "type": "command",
        "service": service,
        "phase": "internal_service_verify",
        "command": display_command,
        "returncode": result.returncode,
        "expected_exit": 0 if check.expect_exit is None else check.expect_exit,
        "contains": check.contains,
        "json_assertions": json_assertions,
        "stdout_excerpt": _redacted_excerpt(stdout),
        "stderr_excerpt": _redacted_excerpt(stderr),
        "ok": exit_ok and contains_ok and json_ok,
    }


def _run_internal_check(
    manifest: Manifest,
    check: VerificationCheck,
    runtime_root: Path,
    timeout: float,
) -> Dict[str, Any]:
    app_root = runtime_root / "apps" / manifest.app
    compose_path = app_root / "compose.yml"
    service = _resolved_service_name(manifest, check.service)
    service_config = manifest.services.get(service)
    name = check.name or f"{service}:{check.path or '/'}"
    path = check.path or "/"
    method = check.method or "GET"
    if service_config is None:
        return {
            "name": name,
            "type": "internal",
            "service": check.service,
            "resolved_service": service,
            "path": path,
            "method": method,
            "phase": "internal_service_verify",
            "ok": False,
            "error": f"Service `{check.service}` could not be resolved.",
            "error_kind": "service_unknown",
        }
    if not compose_path.exists():
        return {
            "name": name,
            "type": "internal",
            "service": check.service,
            "resolved_service": service,
            "path": path,
            "method": method,
            "phase": "internal_service_verify",
            "ok": False,
            "error": f"Compose file not found: {compose_path}",
            "error_kind": "compose_missing",
        }
    script = (
        "url=\"http://127.0.0.1:$2$3\"; "
        "if command -v curl >/dev/null 2>&1; then "
        "curl -sS -X \"$1\" -w '\\n%{http_code}' \"$url\"; "
        "elif command -v node >/dev/null 2>&1; then "
        "node -e 'const http=require(\"http\");const method=process.argv[1];const port=process.argv[2];const path=process.argv[3];const req=http.request({host:\"127.0.0.1\",port:Number(port),path,method},res=>{let body=\"\";res.setEncoding(\"utf8\");res.on(\"data\",chunk=>body+=chunk);res.on(\"end\",()=>{process.stdout.write(body+\"\\n\"+res.statusCode);});});req.on(\"error\",err=>{console.error(err.message);process.exit(2);});req.end();' \"$1\" \"$2\" \"$3\"; "
        "elif [ \"$1\" = \"GET\" ] && command -v wget >/dev/null 2>&1; then "
        "headers=\"${TMPDIR:-/tmp}/ophelia-internal-check.$$\"; trap 'rm -f \"$headers\"' EXIT; "
        "set +e; body=$(wget -q -S -O - \"$url\" 2>\"$headers\"); wget_rc=$?; set -e; "
        "status=$(awk '/^  HTTP\\//{code=$2} END{print code}' \"$headers\"); "
        "printf '%s\\n%s' \"$body\" \"${status:-0}\"; "
        "if [ \"$wget_rc\" -ne 0 ] && [ \"${status:-0}\" = \"0\" ]; then exit \"$wget_rc\"; fi; "
        "else echo 'curl, node, or wget (GET only) is required for Ophelia internal service verification' >&2; exit 127; fi"
    )
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "exec",
                "-T",
                service,
                "sh",
                "-ec",
                script,
                "ophelia-internal-check",
                method,
                str(service_config.port),
                path,
            ],
            cwd=app_root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "type": "internal",
            "service": check.service,
            "resolved_service": service,
            "path": path,
            "method": method,
            "phase": "internal_service_verify",
            "ok": False,
            "error": f"Internal check timed out after {timeout:g}s.",
            "error_kind": "timeout",
        }
    except OSError as exc:
        return {
            "name": name,
            "type": "internal",
            "service": check.service,
            "resolved_service": service,
            "path": path,
            "method": method,
            "phase": "internal_service_verify",
            "ok": False,
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }

    body, status_code = _split_internal_response(result.stdout or "")
    json_assertions = _json_assertions(body, check)
    status_ok = status_code == check.expect_status
    contains_ok = True if check.contains is None else check.contains in body
    json_ok = all(item["ok"] for item in json_assertions)
    ok = result.returncode == 0 and status_ok and contains_ok and json_ok
    payload = {
        "name": name,
        "type": "internal",
        "service": check.service,
        "resolved_service": service,
        "path": path,
        "method": method,
        "phase": "internal_service_verify",
        "status_code": status_code,
        "expected_status": check.expect_status,
        "contains": check.contains,
        "json_assertions": json_assertions,
        "stdout_excerpt": _redacted_excerpt(body),
        "stderr_excerpt": _redacted_excerpt(result.stderr or ""),
        "ok": ok,
    }
    if result.returncode != 0:
        payload["error"] = "Internal service check command failed."
        payload["error_kind"] = "internal_check_command_failed"
        payload["returncode"] = result.returncode
    return payload


def _json_assertions(body: str, check: VerificationCheck) -> List[Dict[str, Any]]:
    assertions = _normalized_json_assertions(check)
    if not assertions:
        return []
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return [
            {
                "path": "<json>",
                "ok": False,
                "expected": "valid JSON",
                "actual": "invalid JSON",
                "error": str(exc),
            }
        ]
    results: List[Dict[str, Any]] = []
    for assertion in assertions:
        result = _evaluate_json_assertion(payload, assertion)
        results.append(
            deep_redact(
                result,
                propagate=True,
            )
        )
    return results


def _normalized_json_assertions(check: VerificationCheck) -> List[Dict[str, Any]]:
    assertions: List[Dict[str, Any]] = []
    if check.expect_json:
        assertions.extend(_expect_json_assertions(check.expect_json))
    for index, raw in enumerate(check.json_assertions):
        assertions.append(_normalize_json_assertion(raw, index))
    return assertions


def _expect_json_assertions(expect_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "path": path,
            "kind": "equals",
            "expected": expected,
            "condition": "equals",
        }
        for path, expected in sorted(expect_json.items())
    ]


def _normalize_json_assertion(raw: Any, index: int) -> Dict[str, Any]:
    if isinstance(raw, str):
        return _assertion_from_string(raw)
    if not isinstance(raw, dict):
        return {
            "path": f"json_assertions[{index}]",
            "kind": "invalid",
            "condition": "valid assertion",
            "error": "JSON assertion must be a string or mapping.",
        }
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return {
            "path": f"json_assertions[{index}].path",
            "kind": "invalid",
            "condition": "valid path",
            "error": "JSON assertion mapping requires a non-empty `path`.",
        }
    if "equals" in raw:
        return {
            "path": path,
            "kind": "equals",
            "expected": raw.get("equals"),
            "condition": "equals",
        }
    if "present" in raw:
        return {
            "path": path,
            "kind": "present",
            "expected": bool(raw.get("present")),
            "condition": "present" if raw.get("present") is not False else "absent",
        }
    if "exists" in raw:
        return {
            "path": path,
            "kind": "present",
            "expected": bool(raw.get("exists")),
            "condition": "present" if raw.get("exists") is not False else "absent",
        }
    if raw.get("is_true") is True:
        return {"path": path, "kind": "boolean", "expected": True, "condition": "is true"}
    if raw.get("is_false") is True:
        return {"path": path, "kind": "boolean", "expected": False, "condition": "is false"}
    if "regex" in raw:
        regex = raw.get("regex")
        if not isinstance(regex, str) or not regex:
            return {
                "path": path,
                "kind": "invalid",
                "condition": "valid regex",
                "error": "`regex` must be a non-empty string.",
            }
        return {"path": path, "kind": "regex", "expected": regex, "condition": f"matches {regex}"}
    return {
        "path": path,
        "kind": "invalid",
        "condition": "supported assertion",
        "error": "JSON assertion mapping must include one of `equals`, `present`, `exists`, `is_true`, `is_false`, or `regex`.",
    }


def _assertion_from_string(expression: str) -> Dict[str, Any]:
    text = expression.strip()
    regex_match = re.match(r"^(.+?)\s*(?:=~|matches)\s*(.+)$", text)
    if regex_match:
        path = regex_match.group(1).strip()
        expected = _parse_literal(regex_match.group(2).strip())
        if not isinstance(expected, str):
            expected = str(expected)
        return {"path": path, "kind": "regex", "expected": expected, "condition": f"matches {expected}"}

    equals_match = re.match(r"^(.+?)\s*==\s*(.+)$", text)
    if equals_match:
        path = equals_match.group(1).strip()
        expected = _parse_literal(equals_match.group(2).strip())
        return {"path": path, "kind": "equals", "expected": expected, "condition": "equals"}

    present_match = re.match(r"^(.+?)\s+(?:exists|present)$", text, flags=re.IGNORECASE)
    if present_match:
        return {
            "path": present_match.group(1).strip(),
            "kind": "present",
            "expected": True,
            "condition": "present",
        }

    bool_match = re.match(r"^(.+?)\s+is\s+(true|false)$", text, flags=re.IGNORECASE)
    if bool_match:
        expected = bool_match.group(2).lower() == "true"
        return {
            "path": bool_match.group(1).strip(),
            "kind": "boolean",
            "expected": expected,
            "condition": "is true" if expected else "is false",
        }

    return {
        "path": text or "<empty>",
        "kind": "invalid",
        "condition": "supported assertion",
        "error": "Use `$.path == value`, `$.path exists`, `$.path is true`, or `$.path =~ \"regex\"`.",
    }


def _parse_literal(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip("\"'")


def _evaluate_json_assertion(payload: Any, assertion: Dict[str, Any]) -> Dict[str, Any]:
    path = str(assertion.get("path", ""))
    kind = assertion.get("kind")
    condition = str(assertion.get("condition", kind or "assertion"))
    if kind == "invalid":
        return {
            "path": path,
            "condition": condition,
            "ok": False,
            "message": assertion.get("error", "Invalid JSON assertion."),
        }

    present, actual = _json_path(payload, path)
    expected = assertion.get("expected")
    result: Dict[str, Any] = {
        "path": path,
        "condition": condition,
        "present": present,
        "ok": False,
    }
    if kind == "present":
        result["expected"] = bool(expected)
        result["ok"] = present is bool(expected)
        result["message"] = _assertion_message(path, result["ok"], condition)
        return result
    if not present:
        result["expected"] = expected
        result["actual"] = "<missing>"
        result["message"] = _assertion_message(path, False, condition)
        return result
    if kind == "equals":
        result["expected"] = expected
        result["actual"] = actual
        result["ok"] = actual == expected
        result["message"] = _assertion_message(path, result["ok"], condition)
        return result
    if kind == "boolean":
        result["expected"] = bool(expected)
        result["actual"] = actual
        result["ok"] = isinstance(actual, bool) and actual is bool(expected)
        result["message"] = _assertion_message(path, result["ok"], condition)
        return result
    if kind == "regex":
        result["expected"] = expected
        result["actual"] = actual
        result["ok"] = isinstance(actual, str) and bool(re.search(str(expected), actual))
        result["message"] = _assertion_message(path, result["ok"], condition)
        return result

    result["message"] = f"Unsupported JSON assertion `{kind}` for path `{path}`."
    return result


def _assertion_message(path: str, ok: bool, condition: str) -> str:
    if ok:
        return f"JSON path `{path}` satisfied `{condition}`."
    return f"JSON path `{path}` did not satisfy `{condition}`."


def _json_path(payload: Any, field_path: str) -> Tuple[bool, Any]:
    tokens = _json_path_tokens(field_path)
    if tokens is None:
        return False, None
    current = payload
    for part in tokens:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            current = current[part]
            continue
        return False, None
    return True, current


def _json_path_tokens(field_path: str) -> Optional[List[str | int]]:
    text = field_path.strip()
    if not text:
        return None
    if text == "$":
        return []
    if text.startswith("$."):
        text = text[2:]
    elif text.startswith("$["):
        text = text[1:]
    tokens: List[str | int] = []
    index = 0
    while index < len(text):
        if text[index] == ".":
            index += 1
            continue
        if text[index] == "[":
            close = text.find("]", index)
            if close == -1:
                return None
            raw_index = text[index + 1 : close].strip()
            if not raw_index.isdigit():
                return None
            tokens.append(int(raw_index))
            index = close + 1
            continue
        next_dot = text.find(".", index)
        next_bracket = text.find("[", index)
        stops = [pos for pos in (next_dot, next_bracket) if pos != -1]
        stop = min(stops) if stops else len(text)
        token = text[index:stop]
        if not token:
            return None
        tokens.append(token)
        index = stop
    return tokens


def _resolved_service_name(manifest: Manifest, service: Optional[str]) -> str:
    if service == "app" and len(manifest.services) == 1:
        return next(iter(manifest.services))
    if service:
        return service
    if len(manifest.services) == 1:
        return next(iter(manifest.services))
    return ""


def _split_internal_response(stdout: str) -> Tuple[str, int]:
    if not stdout:
        return "", 0
    stripped = stdout.rstrip("\n")
    if "\n" not in stripped:
        try:
            return "", int(stripped)
        except ValueError:
            return stdout, 0
    body, status = stripped.rsplit("\n", 1)
    try:
        return body, int(status)
    except ValueError:
        return stdout, 0


def _redacted_excerpt(value: str, limit: int = 240) -> str:
    if not value:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        redacted_json = json.dumps(deep_redact(parsed, propagate=True), sort_keys=True)
        return redacted_json.replace("\x00", "")[:limit]
    redacted = deep_redact({"value": value}, propagate=True).get("value", "")
    return str(redacted).replace("\x00", "")[:limit]


def _check_tls_readiness(
    checks: List[VerificationCheck],
    timeout: float,
    ssl_context: ssl.SSLContext,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for host, port in _https_hosts(checks):
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw_socket:
                with ssl_context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
                    certificate = tls_socket.getpeercert() or {}
            results.append(
                {
                    "host": host,
                    "port": port,
                    "phase": TLS_PHASE,
                    "ok": True,
                    "not_after": certificate.get("notAfter"),
                }
            )
        except Exception as exc:  # pragma: no cover - network failures vary by environment.
            results.append(
                {
                    "host": host,
                    "port": port,
                    "phase": TLS_PHASE,
                    "ok": False,
                    "error": str(exc),
                    "error_kind": _classify_error(exc),
                }
            )
    return results


def _https_hosts(checks: List[VerificationCheck]) -> List[Tuple[str, int]]:
    seen: Dict[Tuple[str, int], None] = {}
    for check in checks:
        if check.url is None:
            continue
        parsed = urllib.parse.urlparse(check.url)
        if parsed.scheme != "https" or parsed.hostname is None:
            continue
        seen[(parsed.hostname, parsed.port or 443)] = None
    return list(seen.keys())


def _tls_results_for_checks(
    checks: List[VerificationCheck],
    tls_readiness: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_host = {(str(item["host"]), int(item["port"])): item for item in tls_readiness}
    results: List[Dict[str, Any]] = []
    for check in checks:
        if check.url is None:
            continue
        parsed = urllib.parse.urlparse(check.url)
        display_url = redact_url(check.url)
        name = check.name or display_url
        if parsed.scheme != "https" or parsed.hostname is None:
            results.append(
                {
                    "name": name,
                    "type": "http",
                    "url": display_url,
                    "phase": TLS_PHASE,
                    "status_code": None,
                    "expected_status": check.expect_status,
                    "contains": check.contains,
                    "ok": True,
                    "skipped": True,
                }
            )
            continue
        tls_result = by_host.get((parsed.hostname, parsed.port or 443), {})
        result = {
            "name": name,
            "type": "http",
            "url": display_url,
            "phase": TLS_PHASE,
            "status_code": None,
            "expected_status": check.expect_status,
            "contains": check.contains,
            "ok": bool(tls_result.get("ok")),
        }
        if not result["ok"]:
            result["error"] = tls_result.get("error", "TLS certificate is not ready.")
            result["error_kind"] = tls_result.get("error_kind", "tls_handshake")
        results.append(result)
    return results


def _attempt_interval(policy: Dict[str, Any], attempt: int) -> float:
    return min(
        float(policy["max_interval"]),
        float(policy["interval"]) * (float(policy["backoff"]) ** max(0, attempt - 1)),
    )


def _classify_error(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if reason is not None and reason is not exc:
        if isinstance(reason, BaseException):
            return _classify_error(reason)
        return "network"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate"
    if isinstance(exc, ssl.SSLError):
        return "tls_handshake"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, OSError):
        if exc.errno == errno.ECONNREFUSED:
            return "connection_refused"
        if exc.errno in {errno.ECONNRESET, errno.EPIPE}:
            return "connection_reset"
        if exc.errno in {errno.EHOSTUNREACH, errno.ENETUNREACH}:
            return "network_unreachable"
    return exc.__class__.__name__
