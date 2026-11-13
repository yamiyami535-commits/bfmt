from fastapi import Depends, HTTPException, status

async def get_current_user():
    """Placeholder: replace with your real auth extraction."""
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not implemented")

def require_admin(user = Depends(get_current_user)):
    if (getattr(user, "role", "") or "").lower() != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user