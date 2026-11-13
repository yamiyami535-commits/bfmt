from __future__ import annotations

import threading

from app.config import get_settings
from app.cr_client import (
    cr_get_token_for_ip,
    cr_list_activities,
    cr_list_devices,
    index_activities_by_hostname,
    index_devices_by_hostname,
)
from app.cr_state import set_snapshot
from app.db import mysql_fetch_all

_stop = threading.Event()
_thread: threading.Thread | None = None


def run_cr_poll_cycle() -> None:
    rows = mysql_fetch_all(
        "SELECT DISTINCT ControlRoomIP AS ip FROM rpa_runner WHERE ControlRoomIP IS NOT NULL AND TRIM(ControlRoomIP) <> ''"
    )
    ips = [str(r["ip"]).strip() for r in rows if r.get("ip")]
    for cr_ip in ips:
        devices_by_host: dict = {}
        activities: list = []
        activities_by_host: dict = {}
        device_error: str | None = None
        activity_error: str | None = None

        try:
            token = cr_get_token_for_ip(cr_ip)
        except Exception as e:
            set_snapshot(
                cr_ip,
                devices_by_host={},
                activities=[],
                activities_by_host={},
                device_error=f"{type(e).__name__}: {e}",
                activity_error=f"{type(e).__name__}: {e}",
            )
            continue

        try:
            devices = cr_list_devices(cr_ip, token)
            devices_by_host = index_devices_by_hostname(devices)
        except Exception as e:
            device_error = f"{type(e).__name__}: {e}"

        try:
            activities = cr_list_activities(cr_ip, token)
            activities_by_host = index_activities_by_hostname(activities)
        except Exception as e:
            activity_error = f"{type(e).__name__}: {e}"

        set_snapshot(
            cr_ip,
            devices_by_host=devices_by_host,
            activities=activities,
            activities_by_host=activities_by_host,
            device_error=device_error,
            activity_error=activity_error,
        )


def _loop() -> None:
    settings = get_settings()
    while not _stop.is_set():
        try:
            run_cr_poll_cycle()
        except Exception:
            pass
        _stop.wait(timeout=max(5, int(settings.cr_poll_interval_seconds)))


def start_cr_poll_worker() -> None:
    global _thread
    s = get_settings()
    if not s.cr_poll_enabled:
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="cr-device-poll-worker", daemon=True)
    _thread.start()


def stop_cr_poll_worker() -> None:
    _stop.set()
    global _thread
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None
