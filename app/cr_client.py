"""
Control Room REST helpers (auth + device list + deploy).
Base URL: {scheme}://{cr_ip}{path}
"""
from __future__ import annotations

from typing import Any

import requests

from app.config import get_settings
from app.db import mysql_fetch_all


def cr_base_url(cr_ip: str) -> str:
    s = get_settings()
    scheme = (s.cr_base_scheme or "http").rstrip(":/")
    return f"{scheme}://{cr_ip}"


def cr_get_token_for_ip(cr_ip: str) -> str:
    rows = mysql_fetch_all(
        "SELECT CR_Username, CR_Password, API_Key FROM rpa_token WHERE CR_IP = %s LIMIT 1",
        (cr_ip,),
    )
    if not rows:
        raise RuntimeError(f"No rpa_token row for CR_IP={cr_ip}")

    username = rows[0]["CR_Username"]
    password = rows[0]["CR_Password"]
    api_key = rows[0]["API_Key"]

    auth_url = f"{cr_base_url(cr_ip)}/v1/authentication"
    auth_payload = {"username": username, "password": password, "apiKey": api_key}
    auth_resp = requests.post(
        auth_url,
        json=auth_payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=15,
    )
    if not auth_resp.ok:
        raise RuntimeError(f"Auth failed ({auth_url}): {auth_resp.status_code} {auth_resp.text[:500]}")

    token = (auth_resp.json() or {}).get("token")
    if not token:
        raise RuntimeError("Auth response had no token")
    return str(token)


def cr_list_devices(cr_ip: str, token: str) -> list[dict[str, Any]]:
    """
    Fetches registered devices from Control Room.
    Default path POST /v2/devices/list — override via settings.cr_devices_list_path if your CR differs.
    """
    s = get_settings()
    path = (s.cr_devices_list_path or "/v2/devices/list").strip()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{cr_base_url(cr_ip)}{path}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Authorization": token,
    }
    body = {"sort": [], "filter": {}, "page": {"offset": 0, "length": int(s.cr_devices_page_length)}}

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    if resp.status_code == 404:
        resp = requests.get(url, headers=headers, timeout=30)

    if not resp.ok:
        raise RuntimeError(f"Devices list failed ({url}): {resp.status_code} {resp.text[:800]}")

    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "data", "devices", "content", "resources"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def cr_deploy_automation(cr_ip: str, token: str, file_id: int, run_as_user_ids: list[int]) -> tuple[int, Any]:
    url = f"{cr_base_url(cr_ip)}/v3/automations/deploy"
    payload = {
        "fileId": int(file_id),
        "runAsUserIds": [int(x) for x in run_as_user_ids],
        "overrideDefaultDevice": False,
    }
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Authorization": token,
        },
        timeout=15,
    )
    ct = resp.headers.get("content-type", "")
    body_out: Any
    if ct.startswith("application/json"):
        try:
            body_out = resp.json()
        except Exception:
            body_out = resp.text
    else:
        body_out = resp.text
    return resp.status_code, body_out


def device_hostname(device: dict[str, Any]) -> str | None:
    for k in ("hostName", "hostname", "name", "deviceName", "deviceHostName"):
        v = device.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def device_connection_status(device: dict[str, Any]) -> str | None:
    for k in ("status", "deviceStatus", "connectionStatus", "agentStatus"):
        v = device.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def index_devices_by_hostname(devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in devices:
        hn = device_hostname(d)
        if not hn:
            continue
        key = hn.lower()
        out[key] = d
    return out


def _parse_list_response(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "data", "content", "resources", "activities", "executions"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def cr_list_activities(cr_ip: str, token: str) -> list[dict[str, Any]]:
    """
    POST /v3/activity/list — recent activity rows (filter client-side for in-progress buckets).
    """
    s = get_settings()
    path = (s.cr_activity_list_path or "/v3/activity/list").strip()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{cr_base_url(cr_ip)}{path}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Authorization": token,
    }
    # Broad fetch: sort by start time, paginate; filter empty = all statuses (trim in app).
    body = {
        "filter": {},
        "sort": [{"field": "startDateTime", "direction": "desc"}],
        "page": {"offset": 0, "length": int(s.cr_activity_page_length)},
    }

    resp = requests.post(url, json=body, headers=headers, timeout=45)
    if not resp.ok:
        raise RuntimeError(f"Activity list failed ({url}): {resp.status_code} {resp.text[:800]}")

    return _parse_list_response(resp.json())


def activity_device_name(row: dict[str, Any]) -> str | None:
    for k in ("deviceName", "hostName", "hostname"):
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def activity_status(row: dict[str, Any]) -> str:
    v = row.get("status")
    return str(v).strip().upper() if v is not None else ""


def index_activities_by_hostname(activities: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    from collections import defaultdict

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in activities:
        dn = activity_device_name(a)
        if not dn:
            continue
        buckets[dn.lower()].append(a)
    # newest first per device (API already sorted desc; keep order)
    return dict(buckets)


def _parse_status_csv(s: str) -> set[str]:
    return {x.strip().upper() for x in (s or "").split(",") if x.strip()}


def summarize_activity_row(status: str) -> str:
    """Bucket label: active | queued | pending | terminal | other."""
    settings = get_settings()
    st = (status or "").upper().replace(" ", "_")
    if st in _parse_status_csv(settings.cr_activity_status_active):
        return "active"
    if st in _parse_status_csv(settings.cr_activity_status_queued):
        return "queued"
    if st in _parse_status_csv(settings.cr_activity_status_pending):
        return "pending"
    terminal = {
        "COMPLETED",
        "RUN_FAILED",
        "RUN_ABORTED",
        "RUN_TIMED_OUT",
        "DEPLOY_FAILED",
    }
    if st in terminal:
        return "terminal"
    return "other"


def activity_row_summary(a: dict[str, Any]) -> dict[str, Any]:
    st = activity_status(a)
    return {
        "id": a.get("id"),
        "status": a.get("status"),
        "bucket": summarize_activity_row(st),
        "automationName": a.get("automationName") or a.get("currentBotName"),
        "fileName": a.get("fileName"),
        "deviceName": activity_device_name(a),
        "userId": a.get("userId"),
        "progress": a.get("progress"),
        "startDateTime": a.get("startDateTime"),
        "queueName": a.get("queueName"),
    }
