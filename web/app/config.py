"""All settings come from environment variables. Read them as `cfg.NAME` at call time."""
import logging
import os
import secrets


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# Public address of the site, e.g. https://coffee.example.com. Used in e-mail links and the
# Google redirect URI. Never taken from request headers (Host-header injection).
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
HTTPS = BASE_URL.startswith("https://")

SECRET_KEY = os.getenv("SECRET_KEY", "")
SECRET_KEY_GENERATED = not SECRET_KEY
if SECRET_KEY_GENERATED:
    SECRET_KEY = secrets.token_hex(32)  # sessions are lost on restart; set SECRET_KEY in production
    logging.getLogger("coffee").warning("SECRET_KEY is not set: a random one is used for this run.")

# Behind Cloudflare / a reverse proxy: take the client IP from CF-Connecting-IP / X-Forwarded-For.
# Leave off when the app is reachable directly, otherwise clients could fake their IP.
TRUST_PROXY = env_bool("TRUST_PROXY")

# Demo only: customers may order without verifying their e-mail address.
DEMO_MODE = env_bool("DEMO_MODE")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()        # receives "new venue" e-mails
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")          # empty = admin panel disabled

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = ""  # filled in at startup from getMe

MAX_NO_SHOWS = 2            # a customer is blocked after this many no-shows
MAX_BODY_BYTES = 100_000    # requests larger than this are rejected
