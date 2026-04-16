from .bootstrap_host import register as register_bootstrap_host
from .deploy import register as register_deploy
from .list_apps import register as register_list
from .render import register as register_render
from .validate import register as register_validate
from .verify import register as register_verify


def register_commands(subparsers):
    register_bootstrap_host(subparsers)
    register_validate(subparsers)
    register_verify(subparsers)
    register_render(subparsers)
    register_deploy(subparsers)
    register_list(subparsers)
