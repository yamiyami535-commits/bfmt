"""
External Ticket Insert — for machine-to-machine callers (ticketing systems,
IVR platform, WhatsApp bot, etc.).

Differences from the admin dashboard endpoint (/api/admin/ticket-insert/submit):
  - Auth: static API key via X-API-Key header (no JWT / no human login needed)
  - Runner: auto-selected via rpa_skillset + rpa_runner availability + ASSIGNED
            load from bot_scheduler — caller never specifies a runner
  - Scenario config: pulled live from bot_playlist (PlayID → GroupID, BotID,
            ScheduleName, etc.) so it's always in sync with your real setup
  - Audit: every call is logged to hub_audit_log with actor = "api:<key_hint>"

API Key management:
  - Stored in hub_settings as  EXTERNAL_API_KEY
  - Set it once via:
      POST /api/admin/external-key/rotate   (admin JWT required)
  - Rotate it any time without redeploying

Runner selection logic:
  1. Find all runners in rpa_skillset for this PlayID
  2. Filter to those that are READY + AVAILABLE in rpa_runner
  3. Count current ASSIGNED rows in bot_scheduler per runner (same PlayID)
  4. Order by rpa_skillset.Priority ASC, then assigned_count ASC
  5. Pick the top result
"""

from __future__ import annotations

import json
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.audit_service import write_audit
from app.db import mysql_execute, mysql_fetch_all
from app.deps import require_admin
from app.settings_store import get_setting, set_setting

router = APIRouter(tags=["ticket-insert-external"])


# ─────────────────────────────────────────────────────────────────────────────
# API Key auth dependency
# ─────────────────────────────────────────────────────────────────────────────

def _get_stored_key() -> str | None:
    return get_setting("EXTERNAL_API_KEY")


def _get_key_expiry() -> datetime | None:
    """Returns the expiry datetime (UTC) of the current key, or None if no expiry set."""
    val = get_setting("EXTERNAL_API_KEY_EXPIRES_AT")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
    """
    Validates X-API-Key header against the stored key in hub_settings.
    Also checks optional expiry — if the key has a TTL set and it has passed,
    the key is rejected even if the value matches.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )
    stored = _get_stored_key()
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External API key not configured. Ask an admin to POST /api/admin/external-key/rotate.",
        )
    # Constant-time compare to prevent timing attacks
    if not secrets.compare_digest(x_api_key.strip(), stored.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    # Check TTL expiry if one was set
    expiry = _get_key_expiry()
    if expiry and datetime.now(timezone.utc) > expiry:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"API key expired at {expiry.isoformat()}. "
                   "Ask an admin to POST /api/admin/external-key/rotate to issue a new one.",
        )
    # Return a hint for audit logging (first 6 chars only — never log the full key)
    return x_api_key[:6] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# Admin: rotate / set the external API key
# ─────────────────────────────────────────────────────────────────────────────

class RotateKeyRequest(BaseModel):
    expires_in_days: int | None = None  # None = never expires


@router.post("/api/admin/external-key/rotate")
def rotate_external_api_key(
    body: RotateKeyRequest = RotateKeyRequest(),
    admin: dict = Depends(require_admin),
) -> dict:
    """
    Generates a new random API key and stores it in hub_settings.
    Returns the key ONCE — save it immediately, it cannot be retrieved again.
    Calling this again invalidates the previous key.

    Optional body:
      { "expires_in_days": 90 }   → key expires 90 days from now
      {}                           → key never expires (default)
    """
    new_key = secrets.token_urlsafe(36)  # ~48 chars URL-safe
    set_setting("EXTERNAL_API_KEY", new_key)

    expiry_iso: str | None = None
    if body.expires_in_days is not None and body.expires_in_days > 0:
        expiry_dt = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        expiry_iso = expiry_dt.isoformat()
        set_setting("EXTERNAL_API_KEY_EXPIRES_AT", expiry_iso)
    else:
        # Clear any previous expiry — this key never expires
        set_setting("EXTERNAL_API_KEY_EXPIRES_AT", None)

    write_audit(
        admin["username"],
        "external_api_key.rotate",
        resource_type="hub_settings",
        resource_id="EXTERNAL_API_KEY",
        detail={
            "hint": new_key[:6] + "…",
            "expires_at": expiry_iso or "never",
        },
    )
    return {
        "api_key": new_key,
        "expires_at": expiry_iso or "never",
        "warning": "Save this key now — it will not be shown again. "
                   "Pass it as the X-API-Key header on every external request.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Runner auto-selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_runner(play_id: int) -> tuple[dict[str, Any], bool]:
    """
    Picks the best runner for a given PlayID. Always assigns — never blocks.

    Selection order:
      Pass 1 — prefer READY + AVAILABLE runners:
               Priority ASC, then in-flight count ASC (PENDING + ASSIGNED per PlayID)
      Pass 2 — fallback if all busy: any runner in rpa_skillset, any status,
               least in-flight (PENDING + ASSIGNED) wins. Always assigns.

    Returns (runner_row, is_fallback).
      is_fallback=False → a free runner was found
      is_fallback=True  → all runners busy; least-loaded was picked anyway

    Raises 503 only if no runners are registered in rpa_skillset at all.

    Note: bot_scheduler.PlayID may be VARCHAR (PHP inserts it quoted) while
    rpa_skillset.PlayID is INT. CAST both sides to CHAR to prevent silent
    type-mismatch failures that make assigned_count always return 0.
    """
    # Pass 1: prefer genuinely available runners
    available = mysql_fetch_all(
        """
        SELECT
            rs.RunnerName,
            rs.RunnerID,
            rs.ControlRoomIP,
            rs.Priority,
            COUNT(bs.id) AS assigned_count
        FROM rpa_skillset rs
        INNER JOIN rpa_runner rr
            ON rr.RunnerName    = rs.RunnerName
           AND rr.TaskBotStatus = 'READY'
           AND rr.RunnerStatus  = 'AVAILABLE'
        LEFT JOIN bot_scheduler bs
            ON bs.RunnerName           = rs.RunnerName
           AND CAST(bs.PlayID AS CHAR) = CAST(rs.PlayID AS CHAR)
           AND bs.CompletionStatus     IN ('ASSIGNED', 'PENDING')
        WHERE rs.PlayID = %s
        GROUP BY rs.RunnerName, rs.RunnerID, rs.ControlRoomIP, rs.Priority
        ORDER BY rs.Priority ASC, assigned_count ASC
        LIMIT 1
        """,
        (play_id,),
    )
    if available:
        return available[0], False

    # Pass 2: fallback — all busy, pick least loaded anyway
    fallback = mysql_fetch_all(
        """
        SELECT
            rs.RunnerName,
            rs.RunnerID,
            rs.ControlRoomIP,
            rs.Priority,
            COUNT(bs.id) AS assigned_count
        FROM rpa_skillset rs
        INNER JOIN rpa_runner rr
            ON rr.RunnerName = rs.RunnerName
        LEFT JOIN bot_scheduler bs
            ON bs.RunnerName           = rs.RunnerName
           AND CAST(bs.PlayID AS CHAR) = CAST(rs.PlayID AS CHAR)
           AND bs.CompletionStatus     IN ('ASSIGNED', 'PENDING')
        WHERE rs.PlayID = %s
        GROUP BY rs.RunnerName, rs.RunnerID, rs.ControlRoomIP, rs.Priority
        ORDER BY assigned_count ASC, rs.Priority ASC
        LIMIT 1
        """,
        (play_id,),
    )
    if fallback:
        return fallback[0], True

    # No runners registered at all for this PlayID
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"No runners registered in rpa_skillset for PlayID={play_id}. "
               "Please add at least one runner for this scenario.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Playlist / scenario config lookup
# ─────────────────────────────────────────────────────────────────────────────

def _load_playlist(play_id: int) -> dict[str, Any]:
    """
    Pulls the hardcoded bot_scheduler fields from bot_playlist by PlayID.
    This keeps the external endpoint in sync with your real config
    automatically — no separate seed table needed.
    """
    rows = mysql_fetch_all(
        """
        SELECT ScheduleName, GroupID, CompanyID, BotID
        FROM bot_playlist
        WHERE ID = %s
        LIMIT 1
        """,
        (play_id,),
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PlayID={play_id} not found in bot_playlist.",
        )
    return rows[0]


# ─────────────────────────────────────────────────────────────────────────────
# Channel → PlayID mapping + Bot_Input field schema
#
# This maps (scenario_key, channel) → PlayID and the Bot_Input fields to build.
# Kept here as a small config dict since it's stable and not user-editable.
# If you add more scenarios later, add a row here.
# ─────────────────────────────────────────────────────────────────────────────

SCENARIO_CHANNEL_MAP: dict[tuple[str, str], dict[str, Any]] = {
    # (scenario_key, channel) → { play_id, play_order, bot_input_fields }
    ("voicemail", "Daisy"):    {"play_id": 76, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"]},
    ("voicemail", "WhatsApp"): {"play_id": 76, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"]},
    ("voicemail", "IVR"):      {"play_id": 76, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"]},

    ("check_port_in", "Daisy"):    {"play_id": 70, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco"]},
    ("check_port_in", "WhatsApp"): {"play_id": 70, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel"]},
    ("check_port_in", "IVR"):      {"play_id": 70, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel"]},

    ("resubmit_port_in", "Daisy"):    {"play_id": 71, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"]},
    ("resubmit_port_in", "WhatsApp"): {"play_id": 71, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"]},
    ("resubmit_port_in", "IVR"):      {"play_id": 71, "play_order": 1, "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"]},
}

# Required fields per scenario (fields the caller MUST provide)
REQUIRED_FIELDS: dict[str, list[str]] = {
    "voicemail":       ["sServiceNumber", "sTicketID", "sRequest", "sTelco"],
    "check_port_in":   ["sServiceNumber", "sTicketID"],
    "resubmit_port_in":["sServiceNumber", "sTicketID", "sCustomerID"],
}

# For Daisy channel check_port_in and voicemail, sTelco is also required
CHANNEL_EXTRA_REQUIRED: dict[tuple[str, str], list[str]] = {
    ("check_port_in", "Daisy"): ["sTelco"],
}


def _generate_schedule_id() -> str:
    return f"{random.randint(0, 100)}-{int(time.time())}-{random.randint(0, 100)}"


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────

class ExternalTicketRequest(BaseModel):
    company: str
    scenario_key: str
    channel: str
    fields: dict[str, str] = {}


class ExternalTicketResponse(BaseModel):
    status: str
    schedule_id: str
    scenario_label: str
    runner_name: str
    runner_id: str | int
    control_room_ip: str
    bot_input: dict[str, str]
    runner_note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Main endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/api/external/ticket-insert",
    response_model=ExternalTicketResponse,
    summary="External ticket insert (API key auth, auto runner selection)",
)
def external_ticket_insert(
    payload: ExternalTicketRequest,
    key_hint: str = Depends(require_api_key),
) -> ExternalTicketResponse:
    """
    Insert a bot_scheduler row from an external system.

    - Auth: X-API-Key header (set via POST /api/admin/external-key/rotate)
    - Runner is auto-selected — do NOT pass runner_name
    - Bot_Input is built from `fields` based on the scenario+channel config
    """

    # 1. Validate company (currently only CelcomDigi — extend SCENARIO_CHANNEL_MAP for others)
    if payload.company != "CelcomDigi":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown company '{payload.company}'. Currently supported: CelcomDigi",
        )

    # 2. Resolve scenario + channel → PlayID + field schema
    map_key = (payload.scenario_key, payload.channel)
    scenario_cfg = SCENARIO_CHANNEL_MAP.get(map_key)
    if scenario_cfg is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown combination scenario_key='{payload.scenario_key}' "
                   f"channel='{payload.channel}'. "
                   f"Valid scenarios: {list({k[0] for k in SCENARIO_CHANNEL_MAP})}. "
                   f"Valid channels: Daisy, WhatsApp, IVR.",
        )

    play_id = scenario_cfg["play_id"]

    # 3. Validate required fields
    required = list(REQUIRED_FIELDS.get(payload.scenario_key, []))
    extra = CHANNEL_EXTRA_REQUIRED.get(map_key, [])
    for f in extra:
        if f not in required:
            required.append(f)

    missing = [f for f in required if not (payload.fields.get(f) or "").strip()]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field(s): {', '.join(missing)}",
        )

    # 4. Auto-select runner via rpa_skillset availability + load
    runner, is_fallback = _select_runner(play_id)

    # 5. Pull scenario hardcoded fields from bot_playlist (always in sync)
    playlist = _load_playlist(play_id)

    # 6. Build Bot_Input strictly from declared field order
    bot_input: dict[str, str] = {}
    for key in scenario_cfg["bot_input_fields"]:
        if key == "sChannel":
            bot_input[key] = payload.channel
        else:
            bot_input[key] = (payload.fields.get(key) or "").strip()

    # Remove empty optional fields so the JSON stays clean
    bot_input = {k: v for k, v in bot_input.items() if v}
    # But always keep sChannel
    bot_input["sChannel"] = payload.channel

    # 7. Generate ScheduleID
    schedule_id = _generate_schedule_id()

    # 8. Insert into bot_scheduler
    sql = """
        INSERT INTO bot_scheduler (
            ScheduleType, GlobalStatus, ScheduleID, ScheduleName,
            ScheduleDateTime, ScheduleDateTimeE,
            PlayOrder, PlayID, GroupID, CompanyID, BotID, BotType,
            ControlRoomIP, RunnerName, RunnerID, Bot_Input,
            DigitalWorkerType, CreatedBy, CreatedDateTime, AssignedDateTime,
            CompletionStatus, RecordingRequired, Intervention
        ) VALUES (
            'Production', 'RUN', %s, %s,
            NOW(), NOW(),
            %s, %s, %s, %s, %s, 'Automation Anywhere',
            %s, %s, %s, %s,
            'Dedicated - On Premise', %s, NOW(), NOW(),
            'PENDING', 'No', 'No'
        )
    """
    params = (
        schedule_id,
        playlist["ScheduleName"],
        scenario_cfg["play_order"],
        play_id,
        playlist["GroupID"],
        playlist["CompanyID"],
        playlist["BotID"],
        runner["ControlRoomIP"],
        runner["RunnerName"],
        runner["RunnerID"],
        json.dumps(bot_input),
        f"api:{key_hint}",  # CreatedBy — identifies this as an API call, not a human
    )

    try:
        mysql_execute(sql, params)
    except Exception as e:
        write_audit(
            f"api:{key_hint}",
            "external_ticket_insert.submit",
            resource_type="bot_scheduler",
            resource_id=schedule_id,
            success=False,
            detail={
                "company": payload.company,
                "scenario": payload.scenario_key,
                "channel": payload.channel,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database insert error: {e}",
        ) from e

    # 9. Audit log — success
    write_audit(
        f"api:{key_hint}",
        "external_ticket_insert.submit",
        resource_type="bot_scheduler",
        resource_id=schedule_id,
        detail={
            "company": payload.company,
            "scenario": payload.scenario_key,
            "channel": payload.channel,
            "runner_name": runner["RunnerName"],
            "play_id": play_id,
            "bot_input": bot_input,
            "runner_fallback": is_fallback,
        },
    )

    return ExternalTicketResponse(
        status="inserted",
        schedule_id=schedule_id,
        scenario_label=playlist["ScheduleName"],
        runner_name=runner["RunnerName"],
        runner_id=runner["RunnerID"],
        control_room_ip=runner["ControlRoomIP"],
        bot_input=bot_input,
        runner_note=(
            f"All runners were busy — assigned to least-loaded runner {runner['RunnerName']}."
            if is_fallback else "Assigned to available runner."
        ),
    )
