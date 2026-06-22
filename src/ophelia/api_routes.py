from __future__ import annotations

from typing import List


# Path templates exposed by the local HTTP API. Keep this as the single
# inspectable route manifest for adapter discovery surfaces.
HTTP_ROUTE_PATTERNS: List[str] = [
    "/health",
    "/actions",
    "/commands",
    "/schema/manifest",
    "/host/inventory",
    "/registry/manifests",
    "/registry/releases",
    "/operations",
    "/plugins",
    "/workflows",
    "/workflows/<id>",
    "/state/status",
    "/state/summary",
    "/state/apps",
    "/state/receipts",
    "/state/routes",
    "/state/backups",
    "/jobs/<id>",
    "/jobs/<id>/events",
    "/lumen/capabilities",
    "/lumen/console-data",
    "/lumen/dashboard-data",
    "/lumen/action-descriptors",
    "/lumen/apps",
    "/lumen/apps/<app>/readiness",
    "/lumen/apps/<app>/timeline",
]
