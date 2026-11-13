from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import get_settings
from app.security import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


@router.post("/login", response_model=TokenResponse)
def login(body: LoginBody) -> TokenResponse:
    """
    Bootstrap login using env-configured admin user.
    Replace with DB users, MFA, and rate limiting before production.
    """
    settings = get_settings()
    role = None
    if body.username == settings.admin_username and body.password == settings.admin_password:
        role = "admin"
    elif settings.user_username and settings.user_password and body.username == settings.user_username and body.password == settings.user_password:
        role = "user"

    if role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token({"sub": body.username, "role": role})
    return TokenResponse(
        access_token=token,
        expires_in_minutes=settings.access_token_expire_minutes,
    )
