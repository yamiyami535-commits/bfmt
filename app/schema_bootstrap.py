"""Create hub tables if missing (idempotent)."""

from __future__ import annotations

import json

from app.db import mysql_conn


DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS hub_audit_log (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      actor_username VARCHAR(128) NOT NULL,
      action VARCHAR(96) NOT NULL,
      resource_type VARCHAR(64) NULL,
      resource_id VARCHAR(512) NULL,
      success TINYINT(1) NOT NULL DEFAULT 1,
      client_ip VARCHAR(64) NULL,
      detail_json LONGTEXT NULL,
      KEY ix_hub_audit_created (created_at),
      KEY ix_hub_audit_actor (actor_username),
      KEY ix_hub_audit_action (action)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS hub_settings (
      k VARCHAR(128) NOT NULL PRIMARY KEY,
      v LONGTEXT NULL,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # ── Ticket Insert: scenario config ──────────────────────────────────────
    # One row per (company, scenario). `hardcoded_json` holds the fixed
    # bot_scheduler columns (ScheduleName, PlayID, GroupID, BotID, etc).
    # `channels_json` describes, per channel, which Bot_Input keys are
    # user-filled vs derived, plus which form fields to render.
    # RunnerID / ControlRoomIP are NEVER stored here — always joined live
    # from rpa_runner based on the operator's Runner selection.
    """
    CREATE TABLE IF NOT EXISTS ticket_scenarios (
      id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      company VARCHAR(64) NOT NULL,
      scenario_key VARCHAR(96) NOT NULL,
      scenario_label VARCHAR(160) NOT NULL,
      hardcoded_json LONGTEXT NOT NULL,
      channels_json LONGTEXT NOT NULL,
      is_active TINYINT(1) NOT NULL DEFAULT 1,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY ux_ticket_scenario (company, scenario_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


# ─────────────────────────────────────────────────────────────────────────────
# Seed data — CelcomDigi scenarios
#
# hardcoded fields: every bot_scheduler column that is fixed per scenario,
# EXCEPT RunnerName / RunnerID / ControlRoomIP (always derived from the
# Runner picked in the form via a live rpa_runner lookup).
#
# channels_json shape:
# {
#   "<CHANNEL>": {
#       "bot_input_fields": [list of Bot_Input keys, in order],
#       "form_fields": [
#           {"key": ..., "label": ..., "type": "text"|"select", "required": true,
#            "options": [...]}   # options only for type=select
#       ]
#   }
# }
#
# Every scenario's form always also includes a Runner select control,
# handled generically by the frontend/backend — not declared per-channel here.
# ─────────────────────────────────────────────────────────────────────────────

SEED_SCENARIOS = [
    {
        "company": "CelcomDigi",
        "scenario_key": "voicemail",
        "scenario_label": "Voicemail & Miss Call Alert",
        "hardcoded": {
            "ScheduleType": "Production",
            "GlobalStatus": "RUN",
            "ScheduleName": "CelcomDigi - Voicemail & Miss Call Alert",
            "PlayOrder": 1,
            "PlayID": 76,
            "GroupID": "9-1719476322",
            "CompanyID": 1,
            "BotID": 89,
            "BotType": "Automation Anywhere",
            "DigitalWorkerType": "Dedicated - On Premise",
            "CompletionStatus": "PENDING",
            "RecordingRequired": "No",
            "Intervention": "No",
        },
        "channels": {
            "Daisy": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sRequest", "label": "Request", "type": "select", "required": True,
                     "options": ["Activate", "Deactivate"]},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                ],
            },
            "WhatsApp": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sRequest", "label": "Request", "type": "select", "required": True,
                     "options": ["Activate", "Deactivate"]},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                ],
            },
            "IVR": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sRequest", "sTelco"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sRequest", "label": "Request", "type": "select", "required": True,
                     "options": ["Activate", "Deactivate"]},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                ],
            },
        },
    },
    {
        "company": "CelcomDigi",
        "scenario_key": "check_port_in",
        "scenario_label": "Check Port In Status",
        "hardcoded": {
            "ScheduleType": "Production",
            "GlobalStatus": "RUN",
            "ScheduleName": "Check Port In Status",
            "PlayOrder": 1,
            "PlayID": 70,
            "GroupID": "9-1717730453",
            "CompanyID": 1,
            "BotID": 85,
            "BotType": "Automation Anywhere",
            "DigitalWorkerType": "Dedicated - On Premise",
            "CompletionStatus": "PENDING",
            "RecordingRequired": "No",
            "Intervention": "No",
        },
        "channels": {
            "Daisy": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                ],
            },
            "WhatsApp": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                ],
            },
            "IVR": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                ],
            },
        },
    },
    {
        "company": "CelcomDigi",
        "scenario_key": "resubmit_port_in",
        "scenario_label": "Resubmit Port In Masterbot",
        "hardcoded": {
            "ScheduleType": "Production",
            "GlobalStatus": "RUN",
            "ScheduleName": "Resubmit Port In Masterbot",
            "PlayOrder": 1,
            "PlayID": 71,
            "GroupID": "9-1717732497",
            "CompanyID": 1,
            "BotID": 86,
            "BotType": "Automation Anywhere",
            "DigitalWorkerType": "Dedicated - On Premise",
            "CompletionStatus": "PENDING",
            "RecordingRequired": "No",
            "Intervention": "No",
        },
        "channels": {
            "Daisy": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                    {"key": "sCustomerID", "label": "Customer ID (NRIC)", "type": "text", "required": True},
                    {"key": "sComment", "label": "Comment", "type": "text", "required": False},
                ],
            },
            "WhatsApp": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                    {"key": "sCustomerID", "label": "Customer ID (NRIC)", "type": "text", "required": True},
                    {"key": "sComment", "label": "Comment", "type": "text", "required": False},
                ],
            },
            "IVR": {
                "bot_input_fields": ["sServiceNumber", "sTicketID", "sChannel", "sTelco", "sCustomerID", "sComment"],
                "form_fields": [
                    {"key": "sServiceNumber", "label": "Service Number", "type": "text", "required": True},
                    {"key": "sTicketID", "label": "Ticket ID", "type": "text", "required": True},
                    {"key": "sTelco", "label": "Telco", "type": "select", "required": True,
                     "options": ["Celcom", "Digi"]},
                    {"key": "sCustomerID", "label": "Customer ID (NRIC)", "type": "text", "required": True},
                    {"key": "sComment", "label": "Comment", "type": "text", "required": False},
                ],
            },
        },
    },
    {
        "company": "CIDB",
        "scenario_key": "cidb_masterbot",
        "scenario_label": "Cancellation Email Masterbot",
        "hardcoded": {
            "ScheduleType": "Production",
            "GlobalStatus": "RUN",
            "ScheduleName": "Cancellation Email Masterbot",
            "PlayOrder": 1,
            "PlayID": 105,
            "GroupID": "9-1784599526",
            "CompanyID": 1,
            "BotID": 118,
            "BotType": "Automation Anywhere",
            "DigitalWorkerType": "Dedicated - On Premise",
            "CompletionStatus": "PENDING",
            "RecordingRequired": "No",
            "Intervention": "No",
        },
        "channels": {
            "Email": {
                "bot_input_fields": [
                    "sAction",
                    "sRequst",
                    "sCRMID",
                    "sEmail",
                    "sCustomerName",
                    "sCustomerType",
                    "sIdentificationNumber",
                    "sLanguage",
                    "sChannel",
                    "sState",
                    "sContactNumber",
                    "sFilename",
                    "sOCRStatus",
                ],
                "form_fields": [],
            },
        },
    },
]


def _seed_ticket_scenarios() -> None:
    """Insert missing default scenarios without overwriting existing config."""
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            for s in SEED_SCENARIOS:
                cur.execute(
                    "INSERT IGNORE INTO ticket_scenarios "
                    "(company, scenario_key, scenario_label, hardcoded_json, channels_json) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        s["company"],
                        s["scenario_key"],
                        s["scenario_label"],
                        json.dumps(s["hardcoded"]),
                        json.dumps(s["channels"]),
                    ),
                )


def ensure_hub_tables() -> None:
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            for ddl in DDL_STATEMENTS:
                cur.execute(ddl)
    _seed_ticket_scenarios()
