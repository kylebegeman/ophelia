"""Bounded, durable host observations for operators and the control plane."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ..execution import SQLiteOperationJournal
from .config import DaemonConfig
from .identity import identity_status
from .recovery import latest_backup_status


def collect_host_observation(
    config: DaemonConfig,
    journal: SQLiteOperationJournal,
    *,
    active_apps: int,
) -> Dict[str, Any]:
    """Collect one non-secret health sample without invoking external processes."""

    disk = shutil.disk_usage(config.runtime_root)
    disk_used_percent = (
        0.0 if disk.total == 0 else round(disk.used * 100 / disk.total, 2)
    )
    identity = identity_status(config)
    backup = latest_backup_status(config)
    activity, agent = _database_activity(journal, config.host_id)
    reasons = []
    status = "ready"

    if disk_used_percent >= config.disk_critical_percent:
        status = "critical"
        reasons.append("runtime_disk_critical")
    elif disk_used_percent >= config.disk_warning_percent:
        status = "warning"
        reasons.append("runtime_disk_warning")

    if identity["state"] in {"expired", "invalid_metadata"}:
        status = "critical"
        reasons.append("host_identity_" + identity["state"])
    elif identity["state"] == "expiring" and status == "ready":
        status = "warning"
        reasons.append("host_identity_expiring")

    if config.recovery_backup_roots and config.recovery_age_recipient:
        if backup["status"] in {"missing", "stale"}:
            if status == "ready":
                status = "warning"
            reasons.append("host_backup_" + backup["status"])

    if agent == "revoked":
        status = "critical"
        reasons.append("control_plane_identity_revoked")
    elif config.agent_enabled and agent == "disconnected" and status == "ready":
        status = "warning"
        reasons.append("control_plane_disconnected")

    return {
        "schema_version": 1,
        "kind": "ophelia.host-observation",
        "host_id": config.host_id,
        "observed_at": _utc_now(),
        "status": status,
        "reasons": sorted(set(reasons)),
        "system": {
            "cpu_count": os.cpu_count(),
            "load_average": _load_average(),
            "memory": _memory_status(),
        },
        "storage": {
            "runtime_root": str(config.runtime_root),
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": disk_used_percent,
            "warning_percent": config.disk_warning_percent,
            "critical_percent": config.disk_critical_percent,
        },
        "activity": {**activity, "active_apps": active_apps},
        "identity": identity,
        "backup": backup,
        "agent_connection_state": agent,
    }


def _database_activity(
    journal: SQLiteOperationJournal, host_id: str
) -> tuple[Dict[str, Any], Optional[str]]:
    connection = journal._connect()
    try:
        operations = {
            row["state"]: int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM operations GROUP BY state"
            ).fetchall()
        }
        workloads = {
            row["state"]: int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM workload_runs GROUP BY state"
            ).fetchall()
        }
        agent_row = connection.execute(
            "SELECT connection_state FROM agent_state WHERE host_id = ?", (host_id,)
        ).fetchone()
    finally:
        connection.close()
        journal._repair_permissions()
    return (
        {
            "operations": operations,
            "workload_runs": workloads,
            "recoverable_operations": sum(
                operations.get(state, 0)
                for state in (
                    "accepted",
                    "planning",
                    "awaiting_approval",
                    "queued",
                    "executing",
                    "compensating",
                    "cancelling",
                )
            ),
            "active_workload_runs": sum(
                workloads.get(state, 0) for state in ("accepted", "running")
            ),
        },
        None if agent_row is None else str(agent_row["connection_state"]),
    )


def _load_average() -> Optional[Dict[str, float]]:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):
        return None
    return {
        "one_minute": round(one, 3),
        "five_minutes": round(five, 3),
        "fifteen_minutes": round(fifteen, 3),
    }


def _memory_status() -> Optional[Dict[str, Any]]:
    proc = Path("/proc/meminfo")
    if proc.is_file() and not proc.is_symlink():
        values: Dict[str, int] = {}
        try:
            for line in proc.read_text(encoding="utf-8").splitlines():
                key, raw = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw.strip().split()[0]) * 1024
        except (OSError, UnicodeError, ValueError, IndexError):
            values = {}
        if set(values) == {"MemTotal", "MemAvailable"}:
            return {
                "total_bytes": values["MemTotal"],
                "available_bytes": values["MemAvailable"],
            }
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        physical_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None
    return {"total_bytes": page_size * physical_pages, "available_bytes": None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
