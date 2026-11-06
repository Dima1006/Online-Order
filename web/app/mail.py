import asyncio
import json
import logging
import smtplib
import socket
import urllib.error
import urllib.request
from email.message import EmailMessage

from . import config as cfg
from .tasks import spawn

log = logging.getLogger("coffee")


def _deliver(msg: EmailMessage, port: int) -> None:
    if port == 465:   # TLS from the first byte
        with smtplib.SMTP_SSL(cfg.SMTP_HOST, port, timeout=20) as s:
            if cfg.SMTP_USER:
                s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            s.send_message(msg)
    else:             # plain connection upgraded with STARTTLS (usually 587)
        with smtplib.SMTP(cfg.SMTP_HOST, port, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            if cfg.SMTP_USER:
                s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            s.send_message(msg)


def _send_brevo(to: str, subject: str, body: str) -> None:
    """HTTPS API instead of SMTP. TLS certificates are verified (urllib's default)."""
    payload = json.dumps({"sender": {"email": cfg.MAIL_FROM, "name": "Ahead"}, "to": [{"email": to}],
                          "subject": subject.replace("\n", " ")[:200], "textContent": body}).encode()
    req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=payload, method="POST",
                                 headers={"api-key": cfg.BREVO_API_KEY, "content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"Brevo answered {exc.code}: {detail}") from None


def _send_sync(to: str, subject: str, body: str) -> None:
    if cfg.BREVO_API_KEY:
        return _send_brevo(to, subject, body)
    msg = EmailMessage()
    msg["From"], msg["To"] = cfg.SMTP_FROM, to
    msg["Subject"] = subject.replace("\n", " ").replace("\r", " ")[:200]
    msg.set_content(body)
    try:
        _deliver(msg, cfg.SMTP_PORT)
    except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError, OSError) as first:
        # Some networks close the STARTTLS port: try the SSL port once before giving up.
        if cfg.SMTP_PORT == 465:
            raise
        try:
            _deliver(msg, 465)
        except Exception as second:
            raise RuntimeError(f"port {cfg.SMTP_PORT}: {type(first).__name__} {first}; port 465: {type(second).__name__} {second}") from second


async def send_mail(to: str, subject: str, body: str) -> bool:
    """Send an e-mail. Without SMTP settings the message is only written to the log (dev mode)."""
    if not cfg.MAIL_ENABLED:
        # The body can hold a password-reset link: never log it unless MAIL_LOG_BODY=1 (local development).
        log.info("[mail, SMTP not configured] to=%s subject=%s%s", to, subject, ("\n" + body) if cfg.MAIL_LOG_BODY else "")
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        return True
    except Exception as exc:  # never break a request because mail is down
        log.error("Could not send mail to %s: %s", to, exc)
        return False


def _smtp_steps(port: int) -> str:
    """Walks through the SMTP conversation step by step and says where the server hangs up. Sends no mail."""
    steps = []
    s = None
    try:
        # Host goes to the constructor: smtplib only remembers it there, and starttls() needs it for the TLS hostname.
        s = smtplib.SMTP(cfg.SMTP_HOST, port, timeout=10); steps.append("connect 220")
        code, msg = s.ehlo(); steps.append(f"ehlo {code}")
        code, msg = s.starttls(); steps.append(f"starttls {code}")
        code, msg = s.ehlo(); steps.append(f"ehlo-tls {code}")
        if cfg.SMTP_USER:
            code, msg = s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD); steps.append(f"login {code}")
        steps.append("OK: the server accepts this login" if cfg.SMTP_USER else "OK (no SMTP_USER, login not tried)")
    except smtplib.SMTPAuthenticationError as exc:
        steps.append(f"login REFUSED {exc.smtp_code} {exc.smtp_error.decode('utf-8', 'replace')[:100]}")
    except Exception as exc:
        steps.append(f"STOPPED after '{steps[-1] if steps else 'start'}': {type(exc).__name__} {str(exc)[:120]}")
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass
    return " > ".join(steps)


def _diagnose_sync() -> str:
    """What the server can see: settings (no secrets) and whether the mail ports answer. Shown to the admin."""
    if cfg.BREVO_API_KEY:
        return f"Using Brevo API, sender {cfg.MAIL_FROM or '(not set)'}, key length {len(cfg.BREVO_API_KEY)}."
    parts = [f"host={cfg.SMTP_HOST!r}", f"port={cfg.SMTP_PORT}", f"user={cfg.SMTP_USER!r}",
             f"password length={len(cfg.SMTP_PASSWORD)} (a Google app password has 16)"]
    try:
        ips = sorted({a[4][0] for a in socket.getaddrinfo(cfg.SMTP_HOST, cfg.SMTP_PORT, type=socket.SOCK_STREAM)})
        parts.append("DNS ok: " + ", ".join(ips[:3]))
    except OSError as exc:
        return " | ".join(parts + [f"DNS FAILED: {exc}. The SMTP_HOST value is wrong."])
    parts.append(f"our hostname={socket.getfqdn()!r}")
    parts.append("conversation: " + _smtp_steps(587))
    try:
        with smtplib.SMTP_SSL(cfg.SMTP_HOST, 465, timeout=10) as s:   # TCP alone is not enough: check the TLS handshake too
            s.ehlo()
            parts.append("port 465 TLS ok")
    except Exception as exc:
        parts.append(f"port 465 FAILED: {type(exc).__name__} {str(exc)[:120]}")
    parts.append("If TLS is cut on both ports, this network blocks SMTP: use BREVO_API_KEY + MAIL_FROM (HTTPS).")
    return " | ".join(parts)


async def diagnose() -> str:
    return await asyncio.to_thread(_diagnose_sync)


async def send_mail_checked(to: str, subject: str, body: str) -> str | None:
    """Like send_mail, but returns the reason when it fails (None = sent). Used by the admin "test e-mail" button."""
    if not cfg.MAIL_ENABLED:
        return "Email is not configured: add the SMTP_HOST, SMTP_USER and SMTP_PASSWORD secrets (or BREVO_API_KEY)."
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
        return None
    except smtplib.SMTPAuthenticationError:
        return "The mail server refused the login. For Gmail use an App Password (not your normal password)."
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:200]}"


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
