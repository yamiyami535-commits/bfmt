from __future__ import annotations

import time
from enum import Enum
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.cr_client import (
    activity_row_summary,
    activity_status,
    cr_deploy_automation,
    cr_get_token_for_ip,
    device_connection_status,
    device_hostname,
    summarize_activity_row,
)
from app.cr_poll_worker import run_cr_poll_cycle
from app.cr_state import get_all_meta, get_snapshot
from app.audit_service import write_audit
from app.db import mysql_execute, mysql_fetch_all
from app.deps import require_admin, require_user_or_admin

router = APIRouter(prefix="/api/admin/bots", tags=["bots"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _activity_row_identity(a: dict) -> str:
    return str(a.get("id") or a.get("deploymentId") or "") or repr(id(a))


def _merge_activities_for_runner(
    runner: str,
    runner_id,
    by_act: dict[str, list[dict]],
    all_activities: list[dict],
) -> list[dict]:
    """Match CR activity rows by deviceName (hostname) and by userId (RunnerID)."""
    seen: set[str] = set()
    out: list[dict] = []

    def add(a: dict) -> None:
        key = _activity_row_identity(a)
        if key in seen:
            return
        seen.add(key)
        out.append(a)

    if runner:
        for a in by_act.get(runner.lower(), []) or []:
            add(a)

    rid = str(runner_id).strip() if runner_id not in (None, "") else ""
    if rid:
        for a in all_activities:
            if str(a.get("userId", "")).strip() == rid:
                add(a)

    def sort_key(x: dict) -> str:
        return str(x.get("startDateTime") or x.get("createdOn") or "")

    out.sort(key=sort_key, reverse=True)
    return out


class BotStatus(str, Enum):
    DISABLE = "DISABLE"
    READY = "READY"
    RESTART = "RESTART"


class BotStatusUpdate(BaseModel):
    status: BotStatus = Field(..., description="Desired TaskBotStatus to write into rpa_runner.TaskBotStatus")

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, v):
        if isinstance(v, str) and v.upper() == "ENABLE":
            return "READY"
        return v


def _restart_runner_service_winrm(host: str, command: str, settings) -> dict:
    try:
        import winrm
    except ImportError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PowerShell remoting requires the pywinrm package. Install pywinrm and restart the app.",
        ) from e

    if not settings.runner_restart_winrm_username or not settings.runner_restart_winrm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set RUNNER_RESTART_WINRM_USERNAME and RUNNER_RESTART_WINRM_PASSWORD in .env for PowerShell remoting.",
        )

    try:
        session = winrm.Session(host, auth=(settings.runner_restart_winrm_username, settings.runner_restart_winrm_password), transport=settings.runner_restart_winrm_transport)
        result = session.run_ps(command)
        return {
            "host": host,
            "command": command,
            "status_code": result.status_code,
            "ok": result.status_code == 0,
            "stdout": result.std_out.decode("utf-8", errors="replace"),
            "stderr": result.std_err.decode("utf-8", errors="replace"),
        }
    except Exception as e:
        return {"host": host, "command": command, "ok": False, "error": str(e)}


def _restart_runner_service(runner_name: str, service_name: str) -> dict:
    settings = get_settings()
    tpl = settings.runner_restart_url_template
    if tpl:
        try:
            if "{service}" in tpl:
                url = tpl.format(runner_name=runner_name, service=quote(service_name, safe=""))
            else:
                url = tpl.format(runner_name=runner_name)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"RUNNER_RESTART_URL_TEMPLATE placeholders invalid. Use {{runner_name}} and optionally {{service}}. Error: {e}",
            ) from e
        try:
            rr = requests.post(url, timeout=30)
            return {"url": url, "status_code": rr.status_code, "ok": rr.ok, "body_preview": rr.text[:300] if rr.text else ""}
        except requests.RequestException as e:
            return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}

    if settings.runner_restart_winrm_host_template:
        host = settings.runner_restart_winrm_host_template.format(runner_name=runner_name)
        command = settings.runner_restart_winrm_command.format(service=service_name, runner_name=runner_name)
        return _restart_runner_service_winrm(host, command, settings)

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Set RUNNER_RESTART_URL_TEMPLATE in .env or configure RUNNER_RESTART_WINRM_HOST_TEMPLATE for PowerShell remoting.",
    )


def _trigger_bot_for_runner(runner_name: str, mark_restart: bool = False) -> dict:
    bot_rows = mysql_fetch_all(
        "SELECT RunnerName, ControlRoomIP, RunnerID, RestartTaskID FROM rpa_runner WHERE RunnerName = %s LIMIT 1",
        (runner_name,),
    )
    if not bot_rows:
        raise HTTPException(status_code=404, detail="Bot not found in rpa_runner")

    cr_ip = bot_rows[0]["ControlRoomIP"]
    runner_id = bot_rows[0]["RunnerID"]
    task_bot_id = bot_rows[0]["RestartTaskID"]

    if not cr_ip:
        raise HTTPException(status_code=400, detail="ControlRoomIP is empty for this bot")
    if runner_id in (None, ""):
        raise HTTPException(status_code=400, detail="RunnerID is empty for this bot")
    if task_bot_id in (None, ""):
        raise HTTPException(status_code=400, detail="RestartTaskID is empty for this bot")

    if mark_restart:
        mysql_execute(
            "UPDATE rpa_runner SET TaskBotStatus = 'RESTART', Update_Time = NOW() WHERE RunnerName = %s",
            (runner_name,),
        )

    settings = get_settings()
    restart_result = None
    if settings.runner_restart_url_template:
        default_svc = "Automation Anywhere Bot Agent"
        try:
            restart_result = _restart_runner_service(runner_name, default_svc)
        except HTTPException:
            raise
        except Exception as e:
            restart_result = {"ok": False, "error": str(e)}

    try:
        token = cr_get_token_for_ip(str(cr_ip).strip())
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    code, deploy_body = cr_deploy_automation(
        str(cr_ip).strip(),
        token,
        int(task_bot_id),
        [int(runner_id)],
    )
    if code < 200 or code >= 300:
        raise HTTPException(
            status_code=502,
            detail=f"Deploy failed (HTTP {code}): {str(deploy_body)[:800]}",
        )

    return {
        "status": "triggered",
        "runner_name": runner_name,
        "cr_ip": cr_ip,
        "task_bot_id": int(task_bot_id),
        "runner_id": int(runner_id),
        "restart_result": restart_result,
        "deploy_response": deploy_body,
    }


@router.get("")
def list_bots(_: dict = Depends(require_user_or_admin)) -> dict:
    rows = mysql_fetch_all(
        "SELECT RunnerName, RunnerReadableName, ControlRoomIP, RunnerStatus, TaskBotStatus, RunnerID, RestartTaskID, "
        "QC_Status, QC_Time, Update_Time "
        "FROM rpa_runner ORDER BY RunnerName"
    )
    return {"count": len(rows), "items": rows}


@router.get("/live")
def bots_live(_: dict = Depends(require_user_or_admin)) -> dict:
    """
    Merge MySQL rpa_runner row with last Control Room device snapshot (poller cache).
    Enable CR polling with CR_POLL_ENABLED=true in .env.
    """
    s = get_settings()
    rows = mysql_fetch_all(
        "SELECT RunnerName, RunnerReadableName, ControlRoomIP, RunnerStatus, TaskBotStatus, RunnerID, RestartTaskID, "
        "QC_Status, QC_Time, Update_Time "
        "FROM rpa_runner ORDER BY RunnerName"
    )
    # Latest scheduler result per runner is shown alongside live Control Room work.
    latest_rows = mysql_fetch_all(
        "SELECT id, RunnerName, ScheduleName, CompletionStatus, CompletionRemarks, CompletionDateTime "
        "FROM bot_scheduler WHERE id IN (SELECT MAX(id) FROM bot_scheduler GROUP BY RunnerName)"
    )
    history_by_runner = {
        str(r.get("RunnerName") or "").strip().lower(): r
        for r in latest_rows
        if str(r.get("RunnerName") or "").strip()
    }
    failed_runs = mysql_fetch_all(
        "SELECT id, RunnerName, ScheduleName, CompletionRemarks, CompletionDateTime "
        "FROM bot_scheduler WHERE CompletionStatus = 'RUN_FAILED' "
        "AND ScheduleDateTime >= DATE_SUB(NOW(), INTERVAL 2 HOUR) "
        "ORDER BY id DESC LIMIT 30"
    )

    now = time.time()
    items: list[dict] = []
    for row in rows:
        cr_ip = (row.get("ControlRoomIP") or "").strip()
        runner = (row.get("RunnerName") or "").strip()
        snap = get_snapshot(cr_ip) if cr_ip else None

        cr_block: dict | None = None
        if snap is not None:
            age = max(0.0, now - float(snap.get("updated_at") or 0))
            device_err = snap.get("device_error")
            activity_err = snap.get("activity_error")
            by_host = snap.get("devices_by_host") or {}
            dev = by_host.get(runner.lower()) if runner else None

            by_act = snap.get("activities_by_host") or {}
            all_activities = snap.get("activities") or []
            act_rows = _merge_activities_for_runner(runner, row.get("RunnerID"), by_act, all_activities)

            activity_counts = {"active": 0, "queued": 0, "pending": 0, "other": 0}
            activity_summaries_open: list[dict] = []
            for a in act_rows:
                st = activity_status(a)
                bucket = summarize_activity_row(st)
                if bucket == "terminal":
                    continue
                if bucket in activity_counts:
                    activity_counts[bucket] += 1

                activity_summaries_open.append(activity_row_summary(a))

            recent = activity_summaries_open[:8]
            last_any = activity_row_summary(act_rows[0]) if act_rows else None

            hint = ""
            if not activity_err:
                if len(all_activities) == 0:
                    hint = "Control Room returned 0 activity rows for this poll."
                elif len(act_rows) == 0 and runner:
                    hint = "No rows matched this runner (try aligning CR deviceName + userId with RunnerName + RunnerID)."

            cr_block = {
                "cache_age_seconds": round(age, 1),
                "poll_error": device_err,
                "activity_poll_error": activity_err,
                "device_matched": dev is not None,
                "device_host": device_hostname(dev) if isinstance(dev, dict) else None,
                "device_status": device_connection_status(dev) if isinstance(dev, dict) else None,
                "activity_counts": activity_counts,
                "activities_recent": recent,
                "activity_cr_list_total": len(all_activities),
                "activity_matched_rows": len(act_rows),
                "activity_last": last_any,
                "activity_hint": hint,
            }

        items.append(
            {
                "mysql": row,
                "control_room": cr_block,
                "run_history": history_by_runner.get(runner.lower()),
            }
        )

    return {
        "cr_poll_enabled": s.cr_poll_enabled,
        "cr_poll_interval_seconds": s.cr_poll_interval_seconds,
        "cr_devices_list_path": s.cr_devices_list_path,
        "cr_activity_list_path": s.cr_activity_list_path,
        "cr_activity_page_length": s.cr_activity_page_length,
        "cr_base_scheme": s.cr_base_scheme,
        "snapshots_meta": get_all_meta(),
        "count": len(items),
        "failed_runs": failed_runs,
        "items": items,
    }


@router.post("/poll/cr-devices")
def manual_cr_device_poll(request: Request, admin: dict = Depends(require_user_or_admin)) -> dict:
    """Run one Control Room poll cycle immediately (devices list + activity list for each CR IP)."""
    run_cr_poll_cycle()
    from app.cr_state import get_all_meta

    meta = get_all_meta()
    write_audit(
        admin["username"],
        "cr.poll_manual",
        resource_type="control_room",
        resource_id="all",
        detail={"snapshots": meta},
        client_ip=_client_ip(request),
    )
    return {"status": "ok", "snapshots": meta}


@router.put("/{runner_name}/status")
def set_bot_status(
    runner_name: str,
    body: BotStatusUpdate,
    request: Request,
    admin: dict = Depends(require_user_or_admin),
) -> dict:
    changed = mysql_execute(
        "UPDATE rpa_runner SET TaskBotStatus = %s WHERE RunnerName = %s",
        (body.status.value, runner_name),
    )
    if changed == 0:
        write_audit(
            admin["username"],
            "bot.status",
            resource_type="runner",
            resource_id=runner_name,
            success=False,
            detail={"error": "not_found", "wanted": body.status.value},
            client_ip=_client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bot not found (RunnerName did not match any row)",
        )
    write_audit(
        admin["username"],
        "bot.status",
        resource_type="runner",
        resource_id=runner_name,
        detail={"status": body.status.value},
        client_ip=_client_ip(request),
    )
    return {"runner_name": runner_name, "taskbot_status": body.status.value, "updated_rows": changed}


@router.post("/{runner_name}/trigger")
def trigger_bot(runner_name: str, request: Request, admin: dict = Depends(require_user_or_admin)) -> dict:
    ip = _client_ip(request)
    try:
        r = _trigger_bot_for_runner(runner_name, mark_restart=False)
        write_audit(
            admin["username"],
            "bot.trigger",
            resource_type="runner",
            resource_id=runner_name,
            detail=r,
            client_ip=ip,
        )
        return r
    except HTTPException as e:
        try:
            write_audit(
                admin["username"],
                "bot.trigger",
                resource_type="runner",
                resource_id=runner_name,
                success=False,
                detail={"error": str(e.detail)},
                client_ip=ip,
            )
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            write_audit(
                admin["username"],
                "bot.trigger",
                resource_type="runner",
                resource_id=runner_name,
                success=False,
                detail={"error": f"Unexpected error: {str(e)}"},
                client_ip=ip,
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"Trigger failed: {str(e)[:200]}"
        )


@router.post("/{runner_name}/restart-service")
def restart_service_only(
    runner_name: str,
    request: Request,
    service: str = Query(default="Automation Anywhere Bot Agent"),
    admin: dict = Depends(require_user_or_admin),
) -> dict:
    """
    Calls RUNNER_RESTART_URL_TEMPLATE on the runner machine (local agent or reverse proxy).
    Does not trigger Control Room deploy unless you do that separately.
    """
    result = _restart_runner_service(runner_name, service)
    write_audit(
        admin["username"],
        "bot.restart_service",
        resource_type="runner",
        resource_id=runner_name,
        success=bool(result.get("ok")),
        detail={"service": service, "result": result},
        client_ip=_client_ip(request),
    )
    return {"runner_name": runner_name, "service": service, "result": result}


@router.post("/jobs/recover-down")
def run_recover_down_job(request: Request, admin: dict = Depends(require_admin)) -> dict:
    settings = get_settings()
    rows = mysql_fetch_all(
        "SELECT RunnerName FROM rpa_runner "
        "WHERE QC_Status = 'Down' AND TaskBotStatus <> 'DISABLE' "
        "ORDER BY RunnerName LIMIT %s",
        (settings.qc_recovery_batch_size,),
    )

    processed = []
    for row in rows:
        runner_name = row["RunnerName"]
        try:
            processed.append({"runner_name": runner_name, "ok": True, "result": _trigger_bot_for_runner(runner_name, mark_restart=True)})
        except HTTPException as e:
            processed.append({"runner_name": runner_name, "ok": False, "error": e.detail})
        except Exception as e:
            processed.append({"runner_name": runner_name, "ok": False, "error": str(e)})

    out = {"checked": len(rows), "results": processed}
    write_audit(
        admin["username"],
        "jobs.recover_down",
        resource_type="job",
        resource_id="recover-down",
        detail=out,
        client_ip=_client_ip(request),
    )
    return out
