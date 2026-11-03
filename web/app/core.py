"""Shared web plumbing: templates, per-request DB connection, login helpers, CSRF-checked forms."""
import logging
import time
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from starlette.concurrency import run_in_threadpool

from . import config as cfg
from . import db, google, orders
from .orders import ACTION_LABELS, ALLOWED, ARRIVAL_CHOICES, STATUS_LABELS
from .security import csrf_token, csrf_valid
from .utils import (Busy, format_zl, hash_password_limited, session_fingerprint, verify_password_limited)

log = logging.getLogger("coffee")
BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "Ahead"  # working name: rename freely
ADMIN_SESSION_SECONDS = 8 * 3600

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["zl"] = format_zl

_static_versions: dict[str, int] = {}


def static_url(path: str) -> str:
    """/static/x.js?v=<file time>: browsers cache it for a year and still get new code after a deploy."""
    if path not in _static_versions:
        try:
            _static_versions[path] = int((BASE_DIR / "static" / path).stat().st_mtime)
        except OSError:
            _static_versions[path] = 0
    return f"/static/{path}?v={_static_versions[path]}"


templates.env.globals.update(APP_NAME=APP_NAME, STATUS_LABELS=STATUS_LABELS, ACTION_LABELS=ACTION_LABELS,
                             ALLOWED=ALLOWED, ARRIVAL_CHOICES=ARRIVAL_CHOICES, static=static_url)


async def hash_async(password: str) -> str:
    """scrypt in a worker thread, at most HASH_CONCURRENCY at a time (each uses ~16 MB of memory)."""
    try:
        return await run_in_threadpool(hash_password_limited, password)
    except Busy:
        raise HTTPException(503, "The server is busy right now. Please try again in a moment.")


async def verify_async(password: str, stored: str) -> bool:
    try:
        return await run_in_threadpool(verify_password_limited, password, stored)
    except Busy:
        raise HTTPException(503, "The server is busy right now. Please try again in a moment.")


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def route(fn):
    """Give the handler its own DB connection and close it afterwards."""
    @wraps(fn)
    async def wrapper(request):
        conn = db.connect()
        request.state.conn = conn
        try:
            return await fn(request, conn)
        finally:
            conn.close()
    return wrapper


# ---------------------------------------------------------------- who is logged in

def current_user(request):
    if not hasattr(request.state, "user"):
        uid = request.session.get("uid")
        user = request.state.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
        if uid and (not user or request.session.get("pv") != session_fingerprint(user["password_hash"])):
            request.session.pop("uid", None)  # deleted account, or password changed elsewhere
            user = None
        request.state.user = user
    return request.state.user


def current_venue(request):
    if not hasattr(request.state, "venue"):
        vid = request.session.get("vid")
        venue = request.state.conn.execute("SELECT * FROM venues WHERE id = ?", (vid,)).fetchone() if vid else None
        if vid and (not venue or request.session.get("vpv") != session_fingerprint(venue["password_hash"])):
            request.session.pop("vid", None)
            venue = None
        request.state.venue = venue
    return request.state.venue


def admin_ok(request) -> bool:
    return time.time() - float(request.session.get("admin_at", 0)) < ADMIN_SESSION_SECONDS


def _here(request) -> str:
    return request.url.path + ("?" + request.url.query if request.url.query else "")


def require_user(request):
    user = current_user(request)
    if not user:
        raise Redirect("/login?next=" + quote(_here(request) if request.method == "GET" else "/"))
    return user


def require_venue(request):
    venue = current_venue(request)
    if not venue:
        raise Redirect("/venue/login")
    return venue


def require_admin(request):
    if not admin_ok(request):
        raise Redirect("/admin/login")


def login_session(request, *, user=None, venue=None, admin=False):
    """Start a fresh session (drops any previous identity and CSRF token)."""
    request.session.clear()
    if user:
        request.session["uid"] = user["id"]
        request.session["pv"] = session_fingerprint(user["password_hash"])
    if venue:
        request.session["vid"] = venue["id"]
        request.session["vpv"] = session_fingerprint(venue["password_hash"])
    if admin:
        request.session["admin_at"] = time.time()


# ---------------------------------------------------------------- forms, messages, rendering

async def read_form(request):
    """Parse a POST body and reject it unless the CSRF token (and Origin, if sent) are valid."""
    form = await request.form(max_fields=200, max_files=0)
    if not csrf_valid(request, form):
        raise HTTPException(403, "Your session expired or the form was invalid. Go back, reload the page and try again.")
    return form


def flash(request, message: str, kind: str = "error") -> None:
    request.session["flash"] = (request.session.get("flash", []) + [[kind, message]])[-3:]


def go(request, url: str, error: str | None = None, msg: str | None = None):
    """Redirect after POST (303) with an optional one-time message stored in the session."""
    if error:
        flash(request, error, "error")
    if msg:
        flash(request, msg, "ok")
    return RedirectResponse(url, status_code=303)


def render(request, name: str, status_code: int = 200, *, fragment: bool = False, **ctx):
    me, ven = current_user(request), current_venue(request)
    conn = request.state.__dict__.get("conn")
    ctx.update(csrf=csrf_token(request), me=me, ven=ven, is_admin=admin_ok(request),
               google_enabled=google.enabled(), demo=cfg.DEMO_MODE, active_order=None, ven_new=0)
    if conn is not None and not fragment:
        if me:  # banner: "your order is being prepared"
            ctx["active_order"] = orders.active_order_of(conn, me["id"])
        if ven:  # badge on "Live" in the venue navigation
            ctx["ven_new"] = conn.execute("SELECT COUNT(*) FROM orders WHERE venue_id = ? AND status = 'new'",
                                          (ven["id"],)).fetchone()[0]
    ctx["flashes"] = [] if fragment else request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


async def http_error(request, exc: HTTPException):
    if request.url.path.startswith("/ws/"):
        return RedirectResponse("/", status_code=303)
    conn = request.state.__dict__.get("conn")
    if conn is None:
        request.state.conn = db.connect()
    try:
        return render(request, "error.html", exc.status_code, code=exc.status_code, message=exc.detail)
    finally:
        if conn is None:
            request.state.conn.close()


async def redirect_handler(request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


async def server_error(request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("Something went wrong. Please try again.", status_code=500)


# ---------------------------------------------------------------- websocket identity

def ws_user(ws):
    """The logged-in customer behind a WebSocket, or None (same checks as for normal pages)."""
    uid = ws.session.get("uid")
    if not uid:
        return None
    conn = db.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        conn.close()
    return user if user and ws.session.get("pv") == session_fingerprint(user["password_hash"]) else None


def ws_venue(ws):
    vid = ws.session.get("vid")
    if not vid:
        return None
    conn = db.connect()
    try:
        venue = conn.execute("SELECT * FROM venues WHERE id = ?", (vid,)).fetchone()
    finally:
        conn.close()
    return venue if venue and ws.session.get("vpv") == session_fingerprint(venue["password_hash"]) else None
