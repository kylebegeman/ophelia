from argparse import Namespace, _SubParsersAction

from ..remote import RemoteError, bootstrap_host


def register(subparsers: _SubParsersAction) -> None:
    parser = subparsers.add_parser("bootstrap-host", help="Prepare the remote host runtime root and networks")
    parser.add_argument("host", help="SSH target such as operator@example-host")
    parser.add_argument("--ssh-port", type=int, default=22022, help="SSH port")
    parser.add_argument(
        "--remote-ophelia-root",
        default="~/ophelia",
        help="Remote platform repo root",
    )
    parser.add_argument(
        "--runtime-root",
        default="~/ophelia-runtime",
        help="Remote runtime root",
    )
    parser.set_defaults(handler=run)


def run(args: Namespace) -> int:
    try:
        bootstrap_host(
            host=args.host,
            ssh_port=args.ssh_port,
            remote_ophelia_root=args.remote_ophelia_root,
            runtime_root=args.runtime_root,
        )
    except RemoteError as exc:
        print(f"Remote bootstrap failed: {exc}")
        return 1

    print(f"Bootstrapped {args.host}:{args.runtime_root}")
    return 0
