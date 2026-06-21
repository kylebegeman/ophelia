from __future__ import annotations

import glob
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ophelia.manifest import ManifestError, load_manifest
from ophelia.schema_export import (
    SCHEMA_ID,
    manifest_json_schema,
    manifest_json_schema_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(glob.glob(str(REPO_ROOT / "examples" / "*.ophelia.yml")))


def _jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


def _load_yaml(path: str) -> dict:
    import yaml

    with open(path) as fh:
        return yaml.safe_load(fh)


class SchemaExportTests(unittest.TestCase):
    def test_schema_round_trips_through_json(self) -> None:
        schema = manifest_json_schema()
        self.assertEqual(json.loads(json.dumps(schema)), schema)

    def test_schema_text_matches_dict(self) -> None:
        text = manifest_json_schema_text()
        self.assertEqual(json.loads(text), manifest_json_schema())

    def test_schema_has_identity_keys(self) -> None:
        schema = manifest_json_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["$id"], SCHEMA_ID)
        self.assertEqual(schema["schema_version"], 1)
        self.assertEqual(schema["title"], "Ophelia Manifest")
        self.assertEqual(schema["type"], "object")

    def test_required_top_level(self) -> None:
        schema = manifest_json_schema()
        self.assertEqual(
            sorted(schema["required"]),
            sorted(["version", "app", "kind", "routes"]),
        )

    def test_enums_encoded(self) -> None:
        props = manifest_json_schema()["properties"]
        self.assertEqual(
            props["kind"]["enum"],
            ["service", "multi-service", "static", "tunnel", "redirect"],
        )
        self.assertEqual(props["environment"]["enum"], ["dev", "staging", "production"])
        self.assertEqual(props["redirect_status"]["enum"], [301, 302, 307, 308])
        self.assertEqual(props["pack"]["properties"]["portability"]["enum"], ["critical", "standard", "static"])
        self.assertEqual(props["networking"]["properties"]["edge"]["enum"], ["shared"])
        self.assertEqual(props["networking"]["properties"]["internal"]["enum"], ["shared", "per-app"])
        self.assertEqual(props["edge"]["properties"]["tls"]["properties"]["mode"]["enum"], ["auto", "internal", "custom"])
        self.assertEqual(props["prism"]["properties"]["surface"]["enum"], ["console", "quark"])
        self.assertEqual(props["verify_policy"]["properties"]["failure_mode"]["enum"], ["hard", "warn"])

    def test_additional_properties_matches_lenient_parser(self) -> None:
        # The parser silently ignores unknown top-level keys, so the schema must
        # not reject them.
        self.assertTrue(manifest_json_schema()["additionalProperties"])

    @unittest.skipUnless(_jsonschema_available(), "jsonschema not installed")
    def test_every_example_validates(self) -> None:
        import jsonschema

        schema = manifest_json_schema()
        validator = jsonschema.Draft202012Validator(schema)
        self.assertTrue(EXAMPLES, "no example manifests found")
        for path in EXAMPLES:
            instance = _load_yaml(path)
            errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
            self.assertEqual(
                errors,
                [],
                f"{path} failed schema validation: "
                + "; ".join(f"{list(e.path)}: {e.message}" for e in errors),
            )

    @unittest.skipUnless(_jsonschema_available(), "jsonschema not installed")
    def test_dragonwriter_critical_pack_validates(self) -> None:
        import jsonschema

        path = str(REPO_ROOT / "examples" / "dragonwriter.ophelia.yml")
        instance = _load_yaml(path)
        self.assertEqual(instance["pack"]["portability"], "critical")
        jsonschema.validate(instance, manifest_json_schema())

    @unittest.skipUnless(_jsonschema_available(), "jsonschema not installed")
    def test_unknown_extension_field_agrees_with_parser(self) -> None:
        import jsonschema

        instance = {
            "version": 1,
            "app": "schema-extension-test",
            "kind": "static",
            "static_root": "/srv/site",
            "routes": [{"domain": "example.com"}],
            "totally_unknown_extension_field": "tolerated",
            "nested_unknown": {"foo": "bar"},
        }

        # Schema must accept it.
        schema_accepts = True
        try:
            jsonschema.validate(instance, manifest_json_schema())
        except jsonschema.ValidationError:
            schema_accepts = False

        # Parser must accept it too (does not raise).
        parser_accepts = True
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "m.ophelia.yml"
            import yaml

            manifest_path.write_text(yaml.safe_dump(instance))
            try:
                load_manifest(manifest_path)
            except ManifestError:
                parser_accepts = False

        self.assertEqual(
            schema_accepts,
            parser_accepts,
            "schema and parser disagree on unknown extension fields",
        )
        self.assertTrue(parser_accepts, "expected parser to tolerate unknown fields")


if __name__ == "__main__":
    unittest.main()
