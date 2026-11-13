from __future__ import annotations

import json
from typing import Any

from app.db import mysql_execute, mysql_fetch_all


def write_audit(
    actor_username: str,
    action: str,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    success: bool = True,
    detail: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> None:
    try:
        mysql_execute(
            "INSERT INTO hub_audit_log (actor_username, action, resource_type, resource_id, success, client_ip, detail_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                actor_username,
                action,
                resource_type,
                resource_id,
                1 if success else 0,
                client_ip,
                json.dumps(detail, default=str) if detail else None,
            ),
        )
    except Exception:
        # Never break main flow if audit insert fails
        pass


def list_audit_logs(*, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    rows = mysql_fetch_all(
        "SELECT id, created_at, actor_username, action, resource_type, resource_id, success, client_ip, detail_json "
        "FROM hub_audit_log ORDER BY id DESC LIMIT %s OFFSET %s",
        (max(1, min(limit, 500)), max(0, offset)),
    )
    cnt = mysql_fetch_all("SELECT COUNT(*) AS c FROM hub_audit_log")
    total = int(cnt[0]["c"]) if cnt else 0
    out = []
    for r in rows:
        d = dict(r)
        dj = d.get("detail_json")
        if isinstance(dj, str) and dj.strip():
            try:
                d["detail"] = json.loads(dj)
            except json.JSONDecodeError:
                d["detail"] = {"raw": dj}
        else:
            d["detail"] = None
        d.pop("detail_json", None)
        out.append(d)
    return out, total
