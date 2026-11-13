"""
Admin and User routes with Query Builder RBAC.
"""

import time
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.deps import require_admin, require_user_or_admin
from app.db import mysql_fetch_all, mysql_execute 

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/me")
def me(user: dict = Depends(require_user_or_admin)) -> dict:
    return {"username": user["username"], "role": user["role"]}


@router.get("/capabilities")
def capabilities(_: dict = Depends(require_user_or_admin)) -> dict:
    """Roadmap: map each item to a real implementation + audit log row."""
    return {
        "telegram_control": {"status": "planned", "note": "Server-side templates + rate limits"},
        "send_email": {"status": "planned", "note": "SMTP or provider API via background worker"},
        "bot_trigger_disable_restart": {"status": "planned", "note": "MySQL/MSSQL + optional host agent"},
        "manual_logs": {"status": "planned", "note": "Append-only audit_log table + filters"},
        "bot_registry_crud": {"status": "planned", "note": "CRUD + validation + RBAC"},
        "remote_deploy_qc": {"status": "planned", "note": "CI/CD or signed artifact + allow-listed commands"},
        "remote_restart_services": {"status": "planned", "note": "Agent with command allow-list"},
        "realtime_bot_status": {"status": "planned", "note": "WebSocket/SSE + heartbeat from collectors"},
    }


# -------------------------------------------------------------------------
# ADVANCED QUERY BUILDER ROUTE WITH ROLE-BASED ACCESS CONTROL (RBAC)
# -------------------------------------------------------------------------

class QueryRequest(BaseModel):
    sql: str


@router.post("/query")
async def execute_admin_raw_query(
    payload: QueryRequest,
    # Allow both user or admin structural profiles to communicate with this router endpoint
    current_user: dict = Depends(require_user_or_admin), 
):
    sql_clean = payload.sql.strip()
    sql_upper = sql_clean.upper()
    user_role = current_user.get("role", "user").lower() # Fallback safely to user
    
    # --- ROLE-BASED COMMAND VALIDATOR RULES ---
    if user_role == "admin":
        # Admins have full access permission mappings (SELECT, INSERT, UPDATE, DELETE)
        allowed_actions = ("SELECT", "INSERT", "UPDATE", "DELETE")
        if not sql_upper.startswith(allowed_actions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Admins are restricted to standard CRUD operations (SELECT, INSERT, UPDATE, DELETE)."
            )
    else:
        # Standard Users can only issue lookups (SELECT) or record updates (UPDATE).
        # Writing commands like INSERT or destructive actions like DELETE are completely blocked.
        allowed_actions = ("SELECT", "UPDATE")
        if not sql_upper.startswith(allowed_actions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Standard user clearance is restricted to Read (SELECT) and Update (UPDATE) queries only."
            )

    start_time = time.time()
    
    try:
        # Check if the query is a data-returning lookup block or an execution transaction
        is_select_query = sql_upper.startswith("SELECT")
        
        if is_select_query:
            rows = mysql_fetch_all(sql_clean)
            columns = list(rows[0].keys()) if rows else []
            affected_rows = len(rows)
        else:
            # Run non-SELECT statements securely (INSERT/UPDATE/DELETE) using the execution routine helper
            affected_rows = mysql_execute(sql_clean)
            rows = [{"message": f"Transaction complete. Affected row count: {affected_rows}"}]
            columns = ["message"]
            
        duration_ms = int((time.time() - start_time) * 1000)
        
        return {
            "rows": rows,
            "columns": columns,
            "duration_ms": duration_ms,
            "affected_rows": affected_rows,
            "is_select": is_select_query
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database execution error: {str(e)}"
        )