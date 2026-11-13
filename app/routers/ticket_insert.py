"""
Ticket Insert — manual bot_scheduler row creation for tickets that didn't
make it into the queue automatically (DB hiccups, upstream API misses, etc).

Mirrors the shape of the existing PHP insert paths (Voicemail, Check Port In,
Resubmit Port In) but is data-driven off `ticket_scenarios` so new
companies/scenarios/channels can be added with a DB row instead of a deploy.

RunnerName is operator-selected from a live `rpa_runner` dropdown; RunnerID
and ControlRoomIP are ALWAYS derived from that selection at submit time —
never stored in scenario config — since those are properties of the runner,
not the scenario.

Admin-only. Every successful insert is written to hub_audit_log.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.audit_service import write_audit
from app.db import mysql_execute, mysql_fetch_all
from app.deps import require_admin

router = APIRouter(prefix="/api/admin/ticket-insert", tags=["ticket-insert"])


# ─────────────────────────────────────────────────────────────────────────────
# Read endpoints — scenario tree + runner dropdown
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/scenarios")
def list_scenarios(_: dict = Depends(require_admin)) -> dict:
    """Returns the full company -> scenario -> channel -> form schema tree
    so the frontend can render cascading selects without hardcoding anything."""
    rows = mysql_fetch_all(
        "SELECT id, company, scenario_key, scenario_label, hardcoded_json, channels_json "
        "FROM ticket_scenarios WHERE is_active = 1 ORDER BY company, scenario_label"
    )

    companies: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        try:
            channels = json.loads(r["channels_json"])
        except (TypeError, ValueError):
            channels = {}
        companies.setdefault(r["company"], []).append({
            "id": r["id"],
            "scenario_key": r["scenario_key"],
            "scenario_label": r["scenario_label"],
            "channels": {
                ch: {"form_fields": cfg.get("form_fields", [])}
                for ch, cfg in channels.items()
            },
        })

    return {"companies": companies}


@router.get("/runners")
def list_runners(_: dict = Depends(require_admin)) -> dict:
    """Live runner list for the Runner dropdown. RunnerID and ControlRoomIP
    travel with each row so the frontend can show them, though the backend
    re-fetches authoritative values from this same table at submit time."""
    rows = mysql_fetch_all(
        "SELECT RunnerName, RunnerReadableName, RunnerID, ControlRoomIP, RunnerStatus "
        "FROM rpa_runner ORDER BY RunnerName"
    )
    return {"runners": rows}


# ─────────────────────────────────────────────────────────────────────────────
# Submit endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TicketInsertRequest(BaseModel):
    company: str
    scenario_key: str
    channel: str
    runner_name: str
    fields: dict[str, str] = {}


def _generate_schedule_id() -> str:
    # Mirrors PHP: rand(0,100) . '-' . time() . '-' . rand(0,100)
    return f"{random.randint(0, 100)}-{int(time.time())}-{random.randint(0, 100)}"


def _load_scenario(company: str, scenario_key: str) -> dict[str, Any]:
    rows = mysql_fetch_all(
        "SELECT scenario_label, hardcoded_json, channels_json FROM ticket_scenarios "
        "WHERE company = %s AND scenario_key = %s AND is_active = 1 LIMIT 1",
        (company, scenario_key),
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active scenario '{scenario_key}' for company '{company}'.",
        )
    row = rows[0]
    return {
        "label": row["scenario_label"],
        "hardcoded": json.loads(row["hardcoded_json"]),
        "channels": json.loads(row["channels_json"]),
    }


def _load_runner(runner_name: str) -> dict[str, Any]:
    rows = mysql_fetch_all(
        "SELECT RunnerName, RunnerID, ControlRoomIP FROM rpa_runner WHERE RunnerName = %s LIMIT 1",
        (runner_name,),
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Runner '{runner_name}' not found in rpa_runner.",
        )
    runner = rows[0]
    if not runner.get("RunnerID"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Runner '{runner_name}' has no RunnerID configured — cannot insert safely.",
        )
    if not runner.get("ControlRoomIP"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Runner '{runner_name}' has no ControlRoomIP configured — cannot insert safely.",
        )
    return runner


@router.post("/submit")
async def submit_ticket_insert(
    payload: TicketInsertRequest,
    admin: dict = Depends(require_admin),
) -> dict:
    scenario = _load_scenario(payload.company, payload.scenario_key)

    channel_cfg = scenario["channels"].get(payload.channel)
    if channel_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Channel '{payload.channel}' is not configured for scenario "
                   f"'{scenario['label']}'.",
        )

    # --- Validate required fields are present ---------------------------
    form_fields = channel_cfg.get("form_fields", [])
    missing = [
        f["key"] for f in form_fields
        if f.get("required") and not (payload.fields.get(f["key"]) or "").strip()
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field(s): {', '.join(missing)}",
        )

    # --- Resolve runner -> RunnerID / ControlRoomIP live -----------------
    runner = _load_runner(payload.runner_name)

    # --- Build Bot_Input strictly from this channel's declared key order -
    bot_input: dict[str, str] = {}
    for key in channel_cfg.get("bot_input_fields", []):
        if key == "sChannel":
            bot_input[key] = payload.channel
        else:
            bot_input[key] = (payload.fields.get(key) or "").strip()
    bot_input_json = json.dumps(bot_input)

    # --- Assemble the full bot_scheduler row ------------------------------
    hardcoded = scenario["hardcoded"]
    schedule_id = _generate_schedule_id()

    columns = [
        "ScheduleType", "GlobalStatus", "ScheduleID", "ScheduleName",
        "ScheduleDateTime", "ScheduleDateTimeE",
        "PlayOrder", "PlayID", "GroupID", "CompanyID", "BotID", "BotType",
        "ControlRoomIP", "RunnerName", "RunnerID", "Bot_Input",
        "DigitalWorkerType", "CreatedBy", "CreatedDateTime", "AssignedDateTime",
        "CompletionStatus", "RecordingRequired", "Intervention",
    ]

    values: dict[str, Any] = {
        "ScheduleType": hardcoded.get("ScheduleType", "Production"),
        "GlobalStatus": hardcoded.get("GlobalStatus", "RUN"),
        "ScheduleID": schedule_id,
        "ScheduleName": hardcoded.get("ScheduleName", scenario["label"]),
        "PlayOrder": hardcoded.get("PlayOrder", 1),
        "PlayID": hardcoded.get("PlayID"),
        "GroupID": hardcoded.get("GroupID"),
        "CompanyID": hardcoded.get("CompanyID"),
        "BotID": hardcoded.get("BotID"),
        "BotType": hardcoded.get("BotType", "Automation Anywhere"),
        "ControlRoomIP": runner["ControlRoomIP"],
        "RunnerName": runner["RunnerName"],
        "RunnerID": runner["RunnerID"],
        "Bot_Input": bot_input_json,
        "DigitalWorkerType": hardcoded.get("DigitalWorkerType", "Dedicated - On Premise"),
        "CreatedBy": admin.get("username", "system"),
        "CompletionStatus": hardcoded.get("CompletionStatus", "PENDING"),
        "RecordingRequired": hardcoded.get("RecordingRequired", "No"),
        "Intervention": hardcoded.get("Intervention", "No"),
    }

    placeholders = []
    params: list[Any] = []
    for col in columns:
        if col in ("ScheduleDateTime", "ScheduleDateTimeE", "CreatedDateTime", "AssignedDateTime"):
            placeholders.append("NOW()")
        else:
            placeholders.append("%s")
            params.append(values[col])

    sql = (
        f"INSERT INTO bot_scheduler ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )

    try:
        mysql_execute(sql, tuple(params))
    except Exception as e:
        write_audit(
            admin["username"],
            "ticket_insert.submit",
            resource_type="bot_scheduler",
            resource_id=schedule_id,
            success=False,
            detail={"company": payload.company, "scenario": payload.scenario_key,
                    "channel": payload.channel, "error": str(e)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database insert error: {e}",
        ) from e

    write_audit(
        admin["username"],
        "ticket_insert.submit",
        resource_type="bot_scheduler",
        resource_id=schedule_id,
        detail={
            "company": payload.company,
            "scenario": payload.scenario_key,
            "scenario_label": scenario["label"],
            "channel": payload.channel,
            "runner_name": runner["RunnerName"],
            "bot_input": bot_input,
        },
    )

    return {
        "status": "inserted",
        "schedule_id": schedule_id,
        "scenario_label": scenario["label"],
        "runner_name": runner["RunnerName"],
        "bot_input": bot_input,
    }
