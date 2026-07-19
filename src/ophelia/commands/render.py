from argparse import Namespace, _SubParsersAction
import json
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..manifest_v2 import ManifestV2Error, load_manifest_v2
from ..manifest_v2_renderer import render_revision_bundle
from ..runtime import render_bundle, write_bundle
from ._output import print_error


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("render", help="Render a manifest into a runtime bundle")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write rendered output into",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        import yaml

        raw = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        is_v2 = isinstance(raw, dict) and raw.get("version") == 2
        manifest = load_manifest_v2(args.manifest) if is_v2 else load_manifest(args.manifest)
    except (ManifestError, ManifestV2Error, OSError, ValueError) as exc:
        print_error(f"Manifest invalid: {exc}", "manifest_invalid", json_output=args.json)
        return 1

    output_dir = args.output_dir or (Path.cwd() / "build" / manifest.app)
    if is_v2:
        revision = manifest.to_revision(created_at="1970-01-01T00:00:00Z")
        bundle = render_revision_bundle(manifest, revision)
    else:
        bundle = render_bundle(manifest)
    write_bundle(bundle, output_dir)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "app": manifest.app,
                    "output_dir": str(output_dir),
                    "generated_files": [str(path) for path in sorted(bundle)],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"Rendered {manifest.app} into {output_dir}")
    return 0
