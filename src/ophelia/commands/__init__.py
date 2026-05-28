from .app import register as register_app
from .apps import register as register_apps
from .actions import register as register_actions
from .api import register as register_api
from .backup import register as register_backup
from .bootstrap_host import register as register_bootstrap_host
from .caddy import register as register_caddy
from .deploy import register as register_deploy
from .diff import register as register_diff
from .doctor import register as register_doctor
from .drift import register as register_drift
from .explain import register as register_explain
from .gc import register as register_gc
from .host import register as register_host
from .inspect import register as register_inspect
from .jobs import register as register_jobs
from .list_apps import register as register_list
from .notes import register as register_notes
from .operations import register as register_operations
from .preflight import register as register_preflight
from .release import register as register_release
from .registry import register as register_registry
from .render import register as register_render
from .rollback import register as register_rollback
from .runtime import register as register_runtime
from .secrets import register as register_secrets
from .status import register as register_status
from .validate import register as register_validate
from .verify import register as register_verify


def register_commands(subparsers):
    register_bootstrap_host(subparsers)
    register_apps(subparsers)
    register_app(subparsers)
    register_caddy(subparsers)
    register_actions(subparsers)
    register_api(subparsers)
    register_jobs(subparsers)
    register_backup(subparsers)
    register_host(subparsers)
    register_registry(subparsers)
    register_preflight(subparsers)
    register_secrets(subparsers)
    register_runtime(subparsers)
    register_operations(subparsers)
    register_notes(subparsers)
    register_gc(subparsers)
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
