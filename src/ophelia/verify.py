from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List

from .manifest import Manifest, VerificationCheck


def verification_checks(manifest: Manifest) -> List[VerificationCheck]:
    if manifest.verify:
        return manifest.verify

    if manifest.profile == "prism":
        domains = [route.domain for route in manifest.routes]
        if manifest.prism and manifest.prism.admin_domain:
            domains.insert(0, manifest.prism.admin_domain)
        if domains:
            primary = domains[0]
            checks = [VerificationCheck(name="health", url=f"https://{primary}/health")]
            if manifest.prism and manifest.prism.surface == "quark":
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


def run_verifications(manifest: Manifest, timeout: int = 10) -> Dict[str, Any]:
    checks = verification_checks(manifest)
    results: List[Dict[str, Any]] = []
    ok = True

    ssl_context = ssl.create_default_context()

    for check in checks:
        name = check.name or check.url
        request = urllib.request.Request(check.url, headers={"User-Agent": "ophelia-verify/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = getattr(response, "status", 200)
                matched = status == check.expect_status
                if matched and check.contains:
                    matched = check.contains in body

                result = {
                    "name": name,
                    "url": check.url,
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
                "url": check.url,
                "status_code": exc.code,
                "expected_status": check.expect_status,
                "contains": check.contains,
                "ok": matched,
                "error": body[:240],
            }
        except Exception as exc:  # pragma: no cover - network failures vary by environment.
            result = {
                "name": name,
                "url": check.url,
                "status_code": None,
                "expected_status": check.expect_status,
                "contains": check.contains,
                "ok": False,
                "error": str(exc),
            }

        ok = ok and result["ok"]
        results.append(result)

    return {
        "ok": ok,
        "count": len(results),
        "results": results,
    }
