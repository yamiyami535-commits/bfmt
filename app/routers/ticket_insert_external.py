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
  4. Order by rpa_skillset.Priority ASC, then assigned_count ASC, then RAND() for load balancing
  5. Pick the top result
"""

from __future__ import annotations

import json
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

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
               Priority ASC, then in-flight count ASC, then RAND() to balance ties.
      Pass 2 — fallback if all busy: any runner in rpa_skillset, any status,
               least in-flight (PENDING + ASSIGNED) wins. Ties split randomly via RAND().

    Returns (runner_row, is_fallback).
      is_fallback=False → a free runner was found
      is_fallback=True  --> all runners busy; least-loaded was picked anyway
    """
    # Pass 1: prefer genuinely available runners (Added RAND() to balance instant spikes)
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
        ORDER BY rs.Priority ASC, assigned_count ASC, RAND() ASC
        LIMIT 1
        """,
        (play_id,),
    )
    if available:
        return available[0], False

    # Pass 2: fallback — all busy, pick least loaded anyway (Replaced Priority sorting with RAND() for ties)
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
        ORDER BY assigned_count ASC, RAND() ASC
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


def _load_cidb_scenario(scenario_key: str, channel: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rows = mysql_fetch_all(
        """
        SELECT scenario_label, hardcoded_json, channels_json
        FROM ticket_scenarios
        WHERE company = %s AND scenario_key = %s AND is_active = 1
        LIMIT 1
        """,
        ("CIDB", scenario_key),
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"CIDB scenario '{scenario_key}' is not configured in ticket_scenarios.",
        )

    try:
        hardcoded = json.loads(rows[0]["hardcoded_json"])
        channels = json.loads(rows[0]["channels_json"])
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CIDB scenario configuration is invalid.",
        ) from error

    channel_config = channels.get(channel)
    if not isinstance(channel_config, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Channel '{channel}' is not configured for CIDB scenario '{scenario_key}'.",
        )
    if not isinstance(hardcoded, dict) or not isinstance(channel_config.get("bot_input_fields"), list):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CIDB scenario configuration is missing scheduler or Bot_Input fields.",
        )
    return rows[0], hardcoded, channel_config


# ─────────────────────────────────────────────────────────────────────────────
# Channel → PlayID mapping + Bot_Input field schema
# ─────────────────────────────────────────────────────────────────────────────

SCENARIO_CHANNEL_MAP: dict[tuple[str, str], dict[str, Any]] = {
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

REQUIRED_FIELDS: dict[str, list[str]] = {
    "voicemail":       ["sServiceNumber", "sTicketID", "sRequest", "sTelco"],
    "check_port_in":   ["sServiceNumber", "sTicketID"],
    "resubmit_port_in":["sServiceNumber", "sTicketID", "sCustomerID"],
}

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
    fields: dict[str, str] = Field(default_factory=dict)


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
    """

    if payload.company not in {"CelcomDigi", "CIDB"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown company '{payload.company}'. Currently supported: CelcomDigi, CIDB",
        )

    map_key = (payload.scenario_key, payload.channel)
    if payload.company == "CIDB":
        if payload.scenario_key != "cidb_masterbot" or payload.channel != "Email":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CIDB supports only scenario_key='cidb_masterbot' and channel='Email'.",
            )
        scenario_row, hardcoded, scenario_cfg = _load_cidb_scenario(payload.scenario_key, payload.channel)
        try:
            play_id = int(hardcoded["PlayID"])
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CIDB scenario PlayID is missing or invalid.",
            ) from error
        if play_id <= 0 or any(not hardcoded.get(key) for key in ("ScheduleName", "GroupID", "CompanyID", "BotID")):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CIDB scenario scheduler configuration is incomplete.",
            )

        fields = payload.fields
        request_keys = [key for key in ("sRequst", "sRequest") if fields.get(key, "").strip()]
        if len(request_keys) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A CIDB request may contain only one request-action field.",
            )
        request_input_key = request_keys[0] if request_keys else None
        request_action = fields.get(request_input_key, "").strip() if request_input_key else ""
        cancellation_action = fields.get("sAction", "").strip()
        if request_action and cancellation_action:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A CIDB request may contain either a request-action field or sAction, not both.",
            )
        if request_action == "Pending Customer Attachment":
            required = ["sCRMID", "sEmail", "sCustomerName", "sLanguage", request_input_key]
        elif cancellation_action == "Email Cancellation":
            required = [
                "sCRMID", "sEmail", "sCustomerName", "sCustomerType",
                "sIdentificationNumber", "sLanguage", "sOCRStatus", "sAction",
            ]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CIDB action must be 'Pending Customer Attachment' in sRequst/sRequest or 'Email Cancellation' in sAction.",
            )
        if cancellation_action and fields.get("sCustomerType", "").strip().lower() not in {"individual", "company"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sCustomerType must be Individual or Company.",
            )
        if cancellation_action and fields.get("sOCRStatus", "").strip() != "MANUAL_REVIEW_APPROVED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email Cancellation requires sOCRStatus='MANUAL_REVIEW_APPROVED'.",
            )
        if fields.get("sLanguage", "").strip().lower() not in {"en", "ms"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sLanguage must be 'en' or 'ms'.",
            )
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", fields.get("sEmail", "").strip()) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sEmail must be a single valid email address.",
            )
        scenario_fields = scenario_cfg["bot_input_fields"]
        configured_request_keys = [key for key in ("sRequst", "sRequest") if key in scenario_fields]
        if len(configured_request_keys) > 1:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CIDB scenario must configure only one request-action Bot_Input key.",
            )
        configured_request_key = configured_request_keys[0] if configured_request_keys else None
        configured_required = [
            configured_request_key if field in {"sRequst", "sRequest"} else field
            for field in required
        ]
        missing_config = [
            field for field in [*configured_required, "sChannel"]
            if field is None or field not in scenario_fields
        ]
        if missing_config:
            missing_labels = [
                "sRequst or sRequest" if field is None else field
                for field in missing_config
            ]
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"CIDB Bot_Input configuration omits required field(s): {', '.join(missing_labels)}.",
            )
        try:
            play_order = int(hardcoded.get("PlayOrder", 1))
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CIDB scenario PlayOrder is invalid.",
            ) from error
        playlist = {
            "ScheduleName": hardcoded["ScheduleName"],
            "GroupID": hardcoded["GroupID"],
            "CompanyID": hardcoded["CompanyID"],
            "BotID": hardcoded["BotID"],
        }
        scenario_label = scenario_row["scenario_label"]
    else:
        scenario_cfg = SCENARIO_CHANNEL_MAP.get(map_key)
        if scenario_cfg is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown combination scenario_key='{payload.scenario_key}' "
                       f"channel='{payload.channel}'.",
            )
        play_id = scenario_cfg["play_id"]
        required = list(REQUIRED_FIELDS.get(payload.scenario_key, []))
        extra = CHANNEL_EXTRA_REQUIRED.get(map_key, [])
        for f in extra:
            if f not in required:
                required.append(f)
        playlist = _load_playlist(play_id)
        scenario_label = playlist["ScheduleName"]
        play_order = scenario_cfg["play_order"]

    missing = [f for f in required if not (payload.fields.get(f) or "").strip()]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field(s): {', '.join(missing)}",
        )

    runner, is_fallback = _select_runner(play_id)

    bot_input: dict[str, str] = {}
    # Build bot_input based on the configured fields. For CIDB we also ensure the
    # request-action field (sRequest) is included even if the DB schema does not
    # list it explicitly.
    for key in scenario_cfg["bot_input_fields"]:
        if key == "sChannel":
            bot_input[key] = payload.channel
        elif key in {"sRequest", "sRequst"} and payload.company == "CIDB":
            # Prefer the correct field name if present; fallback to the legacy name.
            bot_input[key] = (
                payload.fields.get("sRequest")
                or payload.fields.get("sRequst")
                or ""
            ).strip()
        else:
            bot_input[key] = (payload.fields.get(key) or "").strip()

    # Ensure sRequest is always present for CIDB requests, regardless of the
    # configured bot_input_fields list (some deployments may have omitted it).
    if payload.company == "CIDB":
        if "sRequest" not in bot_input:
            bot_input["sRequest"] = (
                payload.fields.get("sRequest")
                or payload.fields.get("sRequst")
                or ""
            ).strip()
        # Clean up any empty values after the forced insertion.
        bot_input = {k: v for k, v in bot_input.items() if v}
    else:
        bot_input = {k: v for k, v in bot_input.items() if v}

    bot_input["sChannel"] = payload.channel

    schedule_id = _generate_schedule_id()

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
        play_order,
        play_id,
        playlist["GroupID"],
        playlist["CompanyID"],
        playlist["BotID"],
        runner["ControlRoomIP"],
        runner["RunnerName"],
        runner["RunnerID"],
        json.dumps(bot_input),
        f"api:{key_hint}",
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
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database insert failed. Check the hub audit log for the failure.",
        ) from e

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
            "bot_input_fields": list(bot_input),
            "runner_fallback": is_fallback,
        },
    )

    return ExternalTicketResponse(
        status="inserted",
        schedule_id=schedule_id,
        scenario_label=scenario_label,
        runner_name=runner["RunnerName"],
        runner_id=runner["RunnerID"],
        control_room_ip=runner["ControlRoomIP"],
        bot_input=bot_input,
        runner_note=(
            f"All runners were busy — assigned to least-loaded runner {runner['RunnerName']}."
            if is_fallback else "Assigned to available runner."
        ),
    )