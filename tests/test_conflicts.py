from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.conflicts import scan_conflicts
from ophelia.manifest import load_manifest
from ophelia.portability import app_readiness_report
from ophelia.runtime import deploy_bundle


class ConflictTests(unittest.TestCase):
    def test_reports_no_conflicts_for_distinct_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.ophelia.yml").write_text(_manifest("one", "one.example.com", 3101))
            (root / "two.ophelia.yml").write_text(_manifest("two", "two.example.com", 3102))

            report = scan_conflicts(root)

            self.assertTrue(report["ok"])
            self.assertEqual([], report["conflicts"])
            self.assertEqual(2, report["manifest_count"])

    def test_reports_duplicate_domain_port_alias_and_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.ophelia.yml").write_text(_manifest("same", "same.example.com", 3101))
            (root / "two.ophelia.yml").write_text(_manifest("same", "same.example.com", 3101))

            report = scan_conflicts(root)

            self.assertFalse(report["ok"])
            conflict_types = {item["type"] for item in report["conflicts"]}
            self.assertIn("duplicate_app_id", conflict_types)
            self.assertIn("duplicate_domain", conflict_types)
            self.assertIn("duplicate_host_port", conflict_types)
            self.assertIn("duplicate_docker_alias", conflict_types)

    def test_runtime_lock_for_same_app_is_not_reported_as_route_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_path = root / "one.ophelia.yml"
            manifest_path.write_text(_manifest("one", "one.example.com", 3101))
            manifest = load_manifest(manifest_path)
            app_root = deploy_bundle(manifest, manifest_path, runtime_root)
            (app_root / "active_release.json").write_text((app_root / "release.json").read_text())

            report = scan_conflicts(root, runtime_root=runtime_root)

        self.assertTrue(report["ok"])
        self.assertEqual([], report["conflicts"])

    def test_two_active_apps_same_host_path_is_blocker_with_route_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            manifest_dir = root / "manifests"
            manifest_dir.mkdir()

            for app in ("alpha", "beta"):
                source = root / f"{app}.ophelia.yml"
                # Distinct apps, distinct host ports, but the SAME host + route.
                source.write_text(_manifest(app, "shared.example.com", 3201 if app == "alpha" else 3202))
                manifest = load_manifest(source)
                app_root = deploy_bundle(manifest, source, runtime_root)
                (app_root / "active_release.json").write_text((app_root / "release.json").read_text())

            # Scan an empty manifest dir so the two apps are only ACTIVE owners.
            report = scan_conflicts(manifest_dir, runtime_root=runtime_root)

        self.assertFalse(report["ok"])
        route_conflicts = [item for item in report["conflicts"] if item["type"] in {"duplicate_route", "duplicate_domain"}]
        self.assertTrue(route_conflicts, "expected a route/domain conflict for two active apps on the same host+path")
        conflict = route_conflicts[0]
        owners = conflict["owners"]
        self.assertGreaterEqual(len(owners), 2)
        for owner in owners:
            self.assertEqual(owner["route_source"], "active_runtime")
            self.assertIn("runtime_bundle_path", owner)
        # The blocker list must surface it.
        self.assertTrue(report["blockers"], "active route collision must populate blockers")
        blocker_codes = {item.get("code") for item in report["blockers"]}
        self.assertTrue(blocker_codes & {"duplicate_route", "duplicate_domain"})

    def test_current_manifests_have_verification_checks(self) -> None:
        manifest_dir = Path(__file__).resolve().parents[1] / "manifests"
        report = scan_conflicts(manifest_dir)

        warning_types = {item["type"] for item in report["warnings"]}
        self.assertNotIn("missing_verification_checks", warning_types)

    def test_conflicting_on_demand_tls_ask_endpoints_are_host_blockers_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one.ophelia.yml").write_text(
                _edge_manifest(
                    "one",
                    "one.example.com",
                    "https://control-one.example.com/allow?token=never-print-one",
                )
            )
            (root / "two.ophelia.yml").write_text(
                _edge_manifest(
                    "two",
                    "two.example.com",
                    "https://control-two.example.com/allow?token=never-print-two",
                )
            )

            report = scan_conflicts(root)
            with patch(
                "ophelia.portability.run_app_owned_verifications",
                return_value={"ok": True, "status": "ok", "count": 1, "checks": []},
            ):
                readiness = app_readiness_report(
                    "one",
                    "production",
                    root / "runtime",
                    root / "one.ophelia.yml",
                )

        conflicts = [item for item in report["conflicts"] if item["type"] == "conflicting_on_demand_tls_ask"]
        self.assertEqual(1, len(conflicts))
        self.assertEqual("on_demand_tls.ask", conflicts[0]["host_resource"])
        self.assertEqual(2, len(conflicts[0]["owners"]))
        self.assertIn("<redacted>", str(conflicts[0]["ask_endpoints"]))
        self.assertNotIn("never-print-one", str(report))
        self.assertNotIn("never-print-two", str(report))
        self.assertIn("conflicting_on_demand_tls_ask", {item["code"] for item in report["blockers"]})
        self.assertIn("edge_on_demand_tls_conflict", {item["code"] for item in readiness["blockers"]})

    def test_shared_ask_endpoint_is_allowed_but_only_one_app_may_own_catch_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ask = "https://control.example.com/allow"
            (root / "one.ophelia.yml").write_text(_edge_manifest("one", "one.example.com", ask))
            (root / "two.ophelia.yml").write_text(_edge_manifest("two", "two.example.com", ask))

            shared_ask_report = scan_conflicts(root)
            self.assertNotIn(
                "conflicting_on_demand_tls_ask",
                {item["type"] for item in shared_ask_report["conflicts"]},
            )

            (root / "one.ophelia.yml").write_text(_edge_manifest("one", "one.example.com", ask, catch_all=True))
            (root / "two.ophelia.yml").write_text(_edge_manifest("two", "two.example.com", ask, catch_all=True))
            catch_all_report = scan_conflicts(root)

        conflicts = [item for item in catch_all_report["conflicts"] if item["type"] == "duplicate_catch_all_edge"]
        self.assertEqual(1, len(conflicts))
        self.assertEqual("edge.catch_all", conflicts[0]["host_resource"])
        self.assertEqual({"one", "two"}, {owner["app"] for owner in conflicts[0]["owners"]})


def _manifest(app: str, domain: str, host_port: int) -> str:
    return f"""
version: 1
app: {app}
kind: service
image: ghcr.io/example/{app}:latest
services:
  web:
    port: 3000
    host_port: {host_port}
routes:
  - domain: {domain}
    service: web
verify:
  - name: health
    url: https://{domain}/health
""".strip() + "\n"


def _edge_manifest(app: str, domain: str, ask: str, catch_all: bool = False) -> str:
    catch_all_block = """
  catch_all:
    service: web
""" if catch_all else ""
    return f"""
version: 1
app: {app}
environment: production
kind: service
image: ghcr.io/example/{app}@sha256:aaaaaaaa
services:
  web:
    port: 3000
routes:
  - domain: {domain}
    service: web
edge:
  on_demand_tls:
    ask: {ask}
{catch_all_block.rstrip()}
verify:
  - name: health
    url: https://{domain}/health
""".strip() + "\n"


if __name__ == "__main__":
    unittest.main()
