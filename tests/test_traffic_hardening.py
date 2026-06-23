from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ophelia.manifest import load_manifest
from ophelia.portability import (
    traffic_apply,
    traffic_plan,
    traffic_rollback_apply,
    traffic_rollback_plan,
    traffic_status,
)
from ophelia.runtime import deploy_bundle


@contextmanager
def _skip_docker_status():
    previous = os.environ.get("OPHELIA_SKIP_DOCKER_STATUS")
    os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OPHELIA_SKIP_DOCKER_STATUS", None)
        else:
            os.environ["OPHELIA_SKIP_DOCKER_STATUS"] = previous


@contextmanager
def _env(key: str, value: str):
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _static_manifest(static_root: Path, environment: str = "staging") -> str:
    return (
        f"""
version: 1
app: static-portable
environment: {environment}
kind: static
static_root: {static_root}
routes:
  - domain: static-portable.example.com
pack:
  portability: static
  owner: personal
verify:
  - name: health
    url: https://static-portable.example.com/health
""".strip()
        + "\n"
    )


def _prepare_app(root: Path, environment: str = "staging") -> Path:
    runtime_root = root / "runtime"
    static_root = root / "site"
    static_root.mkdir()
    (static_root / "index.html").write_text("<h1>Static</h1>\n")
    manifest_path = root / "static.ophelia.yml"
    manifest_path.write_text(_static_manifest(static_root, environment))
    manifest = load_manifest(manifest_path)
    app_root = deploy_bundle(manifest, manifest_path, runtime_root)
    (app_root / "active_release.json").write_text((app_root / "release.json").read_text())
    return manifest_path


_HEALTHY = {
    "url": "https://target.example.net/health",
    "executed": True,
    "ok": True,
    "status_code": 200,
    "expected_status": 200,
}


class TrafficHardeningTests(unittest.TestCase):
    def test_plan_is_read_only(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                active_release = runtime_root / "apps" / "static-portable" / "active_release.json"
                before = active_release.read_text()
                before_traffic_dirs = sorted(
                    (runtime_root / "apps" / "static-portable" / "traffic").glob("*")
                ) if (runtime_root / "apps" / "static-portable" / "traffic").exists() else []
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                )
                after = active_release.read_text()
                after_traffic_dirs = sorted(
                    (runtime_root / "apps" / "static-portable" / "traffic").glob("*")
                ) if (runtime_root / "apps" / "static-portable" / "traffic").exists() else []

        self.assertEqual("app.traffic.plan", plan["operation"])
        self.assertEqual(before, after)
        self.assertEqual(before_traffic_dirs, after_traffic_dirs)

    def test_apply_rejects_token_from_plan_with_different_target_origin(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                other_plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "other.example.net",  # different target origin
                    "staging",
                    runtime_root,
                    manifest_path,
                )
                stale_token = str(other_plan["confirmation_token"])
                receipt = traffic_apply(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",  # apply uses the real target origin
                    "staging",
                    runtime_root,
                    manifest_path,
                    confirm=stale_token,
                )

        self.assertEqual("blocked", receipt["status"])
        self.assertIn(
            "confirmation_token_mismatch",
            {item["code"] for item in receipt["blockers"]},
        )

    def test_apply_rejects_token_from_plan_with_different_provider_digest(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                dns_records_a = root / "dns-a.json"
                dns_records_b = root / "dns-b.json"
                config_a = root / "providers-a.json"
                config_b = root / "providers-b.json"
                config_a.write_text(
                    json.dumps({"dns": {"provider": "file", "record_file": str(dns_records_a), "allow_mutation": True}})
                )
                config_b.write_text(
                    json.dumps({"dns": {"provider": "file", "record_file": str(dns_records_b), "allow_mutation": True}})
                )
                plan_a = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="file",
                    provider_config=config_a,
                    execute_provider_mutation=True,
                )
                # Apply with the SAME args but a DIFFERENT provider config (different
                # digest) using config_a's token must be rejected.
                receipt = traffic_apply(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="file",
                    provider_config=config_b,
                    execute_provider_mutation=True,
                    confirm=str(plan_a["confirmation_token"]),
                )

        self.assertIn(
            "confirmation_token_mismatch",
            {item["code"] for item in receipt["blockers"]},
        )

    def test_blocked_provider_config_withholds_apply_token(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                provider_config = root / "providers.json"
                # Literal secret in the config is a hard provider-config blocker.
                provider_config.write_text(
                    json.dumps(
                        {
                            "dns": {
                                "provider": "cloudflare",
                                "zone_id": "zone-fixture",
                                "api_token": "super-secret-literal",
                                "allow_mutation": True,
                            }
                        }
                    )
                )
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="cloudflare",
                    provider_config=provider_config,
                    execute_provider_mutation=True,
                )

        self.assertIsNone(plan["confirmation_token"])
        self.assertTrue(plan["provider_config_blocked"])
        self.assertIn(
            "provider_config_validation_blocked",
            {item["code"] for item in plan["blockers"]},
        )
        # The literal secret value must never appear in the plan output.
        self.assertNotIn("super-secret-literal", json.dumps(plan))

    def test_production_apply_without_health_is_blocked_by_policy(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root, environment="production")
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "production",
                    runtime_root,
                    manifest_path,
                )

        self.assertIsNone(plan["confirmation_token"])
        self.assertIn("traffic_policy_blocked", {item["code"] for item in plan["blockers"]})

    def test_production_apply_with_health_and_rollback_proceeds(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root, environment="production")
                with mock.patch(
                    "ophelia.portability._run_target_health_check",
                    return_value=_HEALTHY,
                ):
                    plan = traffic_plan(
                        "static-portable",
                        "source-host",
                        "target-host",
                        "target.example.net",
                        "production",
                        runtime_root,
                        manifest_path,
                        target_health_url="https://target.example.net/health",
                        run_target_health=True,
                    )

        self.assertEqual([], plan["blockers"])
        self.assertIsNotNone(plan["confirmation_token"])
        self.assertTrue(plan["target_health_present"])

    def test_production_configured_but_unexecuted_health_url_does_not_satisfy_gate(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root, environment="production")
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "production",
                    runtime_root,
                    manifest_path,
                    target_health_url="https://target.example.net/health",
                    run_target_health=False,
                )

        self.assertFalse(plan["target_health_present"])
        self.assertIsNone(plan["confirmation_token"])
        self.assertIn("traffic_policy_blocked", {item["code"] for item in plan["blockers"]})

    def test_cloudflare_provider_never_stores_token_value(self) -> None:
        canary = "cf-canary-token-value-do-not-store"
        with _skip_docker_status(), _env("OPHELIA_TEST_CF_TOKEN", canary):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                provider_config = root / "providers.json"
                provider_config.write_text(
                    json.dumps(
                        {
                            "dns": {
                                "provider": "cloudflare",
                                "zone_id": "zone-fixture",
                                "api_token_env": "OPHELIA_TEST_CF_TOKEN",
                                "allow_mutation": True,
                                "base_url": "https://api.cloudflare.test/client/v4",
                            }
                        }
                    )
                )

                def fake_cloudflare(method, url, token, body=None, timeout=30):
                    self.assertEqual(canary, token)
                    if method == "GET":
                        return {
                            "success": True,
                            "result": [
                                {
                                    "id": "record-fixture",
                                    "type": "CNAME",
                                    "name": "static-portable.example.com",
                                    "content": "source.example.net",
                                    "ttl": 300,
                                    "proxied": False,
                                }
                            ],
                        }
                    return {
                        "success": True,
                        "result": {
                            "id": "record-fixture",
                            "type": body["type"],
                            "name": body["name"],
                            "content": body["content"],
                            "ttl": body["ttl"],
                            "proxied": body["proxied"],
                        },
                    }

                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="cloudflare",
                    provider_config=provider_config,
                    execute_provider_mutation=True,
                )
                with mock.patch("ophelia.portability._cloudflare_request", side_effect=fake_cloudflare):
                    receipt = traffic_apply(
                        "static-portable",
                        "source-host",
                        "target-host",
                        "target.example.net",
                        "staging",
                        runtime_root,
                        manifest_path,
                        dns_provider="cloudflare",
                        provider_config=provider_config,
                        execute_provider_mutation=True,
                        confirm=str(plan["confirmation_token"]),
                    )
                status = traffic_status(
                    "static-portable",
                    "staging",
                    runtime_root,
                    manifest_path,
                )

        # Plan stores only the env-ref NAME; never the token VALUE.
        self.assertIn("OPHELIA_TEST_CF_TOKEN", json.dumps(plan))
        self.assertNotIn(canary, json.dumps(plan))
        self.assertEqual("succeeded", receipt["status"])
        self.assertNotIn(canary, json.dumps(receipt))
        self.assertNotIn(canary, json.dumps(status))
        # The on-disk plan/receipt persisted under the traffic checkpoint is clean.
        traffic_path = Path(str(receipt["traffic_path"]))
        for path in traffic_path.rglob("*.json"):
            self.assertNotIn(canary, path.read_text())

    def test_rollback_blocks_unsafe_delete_unless_approved(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                dns_records = root / "dns-records.json"
                provider_config = root / "providers.json"
                # Start with NO existing record so the forward apply CREATES it.
                dns_records.write_text(json.dumps({"schema_version": 1, "records": {}}))
                provider_config.write_text(
                    json.dumps({"dns": {"provider": "file", "record_file": str(dns_records), "allow_mutation": True}})
                )
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="file",
                    provider_config=provider_config,
                    execute_provider_mutation=True,
                )
                forward = traffic_apply(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    dns_provider="file",
                    provider_config=provider_config,
                    execute_provider_mutation=True,
                    confirm=str(plan["confirmation_token"]),
                )
                receipt_id = str(forward["operation_id"])
                # Forward receipt records prior state: the created record has previous=None.
                mutation = forward["provider_mutations"][0]
                first_change = mutation["changes"][0]

                blocked = traffic_rollback_plan(
                    "static-portable",
                    receipt_id,
                    "staging",
                    runtime_root,
                )
                approved = traffic_rollback_plan(
                    "static-portable",
                    receipt_id,
                    "staging",
                    runtime_root,
                    approve_unsafe_delete=True,
                )
                approved_receipt = traffic_rollback_apply(
                    "static-portable",
                    receipt_id,
                    "staging",
                    runtime_root,
                    str(approved["confirmation_token"]),
                    approve_unsafe_delete=True,
                )
                final_records = json.loads(dns_records.read_text())["records"]

        self.assertEqual("succeeded", forward["status"])
        self.assertIsNone(first_change["previous"])  # forward apply CREATED the record
        # Default rollback refuses the unsafe delete and withholds the token.
        self.assertIn("rollback_unsafe_delete", {item["code"] for item in blocked["blockers"]})
        self.assertIsNone(blocked["confirmation_token"])
        # Approved rollback proceeds and deletes the created record.
        self.assertEqual([], approved["blockers"])
        self.assertEqual("succeeded", approved_receipt["status"])
        self.assertNotIn("static-portable.example.com", final_records)

    def test_traffic_status_works_without_credentials(self) -> None:
        with _skip_docker_status():
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                runtime_root = root / "runtime"
                manifest_path = _prepare_app(root)
                # No traffic operations performed yet; status must still parse.
                empty_status = traffic_status(
                    "static-portable",
                    "staging",
                    runtime_root,
                    manifest_path,
                )
                # A checkpoint apply (manual providers, no credentials) then status.
                plan = traffic_plan(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                )
                traffic_apply(
                    "static-portable",
                    "source-host",
                    "target-host",
                    "target.example.net",
                    "staging",
                    runtime_root,
                    manifest_path,
                    confirm=str(plan["confirmation_token"]),
                )
                status = traffic_status(
                    "static-portable",
                    "staging",
                    runtime_root,
                    manifest_path,
                )

        # Parseable JSON in both cases.
        json.loads(json.dumps(empty_status))
        json.loads(json.dumps(status))
        self.assertEqual("ophelia.traffic_status", status["kind"])
        self.assertFalse(status["credentials_required"])
        self.assertFalse(status["live_probe_performed"])
        self.assertIsNone(empty_status["latest_apply"])
        self.assertIsNotNone(status["latest_apply"])
        self.assertEqual("static-portable", status["app"])
        self.assertTrue(
            any(route.get("domain") == "static-portable.example.com" for route in status["route_ownership"])
        )


if __name__ == "__main__":
    unittest.main()
