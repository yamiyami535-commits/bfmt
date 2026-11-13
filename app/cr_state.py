"""
Thread-safe cache of Control Room snapshots per CR_IP (devices + activity list).
Updated by the background poller; read by /api/admin/bots/live.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
# cr_ip -> snapshot dict
_snapshots: dict[str, dict[str, Any]] = {}


def set_snapshot(
    cr_ip: str,
    *,
    devices_by_host: dict[str, dict[str, Any]],
    activities: list[dict[str, Any]],
    activities_by_host: dict[str, list[dict[str, Any]]],
    device_error: str | None,
    activity_error: str | None,
) -> None:
    with _lock:
        _snapshots[cr_ip] = {
            "updated_at": time.time(),
            "devices_by_host": devices_by_host,
            "activities": activities,
            "activities_by_host": activities_by_host,
            "device_error": device_error,
            "activity_error": activity_error,
        }


def get_snapshot(cr_ip: str) -> dict[str, Any] | None:
    with _lock:
        row = _snapshots.get(cr_ip)
        return dict(row) if row else None


def get_all_meta() -> dict[str, Any]:
    with _lock:
        return {
            "control_rooms": list(_snapshots.keys()),
            "snapshots": {
                k: {
                    "updated_at": v.get("updated_at"),
                    "device_error": v.get("device_error"),
                    "activity_error": v.get("activity_error"),
                    "device_count": len(v.get("devices_by_host") or {}),
                    "activity_count": len(v.get("activities") or []),
                }
                for k, v in _snapshots.items()
            },
        }
