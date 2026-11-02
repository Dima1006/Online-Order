"""Owner-only admin: approve new venues, suspend them, unblock customers.
Protected by ADMIN_PASSWORD (env). If the variable is empty the whole admin area is disabled."""
import hmac

from starlette.exceptions import HTTPException

from . import config as cfg
from .core import go, login_session, read_form, render, require_admin, route
from .mail import public_url, send_mail
from .security import client_ip, limiter

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
    blocked = conn.execute("SELECT * FROM users WHERE is_blocked = 1 OR warning_count > 0 ORDER BY id DESC").fetchall()
    return render(request, "admin.html", venues=venues, blocked=blocked)


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
    with conn:
        conn.execute("UPDATE venues SET status = ? WHERE id = ?", (ACTIONS[action], venue["id"]))
    if action == "approve":
        await send_mail(venue["email"], "Your venue is approved",
                        f"Good news: {venue['name']} is now public.\n{public_url('/v/' + venue['slug'], request)}")
    elif action == "reject":
        await send_mail(venue["email"], "Your venue was not approved",
                        f"We could not approve {venue['name']}. Reply to this email if you think this is a mistake.")
    return go(request, "/admin", msg=f"{venue['name']}: {ACTIONS[action]}.")


@route
async def admin_unblock_user(request, conn):
    _enabled()
    await read_form(request)
    require_admin(request)
    with conn:
        conn.execute("UPDATE users SET warning_count = 0, is_blocked = 0 WHERE id = ?", (request.path_params["user_id"],))
    return go(request, "/admin", msg="Customer unblocked.")
