"""Owner-only admin: approve new venues, suspend them, unblock customers.
Protected by ADMIN_PASSWORD (env). If the variable is empty the whole admin area is disabled."""
import hmac

from starlette.exceptions import HTTPException

from . import config as cfg
from .core import go, login_session, read_form, render, require_admin, route
from .mail import diagnose, public_url, send_mail_bg, send_mail_checked
from .security import client_ip, limiter
from .web_public import invalidate_index

ACTIONS = {"approve": "approved", "reject": "rejected", "suspend": "suspended", "restore": "approved"}


def _enabled():
    if not cfg.ADMIN_PASSWORD:
        raise HTTPException(404, "The admin area is not enabled. Set ADMIN_PASSWORD to turn it on.")


@route
async def admin_login(request, conn):
    _enabled()
    if request.method == "GET":
        return render(request, "admin_login.html")
    form = await read_form(request)
    if not limiter.allow(f"admin-ip:{client_ip(request)}", 5, 600) or not limiter.allow("admin-all", 30, 3600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
    sent = str(form.get("password", ""))[:256].encode()
    if not hmac.compare_digest(sent, cfg.ADMIN_PASSWORD.encode()):
        return render(request, "admin_login.html", 400, error="Wrong password.")
    login_session(request, admin=True)
    return go(request, "/admin")


@route
async def admin_home(request, conn):
    _enabled()
    require_admin(request)
    venues = conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM items i WHERE i.venue_id = v.id) AS item_count, "
        "(SELECT COUNT(*) FROM orders o WHERE o.venue_id = v.id) AS order_count "
        "FROM venues v ORDER BY CASE v.status WHEN 'pending' THEN 0 ELSE 1 END, v.id DESC").fetchall()
    blocked = conn.execute(
        "SELECT f.*, u.name, u.email, v.name AS venue_name FROM venue_customer_flags f "
        "JOIN users u ON u.id = f.user_id JOIN venues v ON v.id = f.venue_id "
        "WHERE f.warnings > 0 ORDER BY f.blocked DESC, f.venue_id").fetchall()
    messages = conn.execute("SELECT * FROM contact_messages ORDER BY id DESC LIMIT 30").fetchall()
    return render(request, "admin.html", venues=venues, blocked=blocked, messages=messages,
                  mail_enabled=cfg.MAIL_ENABLED, admin_email=cfg.ADMIN_EMAIL)


@route
async def admin_test_mail(request, conn):
    """Sends a real e-mail to ADMIN_EMAIL and shows the exact result, so a wrong SMTP setting is visible at once."""
    _enabled()
    await read_form(request)
    require_admin(request)
    if not limiter.allow("admin-test-mail", 5, 600):
        raise HTTPException(429, "Please wait a few minutes before sending another test.")
    if not cfg.ADMIN_EMAIL:
        return go(request, "/admin", error="Set the ADMIN_EMAIL secret first: the test mail is sent to it.")
    problem = await send_mail_checked(cfg.ADMIN_EMAIL, "Test email", "If you can read this, sending email works.")
    if problem:
        return go(request, "/admin", error=f"Test email failed. {problem}")
    return go(request, "/admin", msg=f"Test email sent to {cfg.ADMIN_EMAIL}. Check the inbox (and spam).")


@route
async def admin_venue_action(request, conn):
    _enabled()
    form = await read_form(request)
    require_admin(request)
    action = request.path_params["action"]
    if action not in ACTIONS:
        raise HTTPException(404, "Unknown action")
    venue = conn.execute("SELECT * FROM venues WHERE id = ?", (request.path_params["venue_id"],)).fetchone()
    if not venue:
        raise HTTPException(404, "Venue not found")
    if ACTIONS[action] == "approved":  # approval is the check that a venue is real and ready: do not let empty shells through
        missing = []
        if not venue["address"]:
            missing.append("a street address")
        if not venue["city"]:
            missing.append("a city")
        if not conn.execute("SELECT 1 FROM items WHERE venue_id = ? AND is_available = 1", (venue["id"],)).fetchone():
            missing.append("at least one menu item")
        if missing:
            return go(request, "/admin", error=f"{venue['name']} cannot be approved yet. Missing: {', '.join(missing)}.")
    with conn:
        conn.execute("UPDATE venues SET status = ? WHERE id = ?", (ACTIONS[action], venue["id"]))
    invalidate_index()
    if action == "approve":
        send_mail_bg(venue["email"], "Your venue is approved",
                     f"Good news: {venue['name']} is now public.\n{public_url('/v/' + venue['slug'], request)}")
    elif action == "reject":
        send_mail_bg(venue["email"], "Your venue was not approved",
                     f"We could not approve {venue['name']}. Reply to this email if you think this is a mistake.")
    return go(request, "/admin", msg=f"{venue['name']}: {ACTIONS[action]}.")


@route
async def admin_unblock_user(request, conn):
    _enabled()
    await read_form(request)
    require_admin(request)
    with conn:
        conn.execute("DELETE FROM venue_customer_flags WHERE venue_id = ? AND user_id = ?",
                     (request.path_params["venue_id"], request.path_params["user_id"]))
    return go(request, "/admin", msg="Warnings cleared.")


@route
async def admin_mail_check(request, conn):
    """Shows how the mail settings look from the server (no secrets) and whether the mail ports answer."""
    _enabled()
    await read_form(request)
    require_admin(request)
    if not limiter.allow("admin-mail-check", 10, 600):
        raise HTTPException(429, "Please wait a few minutes before checking again.")
    return go(request, "/admin", msg="Mail check: " + await diagnose())
