from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from ..operation_schema import error_envelope


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def print_error(message: str, code: str, *, json_output: bool) -> None:
    if json_output:
        print_json({"ok": False, **error_envelope(message, code)})
    else:
        print(message)


def print_issues(title: str, issues: Iterable[Any]) -> None:
    items = list(issues)
    if not items:
        return
    print(f"{title}:")
    for item in items:
        if isinstance(item, Mapping):
            print(f"  - {item.get('message') or item.get('code') or item}")
        else:
            print(f"  - {item}")
