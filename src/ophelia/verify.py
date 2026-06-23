from __future__ import annotations

import errno
import json
import shlex
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DEFAULT_RUNTIME_ROOT
from .manifest import Manifest, VerificationCheck, VerificationPolicy
from .redaction import deep_redact, redact_command_string, redact_url


TLS_PHASE = "certificate_obtain"
ROUTE_PHASE = "external_route_verify"
DEFAULT_BACKOFF = 1.2
MAX_INTERVAL = 30.0


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
    request = urllib.request.Request(check.url, headers={"User-Agent": "ophelia-verify/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
            matched = status == check.expect_status
            if matched and check.contains:
                matched = check.contains in body
            json_assertions = _json_assertions(body, check.expect_json)
            if json_assertions and not all(item["ok"] for item in json_assertions):
                matched = False

            return {
                "name": name,
                "type": "http",
                "url": display_url,
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
        json_assertions = _json_assertions(body, check.expect_json)
        if json_assertions and not all(item["ok"] for item in json_assertions):
            matched = False
        result = {
            "name": name,
            "type": "http",
            "url": display_url,
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
    service = check.service or ""
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
    json_assertions = _json_assertions(stdout, check.expect_json)
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


def _json_assertions(body: str, expected: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not expected:
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
    assertions: List[Dict[str, Any]] = []
    for field_path, expected_value in sorted(expected.items()):
        found, actual = _json_path(payload, field_path)
        ok = found and actual == expected_value
        assertions.append(
            deep_redact(
                {
                    "path": field_path,
                    "ok": ok,
                    "expected": expected_value,
                    "actual": actual if found else None,
                    "present": found,
                },
                propagate=True,
            )
        )
    return assertions


def _json_path(payload: Any, field_path: str) -> Tuple[bool, Any]:
    current = payload
    for part in field_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return False, None
    return True, current


def _redacted_excerpt(value: str, limit: int = 240) -> str:
    if not value:
        return ""
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
