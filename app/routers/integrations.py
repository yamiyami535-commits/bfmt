from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.audit_service import write_audit
from app.deps import require_admin
from app.notifications import render_template, send_email_text, send_telegram_text
from app.settings_store import get_setting, mask_secret, set_setting

router = APIRouter(prefix="/api/admin/integrations", tags=["integrations"])


class TelegramSettingsBody(BaseModel):
    bot_token: str | None = Field(None, description="Leave null to keep existing")
    chat_id: str | None = None
    message_template: str | None = Field(None, description="Use {message} placeholder")


class TelegramTestBody(BaseModel):
    message: str = Field(default="Test message from RPA Control Hub")


class EmailSettingsBody(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    use_tls: bool | None = None
    username: str | None = None
    password: str | None = Field(None, description="Empty string = do not change stored password")
    from_addr: str | None = None
    to_addr: str | None = None


class EmailTestBody(BaseModel):
    subject: str = Field(default="RPA Control Hub test")
    body: str = Field(default="This is a test email from RPA Control Hub.")


@router.get("/telegram")
def get_telegram_settings(_: dict = Depends(require_admin)) -> dict:
    tok = get_setting("TELEGRAM_BOT_TOKEN")
    return {
        "chat_id": get_setting("TELEGRAM_CHAT_ID") or "",
        "bot_token_masked": mask_secret(tok),
        "message_template": get_setting("TELEGRAM_MESSAGE_TEMPLATE") or "Hub: {message}",
        "configured": bool(tok and get_setting("TELEGRAM_CHAT_ID")),
    }


@router.put("/telegram")
def put_telegram_settings(body: TelegramSettingsBody, admin: dict = Depends(require_admin)) -> dict:
    if body.bot_token is not None:
        set_setting("TELEGRAM_BOT_TOKEN", body.bot_token.strip() or None)
    if body.chat_id is not None:
        set_setting("TELEGRAM_CHAT_ID", body.chat_id.strip() or None)
    if body.message_template is not None:
        set_setting("TELEGRAM_MESSAGE_TEMPLATE", body.message_template)
    write_audit(
        admin["username"],
        "settings.telegram.update",
        resource_type="integration",
        resource_id="telegram",
        detail={"keys": [k for k, v in body.model_dump().items() if v is not None]},
    )
    return {"status": "saved"}


@router.post("/telegram/test")
def post_telegram_test(body: TelegramTestBody, admin: dict = Depends(require_admin)) -> dict:
    try:
        tpl = get_setting("TELEGRAM_MESSAGE_TEMPLATE")
        text = render_template(tpl, message=body.message)
        send_telegram_text(text)
        write_audit(admin["username"], "integrations.telegram.test", detail={"ok": True})
        return {"status": "sent"}
    except Exception as e:
        write_audit(admin["username"], "integrations.telegram.test", success=False, detail={"error": str(e)})
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/email")
def get_email_settings(_: dict = Depends(require_admin)) -> dict:
    pwd = get_setting("EMAIL_PASSWORD")
    return {
        "smtp_host": get_setting("EMAIL_SMTP_HOST") or "",
        "smtp_port": int(get_setting("EMAIL_SMTP_PORT") or "587"),
        "use_tls": (get_setting("EMAIL_USE_TLS") or "true").lower() in ("1", "true", "yes"),
        "username": get_setting("EMAIL_USERNAME") or "",
        "password_masked": mask_secret(pwd) if pwd else "",
        "from_addr": get_setting("EMAIL_FROM") or "",
        "to_addr": get_setting("EMAIL_TO") or "",
        "configured": bool(get_setting("EMAIL_SMTP_HOST") and get_setting("EMAIL_FROM") and get_setting("EMAIL_TO")),
    }


@router.put("/email")
def put_email_settings(body: EmailSettingsBody, admin: dict = Depends(require_admin)) -> dict:
    if body.smtp_host is not None:
        set_setting("EMAIL_SMTP_HOST", body.smtp_host.strip() or None)
    if body.smtp_port is not None:
        set_setting("EMAIL_SMTP_PORT", str(body.smtp_port))
    if body.use_tls is not None:
        set_setting("EMAIL_USE_TLS", "true" if body.use_tls else "false")
    if body.username is not None:
        set_setting("EMAIL_USERNAME", body.username.strip() or None)
    if body.password is not None and body.password != "":
        set_setting("EMAIL_PASSWORD", body.password)
    if body.from_addr is not None:
        set_setting("EMAIL_FROM", body.from_addr.strip() or None)
    if body.to_addr is not None:
        set_setting("EMAIL_TO", body.to_addr.strip() or None)
    write_audit(
        admin["username"],
        "settings.email.update",
        resource_type="integration",
        resource_id="email",
        detail={"keys": [k for k, v in body.model_dump().items() if v is not None and k != "password"]},
    )
    return {"status": "saved"}


@router.post("/email/test")
def post_email_test(body: EmailTestBody, admin: dict = Depends(require_admin)) -> dict:
    try:
        send_email_text(body.subject, body.body)
        write_audit(admin["username"], "integrations.email.test", detail={"ok": True})
        return {"status": "sent"}
    except Exception as e:
        write_audit(admin["username"], "integrations.email.test", success=False, detail={"error": str(e)})
        raise HTTPException(status_code=400, detail=str(e)) from e
