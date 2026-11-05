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

# Values pasted into GitHub secrets often carry a stray space or newline: trim them.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "").strip() or "587")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
if SMTP_HOST.lower() == "smtp.gmail.com":
    SMTP_PASSWORD = SMTP_PASSWORD.replace(" ", "")   # Google shows app passwords as "abcd efgh ijkl mnop"
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
# Alternative to SMTP: Brevo's HTTPS API (works where mail ports are blocked). Sender = a sender address verified in Brevo.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "").strip() or SMTP_FROM or os.getenv("ADMIN_EMAIL", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = ""  # filled in at startup from getMe

# Print the body of e-mails (password-reset links!) to the log when SMTP is not set up. Local development only.
MAIL_LOG_BODY = env_bool("MAIL_LOG_BODY")
MAIL_ENABLED = bool(SMTP_HOST or BREVO_API_KEY)
# With working e-mail, customers must confirm their address before ordering (otherwise nobody could be reached).
VERIFY_REQUIRED = MAIL_ENABLED
CODE_TTL_MIN = 10                # a sign-in code from e-mail works this long
CODE_MAX_TRIES = 5               # wrong guesses before the code is thrown away
CONTACT_PER_HOUR = 3             # contact-form messages per IP per hour

# ---- ordering rules
MAX_NO_SHOWS = 2                 # a customer is blocked at a venue after this many no-shows there
NO_SHOW_GRACE_MIN = 15           # "no-show" can be set only this long after the promised arrival time
EXPIRE_AFTER_MIN = 120           # unhandled orders are closed automatically this long after the arrival time
MAX_ACTIVE_PER_USER = 4          # active orders one customer may have at the same time
MAX_ACTIVE_PER_USER_VENUE = 2    # ... and at one venue
MAX_ORDER_TOTAL = 100_000        # grosze = 1000 zł per order
MAX_CART_LINES = 30
MAX_CATEGORIES = 30              # per venue
MAX_OPTION_GROUPS = 20           # per venue
MAX_OPTIONS_PER_GROUP = 20
MAX_UPLOAD_BYTES = 6_000_000     # a photo may be this big before it is shrunk
MAX_IMAGE_PIXELS = 25_000_000    # decompression-bomb guard
UPLOADS_PER_HOUR = 30            # photo uploads per venue per hour
MAX_ITEMS = 300                  # per venue
VENUE_ORDERS_PER_HOUR = 120      # stops a flood of fake orders at one venue

# ---- load protection
MAX_BODY_BYTES = 100_000         # requests larger than this are rejected (counted while reading)
RATE_ALL = (240, 60)             # requests per IP per 60 s, all pages
RATE_POST = (60, 60)             # form submissions per IP per 60 s
RATE_SEARCH = (40, 60)           # live-search requests per IP per 60 s
HASH_CONCURRENCY = 4             # password hashes computed at the same time (each uses ~16 MB)
QUERY_TIMEOUT_S = 3              # a single database query running longer than this is aborted
WS_MAX_PER_IP = 10               # open WebSockets per IP
