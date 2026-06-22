from __future__ import annotations

import errno
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .manifest import Manifest, VerificationCheck, VerificationPolicy
from .redaction import redact_url


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
) -> Dict[str, Any]:
    checks = verification_checks(manifest)
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
            tls_readiness = _check_tls_readiness(checks, timeout=policy["timeout"], ssl_context=ssl_context)
            failed_tls = [item for item in tls_readiness if not item["ok"]]
            if failed_tls:
                phase = TLS_PHASE
                results = _tls_results_for_checks(checks, tls_readiness)
                ok = False

        if ok:
            results = [
                _run_check(check, timeout=policy["timeout"], ssl_context=ssl_context)
                for check in checks
            ]
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

            return {
                "name": name,
                "url": display_url,
                "phase": ROUTE_PHASE,
                "status_code": status,
                "expected_status": check.expect_status,
                "contains": check.contains,
                "ok": matched,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        matched = exc.code == check.expect_status
        if matched and check.contains:
            matched = check.contains in body
        result = {
            "name": name,
            "url": display_url,
            "phase": ROUTE_PHASE,
            "status_code": exc.code,
            "expected_status": check.expect_status,
            "contains": check.contains,
            "ok": matched,
        }
        if not matched:
            result["error"] = body[:240]
            result["error_kind"] = "http_status"
        return result
    except Exception as exc:  # pragma: no cover - network failures vary by environment.
        return {
            "name": name,
            "url": display_url,
            "phase": ROUTE_PHASE,
            "status_code": None,
            "expected_status": check.expect_status,
            "contains": check.contains,
            "ok": False,
            "error": str(exc),
            "error_kind": _classify_error(exc),
        }


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
        parsed = urllib.parse.urlparse(check.url)
        display_url = redact_url(check.url)
        name = check.name or display_url
        if parsed.scheme != "https" or parsed.hostname is None:
            results.append(
                {
                    "name": name,
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
