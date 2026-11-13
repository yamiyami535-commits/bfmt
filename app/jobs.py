from __future__ import annotations

import threading
import time

from fastapi import HTTPException

from app.config import get_settings
from app.db import mysql_fetch_all
from app.routers.bots import _trigger_bot_for_runner

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def run_recovery_cycle() -> dict:
    settings = get_settings()
    rows = mysql_fetch_all(
        "SELECT RunnerName FROM rpa_runner "
        "WHERE QC_Status = 'Down' AND TaskBotStatus <> 'DISABLE' "
        "ORDER BY RunnerName LIMIT %s",
        (settings.qc_recovery_batch_size,),
    )
    ok = 0
    fail = 0
    errors: list[dict] = []
    for row in rows:
        name = row["RunnerName"]
        try:
            _trigger_bot_for_runner(name, mark_restart=True)
            ok += 1
        except HTTPException as e:
            fail += 1
            errors.append({"runner_name": name, "error": e.detail})
        except Exception as e:
            fail += 1
            errors.append({"runner_name": name, "error": str(e)})
    return {"checked": len(rows), "ok": ok, "failed": fail, "errors": errors}


def _worker_loop() -> None:
    settings = get_settings()
    while not _stop_event.is_set():
        try:
            run_recovery_cycle()
        except Exception:
            # Keep loop alive; errors are visible via manual endpoint.
            pass
        _stop_event.wait(timeout=max(5, settings.qc_recovery_interval_seconds))


def start_recovery_worker() -> None:
    global _thread
    settings = get_settings()
    if not settings.qc_recovery_enabled:
        return
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker_loop, name="qc-recovery-worker", daemon=True)
    _thread.start()


def stop_recovery_worker() -> None:
    _stop_event.set()
    global _thread
    if _thread and _thread.is_alive():
        _thread.join(timeout=3)
    _thread = None
