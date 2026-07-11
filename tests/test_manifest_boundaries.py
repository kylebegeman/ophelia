from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from ophelia.manifest import ManifestError, load_manifest
from ophelia.validation import host_path_capability


REPO_ROOT = Path(__file__).resolve().parents[1]


def _service_manifest() -> dict:
    return {
        "version": 1,
        "app": "demo-app",
        "environment": "staging",
        "kind": "service",
        "image": "example/demo:latest",
        "services": {"web": {"port": 8080}},
        "routes": [{"domain": "demo.example.com", "service": "web"}],
    }


class ManifestBoundaryTests(unittest.TestCase):
    def test_unsupported_version_fails_before_semantic_and_source_parsing(self) -> None:
        raw = {
            "version": 2,
            "routes": "not-a-list",
            "env_files": ["/missing/host/path.env"],
        }

        with self.assertRaises(ManifestError) as raised:
            self._load(raw)

        self.assertEqual(
            "Unsupported manifest version `2`; only version 1 is supported.",
            str(raised.exception),
        )

    def test_canonicalizes_identifiers_domains_and_route_paths(self) -> None:
        raw = _service_manifest()
        raw["services"] = {"web-api": {"port": 8080}}
        raw["routes"] = [
            {
                "domain": "BÜCHER.Example",
                "service": "web-api",
                "path_prefix": "/caf%65",
                "strip_prefix": "/caf%65",
                "rewrite_prefix": "/v%31",
            }
        ]
        raw["data"] = {
            "volumes": [
                {
                    "name": "uploads-1",
                    "mount": "/data",
                    "service": "web-api",
                }
            ]
        }

        manifest = self._load(raw)
        route = manifest.routes[0]

        self.assertEqual("demo-app", manifest.app)
        self.assertEqual(["web-api"], list(manifest.services))
        self.assertEqual("uploads-1", manifest.data.volumes[0].name)
        self.assertEqual("xn--bcher-kva.example", route.domain)
        self.assertEqual("/cafe", route.path_prefix)
        self.assertEqual("/cafe", route.strip_prefix)
        self.assertEqual("/v1", route.rewrite_prefix)
        lock_route = manifest.to_lock_dict()["routes"][0]
        self.assertEqual("xn--bcher-kva.example", lock_route["domain"])
        self.assertEqual("/cafe", lock_route["path_prefix"])
        self.assertEqual("/cafe", lock_route["strip_prefix"])
        self.assertEqual("/v1", lock_route["rewrite_prefix"])
        self.assertIs(type(manifest.environment), str)
        self.assertEqual("staging", manifest.environment)

    def test_rejects_domain_boundary_attacks(self) -> None:
        for domain in (
            "xn--.example",
            "*.example.com",
            "example.com:443",
            "example.com\nrespond",
            "example.com{env.SECRET}",
        ):
            with self.subTest(domain=domain):
                raw = _service_manifest()
                raw["routes"][0]["domain"] = domain
                with self.assertRaises(ManifestError):
                    self._load(raw)

    def test_rejects_route_path_boundary_attacks(self) -> None:
        for path in (
            "/admin\nrespond",
            "/admin{env.SECRET}",
            "/admin%2fconsole",
            "/admin%5cconsole",
            "/admin%3fquery",
            "/admin%23fragment",
        ):
            with self.subTest(path=path):
                raw = _service_manifest()
                raw["routes"][0]["path"] = path
                with self.assertRaises(ManifestError):
                    self._load(raw)

    def test_rejects_invalid_app_service_and_data_volume_names(self) -> None:
        cases = []

        invalid_app = _service_manifest()
        invalid_app["app"] = "Bad_App"
        cases.append(invalid_app)

        invalid_service = _service_manifest()
        invalid_service["services"] = {"Web": {"port": 8080}}
        invalid_service["routes"][0]["service"] = "Web"
        cases.append(invalid_service)

        invalid_volume = _service_manifest()
        invalid_volume["data"] = {"volumes": [{"name": "bad_name"}]}
        cases.append(invalid_volume)

        invalid_reference = _service_manifest()
        invalid_reference["routes"][0]["service"] = "web\nrespond"
        cases.append(invalid_reference)

        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ManifestError):
                self._load(raw)

    def test_infers_nearest_repository_root_for_manifest_relative_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            manifests = repository / "manifests"
            manifests.mkdir(parents=True)
            (repository / ".git").mkdir()
            (repository / "shared.env").write_text("KEY=value\n")
            path = manifests / "app.ophelia.yml"
            raw = _service_manifest()
            raw["env_files"] = ["../shared.env"]
            path.write_text(json.dumps(raw))

            manifest = load_manifest(path)

        self.assertEqual(["../shared.env"], manifest.env_files)

    def test_standalone_manifest_falls_back_to_its_directory_as_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_dir = root / "standalone"
            manifest_dir.mkdir()
            (root / "outside.env").write_text("KEY=value\n")
            path = manifest_dir / "app.ophelia.yml"
            raw = _service_manifest()
            raw["env_files"] = ["../outside.env"]
            path.write_text(json.dumps(raw))

            with self.assertRaises(ManifestError) as raised:
                load_manifest(path)

        self.assertIn("outside its source root", str(raised.exception))

    def test_explicit_source_root_rejects_traversal_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repository = base / "repo"
            manifest_dir = repository / "manifests"
            manifest_dir.mkdir(parents=True)
            outside = base / "outside.env"
            outside.write_text("KEY=value\n")

            traversal_path = manifest_dir / "traversal.ophelia.yml"
            traversal = _service_manifest()
            traversal["env_files"] = ["../../outside.env"]
            traversal_path.write_text(json.dumps(traversal))
            with self.assertRaises(ManifestError):
                load_manifest(traversal_path, source_root=repository)

            link = repository / "linked.env"
            try:
                link.symlink_to(outside)
            except OSError as error:
                self.skipTest("symlinks unavailable: %s" % error)
            symlink_path = manifest_dir / "symlink.ophelia.yml"
            symlink = _service_manifest()
            symlink["env_files"] = ["../linked.env"]
            symlink_path.write_text(json.dumps(symlink))
            with self.assertRaises(ManifestError):
                load_manifest(symlink_path, source_root=repository)

    def test_absolute_env_and_mount_sources_require_host_path_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "host.env"
            source.write_text("KEY=value\n")
            mount = root / "assets"
            mount.mkdir()
            path = root / "app.ophelia.yml"
            raw = _service_manifest()
            raw["env_files"] = [str(source)]
            raw["services"]["web"]["mounts"] = [
                {"source": str(mount), "target": "/srv/assets", "bind": True}
            ]
            path.write_text(json.dumps(raw))

            with self.assertRaises(ManifestError) as raised:
                load_manifest(path)
            self.assertIn("HostPathCapability", str(raised.exception))

            allowed = root / "allowed"
            allowed.mkdir()
            with self.assertRaises(ManifestError) as denied:
                load_manifest(
                    path,
                    host_path_capability=host_path_capability([allowed]),
                )
            self.assertIn("outside the allowed host roots", str(denied.exception))

            manifest = load_manifest(
                path,
                host_path_capability=host_path_capability([root]),
            )

        self.assertEqual(str(source), manifest.env_files[0])
        self.assertEqual(str(mount), manifest.services["web"].mounts[0].source)

    def test_json_lock_reopening_skips_only_source_authority_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.lock.json"
            raw = _service_manifest()
            raw["env_files"] = ["/definitely/missing/ophelia.env"]
            path.write_text(json.dumps(raw))

            manifest = load_manifest(path)
            self.assertEqual(raw["env_files"], manifest.env_files)

            ordinary_json = Path(directory) / "app.json"
            ordinary_json.write_text(json.dumps(raw))
            with self.assertRaises(ManifestError) as source_error:
                load_manifest(ordinary_json)
            self.assertIn("HostPathCapability", str(source_error.exception))

            raw["routes"][0]["domain"] = "*.example.com"
            path.write_text(json.dumps(raw))
            with self.assertRaises(ManifestError):
                load_manifest(path)

    def test_unknown_keys_are_deterministic_immutable_warning_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            path = root / "app.ophelia.yml"
            raw = _service_manifest()
            raw["versoin"] = 1
            raw["services"]["web"]["privileged"] = True
            raw["services"]["web"]["mounts"] = [
                {
                    "source": "assets",
                    "target": "/srv/assets",
                    "propagation": "shared",
                }
            ]
            raw["routes"][0]["handle"] = "respond"
            raw["edge"] = {"tls": {"mode": "auto", "issuer": "internal"}}
            raw["data"] = {"future_contract": {"enabled": True}}
            path.write_text(json.dumps(raw))

            manifest = load_manifest(path)

        self.assertEqual(
            [
                "data.future_contract",
                "edge.tls.issuer",
                "routes[0].handle",
                "services.web.mounts[0].propagation",
                "services.web.privileged",
                "versoin",
            ],
            [diagnostic.field for diagnostic in manifest.diagnostics],
        )
        self.assertTrue(all(item.severity == "warning" for item in manifest.diagnostics))
        self.assertTrue(all(item.code == "unknown_key" for item in manifest.diagnostics))
        self.assertEqual({"enabled": True}, manifest.to_lock_dict()["data"]["future_contract"])
        self.assertNotIn("diagnostics", manifest.to_lock_dict())
        with self.assertRaises(FrozenInstanceError):
            manifest.diagnostics[0].code = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            manifest.diagnostics.append(manifest.diagnostics[0])  # type: ignore[attr-defined]

    def test_all_shipped_example_and_fixture_manifests_load(self) -> None:
        paths = []
        paths.extend(sorted((REPO_ROOT / "examples").glob("*.ophelia.yml")))
        paths.extend(sorted((REPO_ROOT / "manifests").glob("*.ophelia.yml")))
        paths.extend(
            sorted((REPO_ROOT / "fixtures" / "app-suite" / "manifests").glob("*.ophelia.yml"))
        )
        paths.extend(sorted((REPO_ROOT / "fixtures" / "adoption").glob("*/.ophelia.yml")))

        loaded = [load_manifest(path) for path in paths]

        self.assertEqual(len(paths), len(loaded))
        self.assertGreater(len(loaded), 10)

    def _load(self, raw: dict):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.ophelia.yml"
            path.write_text(json.dumps(copy.deepcopy(raw)))
            return load_manifest(path)


if __name__ == "__main__":
    unittest.main()
