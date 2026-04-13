from argparse import Namespace, _SubParsersAction
from pathlib import Path

from ..manifest import ManifestError, load_manifest
from ..runtime import render_bundle, write_bundle


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("render", help="Render a manifest into a runtime bundle")
    parser.add_argument("manifest", type=Path, help="Path to the .ophelia manifest")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory to write rendered output into",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"Manifest invalid: {exc}")
        return 1

    output_dir = args.output_dir or (Path.cwd() / "build" / manifest.app)
    write_bundle(render_bundle(manifest), output_dir)

    print(f"Rendered {manifest.app} into {output_dir}")
    return 0
