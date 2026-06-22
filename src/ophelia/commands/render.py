from argparse import Namespace, _SubParsersAction
import json
from pathlib import Path

from ..manifest import ManifestError, load_manifest
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
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print_error(f"Manifest invalid: {exc}", "manifest_invalid", json_output=args.json)
        return 1

    output_dir = args.output_dir or (Path.cwd() / "build" / manifest.app)
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
