from .actions import register as register_actions
from .api import register as register_api
from .backup import register as register_backup
from .bootstrap_host import register as register_bootstrap_host
from .deploy import register as register_deploy
from .diff import register as register_diff
from .doctor import register as register_doctor
from .drift import register as register_drift
from .explain import register as register_explain
from .inspect import register as register_inspect
from .jobs import register as register_jobs
from .list_apps import register as register_list
from .release import register as register_release
from .render import register as register_render
from .rollback import register as register_rollback
from .status import register as register_status
from .validate import register as register_validate
from .verify import register as register_verify


def register_commands(subparsers):
    register_bootstrap_host(subparsers)
    register_actions(subparsers)
    register_api(subparsers)
    register_jobs(subparsers)
    register_backup(subparsers)
    register_validate(subparsers)
    register_explain(subparsers)
    register_verify(subparsers)
    register_render(subparsers)
    register_deploy(subparsers)
    register_diff(subparsers)
    register_drift(subparsers)
    register_status(subparsers)
    register_doctor(subparsers)
    register_inspect(subparsers)
    register_list(subparsers)
    register_release(subparsers)
    register_rollback(subparsers)
