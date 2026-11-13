from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import requests

from app.settings_store import get_setting


def send_telegram_text(message: str, *, bot_token: str | None = None, chat_id: str | None = None) -> None:
    token = bot_token or get_setting("TELEGRAM_BOT_TOKEN")
    cid = chat_id or get_setting("TELEGRAM_CHAT_ID")
    if not token or not cid:
        raise ValueError("Telegram bot token or chat id not configured (save in Alerts first).")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": cid, "text": message[:4000], "parse_mode": "HTML"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "Telegram API returned not ok")


def render_template(template: str | None, *, message: str) -> str:
    t = (template or "Hub: {message}").strip()
    return t.replace("{message}", message).replace("[msg]", message)


def send_email_text(
    subject: str,
    body: str,
    *,
    to_addr: str | None = None,
) -> None:
    host = get_setting("EMAIL_SMTP_HOST")
    port = int(get_setting("EMAIL_SMTP_PORT") or "587")
    use_tls = (get_setting("EMAIL_USE_TLS") or "true").lower() in ("1", "true", "yes")
    user = get_setting("EMAIL_USERNAME")
    password = get_setting("EMAIL_PASSWORD")
    from_addr = get_setting("EMAIL_FROM")
    default_to = get_setting("EMAIL_TO")
    to = to_addr or default_to
    if not host or not to or not from_addr:
        raise ValueError("SMTP host, From, or To not configured (save in Alerts first).")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to], msg.as_string())
