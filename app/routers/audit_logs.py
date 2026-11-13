from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.audit_service import list_audit_logs
from app.deps import require_user_or_admin

router = APIRouter(prefix="/api/admin/audit", tags=["audit"])


@router.get("/logs")
def get_audit_logs(
    _: dict = Depends(require_user_or_admin),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    items, total = list_audit_logs(limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
