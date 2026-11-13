from __future__ import annotations

from app.db import mysql_execute, mysql_fetch_all


def get_setting(key: str) -> str | None:
    rows = mysql_fetch_all("SELECT v FROM hub_settings WHERE k = %s", (key,))
    if not rows:
        return None
    v = rows[0].get("v")
    return None if v is None else str(v)


def set_setting(key: str, value: str | None) -> None:
    mysql_execute(
        "INSERT INTO hub_settings (k, v) VALUES (%s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)",
        (key, value),
    )


def mask_secret(s: str | None, keep: int = 4) -> str | None:
    if not s:
        return s
    if len(s) <= keep * 2:
        return "****"
    return s[:keep] + "…" + s[-keep:]
