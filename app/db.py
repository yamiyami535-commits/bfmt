from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pymysql

from app.config import get_settings


@contextmanager
def mysql_conn():
    """
    Minimal MySQL connector.
    For scale: switch to pooling (e.g. SQLAlchemy engine) and background jobs for long operations.
    """
    s = get_settings()
    missing = [k for k in ("mysql_host", "mysql_user", "mysql_password", "mysql_db") if not getattr(s, k)]
    if missing:
        raise RuntimeError(f"Missing MySQL settings in .env: {', '.join(missing)}")

    conn = pymysql.connect(
        host=s.mysql_host,
        port=s.mysql_port,
        user=s.mysql_user,
        password=s.mysql_password,
        db=s.mysql_db,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()


def mysql_fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def mysql_execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.rowcount)
