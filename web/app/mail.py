import asyncio
import logging
import smtplib
from email.message import EmailMessage

from . import config as cfg
from .tasks import spawn

log = logging.getLogger("coffee")


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"] = cfg.SMTP_FROM, to
    msg["Subject"] = subject.replace("\n", " ").replace("\r", " ")[:200]
    msg.set_content(body)
    if cfg.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=15) as s:
            if cfg.SMTP_USER:
                s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if cfg.SMTP_USER:
                s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            s.send_message(msg)


async def send_mail(to: str, subject: str, body: str) -> bool:
    """Send an e-mail. Without SMTP settings the message is only written to the log (dev mode)."""
    if not cfg.SMTP_HOST:
        # The body can hold a password-reset link: never log it unless MAIL_LOG_BODY=1 (local development).
        log.info("[mail, SMTP not configured] to=%s subject=%s%s", to, subject, ("\n" + body) if cfg.MAIL_LOG_BODY else "")
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        return True
    except Exception as exc:  # never break a request because mail is down
        log.error("Could not send mail to %s: %s", to, exc)
        return False


def send_mail_bg(to: str, subject: str, body: str) -> None:
    """Send without making the visitor wait for the mail server."""
    spawn(send_mail(to, subject, body))


def public_url(path: str, request=None) -> str:
    """Absolute URL for links in e-mails. BASE_URL wins; request is only a dev fallback."""
    if cfg.BASE_URL:
        return cfg.BASE_URL + path
    if request is not None:
        return str(request.base_url).rstrip("/") + path
    return path
