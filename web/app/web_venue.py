"""Venue side: registration, login, password reset and the panel
(live board, order list, menu, settings, customers, Telegram)."""
import hmac
import hashlib
import re
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocket

from . import config as cfg
from . import events, orders, realtime
from .core import (go, hash_async, login_session, read_form, render, require_venue, route, verify_async, ws_venue)
from .legal import CAPTCHA_ERROR, captcha_ok
from .mail import public_url, send_mail_bg
from .orders import hhmm, utcnow
from .security import client_ip, limiter, ws_origin_ok
from .utils import (clean, dummy_hash, name_problem, normalize, parse_hhmm, parse_variants, password_problem,
                    safe_next, session_fingerprint, slugify, valid_email)
from . import media
from .web_options import load_groups, save_item_groups
from .web_public import invalidate_index, load_menu

TOO_MANY = "Too many attempts. Please wait a few minutes and try again."
PREP_CHOICES = (5, 10, 15, 20, 30)


def _throttle(key: str, limit: int, window: float) -> None:
    if not limiter.allow(key, limit, window):
        raise HTTPException(429, TOO_MANY)


def unique_slug(conn, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM venues WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------- registration, login, password reset

@route
async def venue_register(request, conn):
    if request.method == "GET":
        return render(request, "venue_register.html", form={})
    form = await read_form(request)
    if form.get("website"):  # honeypot: invisible to people, bots fill it in
        raise HTTPException(400, "Something went wrong. Please try again.")
    _throttle(f"venue-register-ip:{client_ip(request)}", 3, 3600)  # stops mass-creating fake venues
    name, city = clean(str(form.get("venue_name", "")), 80), clean(str(form.get("city", "")), 60)
    email = clean(str(form.get("email", "")), 254).lower()
    password = str(form.get("password", ""))
    error = None
    if len(name) < 2:
        error = "Enter the venue name."
    elif not valid_email(email):
        error = "Enter a valid email address."
    elif not form.get("agree"):
        error = "Please accept the Terms and the Privacy Policy."
    elif not await captcha_ok(request, form):
        error = CAPTCHA_ERROR
    elif (problem := password_problem(password, email)):
        error = problem
    elif conn.execute("SELECT 1 FROM venues WHERE email = ?", (email,)).fetchone():
        error = "This email is already registered."
    if error:
        return render(request, "venue_register.html", 400, error=error,
                      form={"venue_name": name, "city": city, "email": email})
    hashed = await hash_async(password)
    with conn:
        vid = conn.execute("INSERT INTO venues (slug, name, city, email, password_hash) VALUES (?, ?, ?, ?, ?)",
                           (unique_slug(conn, name), name, city, email, hashed)).lastrowid
    venue = conn.execute("SELECT * FROM venues WHERE id = ?", (vid,)).fetchone()
    if cfg.ADMIN_EMAIL:
        send_mail_bg(cfg.ADMIN_EMAIL, f"New venue to approve: {name}",
                     f"A venue registered and is waiting for your approval.\n\nName: {name}\nCity: {city}\n"
                     f"Owner email: {email}\n\nReview it here: {public_url('/admin', request)}")
    login_session(request, venue=venue)
    return go(request, "/panel",
              msg="Venue created. Follow the checklist below while we review your venue: it becomes public once approved.")


@route
async def venue_login(request, conn):
    if request.method == "GET":
        return render(request, "venue_login.html", email="")
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    password = str(form.get("password", ""))[:129]
    ip = client_ip(request)
    _throttle(f"vlogin-ip:{ip}", 20, 600)
    _throttle(f"vlogin-pair:{ip}:{email}", 8, 600)
    _throttle(f"vlogin-email:{email}", 40, 3600)
    venue = conn.execute("SELECT * FROM venues WHERE email = ?", (email,)).fetchone()
    ok = await verify_async(password, venue["password_hash"] if venue else dummy_hash())
    if not (venue and ok):
        return render(request, "venue_login.html", 400, email=email, error="Wrong email or password.")
    login_session(request, venue=venue)
    return go(request, "/panel")


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.SECRET_KEY, salt="venue-reset-password")


def _fingerprint(venue) -> str:
    return hashlib.sha256(venue["password_hash"].encode()).hexdigest()[:16]


@route
async def venue_forgot(request, conn):
    if request.method == "GET":
        return render(request, "venue_forgot.html")
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    _throttle(f"vforgot-ip:{client_ip(request)}", 5, 3600)
    _throttle(f"vforgot-email:{email}", 3, 3600)
    venue = conn.execute("SELECT * FROM venues WHERE email = ?", (email,)).fetchone()
    if venue:
        token = _signer().dumps({"vid": venue["id"], "fp": _fingerprint(venue)})
        send_mail_bg(email, "Reset your venue password",
                     f"Choose a new password for {venue['name']}:\n{public_url('/venue/reset/' + token, request)}\n\n"
                     "The link works once and is valid for 1 hour. If you did not ask for it, ignore this message.")
    return render(request, "message.html", title="Check your inbox",
                  text="If a venue account with this email exists, we sent a link to reset the password.")


@route
async def venue_reset(request, conn):
    token = request.path_params["token"]
    venue = None
    try:
        data = _signer().loads(token, max_age=3600)
        venue = conn.execute("SELECT * FROM venues WHERE id = ?", (data.get("vid"),)).fetchone()
        if venue and not hmac.compare_digest(_fingerprint(venue), str(data.get("fp"))):  # link works once
            venue = None
    except (BadSignature, SignatureExpired):
        pass
    if not venue:
        return render(request, "message.html", 400, title="Link expired",
                      text="This reset link is invalid, already used or expired. Request a new one.")
    if request.method == "GET":
        return render(request, "venue_reset.html", token=token)
    form = await read_form(request)
    _throttle(f"vreset-ip:{client_ip(request)}", 10, 3600)
    password = str(form.get("password", ""))
    if (problem := password_problem(password, venue["email"])):
        return render(request, "venue_reset.html", 400, token=token, error=problem)
    hashed = await hash_async(password)
    with conn:
        conn.execute("UPDATE venues SET password_hash = ? WHERE id = ?", (hashed, venue["id"]))
    return go(request, "/venue/login", msg="Password changed. Log in with the new password.")


@route
async def panel_password(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    _throttle(f"vpw-change:{venue['id']}", 5, 3600)
    if not await verify_async(str(form.get("current_password", ""))[:129], venue["password_hash"]):
        return go(request, "/panel/settings", error="Current password is wrong.")
    new = str(form.get("new_password", ""))
    if (problem := password_problem(new, venue["email"])):
        return go(request, "/panel/settings", error=problem)
    hashed = await hash_async(new)
    with conn:
        conn.execute("UPDATE venues SET password_hash = ? WHERE id = ?", (hashed, venue["id"]))
    request.session["vpv"] = session_fingerprint(hashed)  # this device stays logged in, the others are logged out
    return go(request, "/panel/settings", msg="Password changed. Other devices were logged out.")


# ---------------------------------------------------------------- live board and order list

def _pause_text(venue) -> str | None:
    paused = venue["paused_until"]
    if paused and paused > utcnow().strftime(orders.UTC_FMT):
        return hhmm(orders._parse_utc(paused))
    return None


def _checklist(conn, venue) -> list[tuple[str, bool, str]]:
    items = conn.execute("SELECT COUNT(*) FROM items WHERE venue_id = ? AND is_available = 1", (venue["id"],)).fetchone()[0]
    chats = conn.execute("SELECT COUNT(*) FROM tg_chats WHERE venue_id = ?", (venue["id"],)).fetchone()[0]
    return [("Add your street address", bool(venue["address"]), "/panel/settings"),
            ("Add at least one item to the menu", items > 0, "/panel/menu"),
            ("Wait for approval (we check every new venue)", venue["status"] == "approved", ""),
            ("Connect Telegram for push notifications (optional)", chats > 0 or not cfg.TELEGRAM_BOT_TOKEN, "/panel/settings")]


def _panel_next(form) -> str:
    nxt = safe_next(str(form.get("next", "")), "/panel")
    return nxt if nxt.startswith("/panel") else "/panel"


@route
async def panel_live(request, conn):
    venue = require_venue(request)
    checklist = _checklist(conn, venue)
    return render(request, "panel_live.html", venue=venue, active=orders.live_orders(conn, venue["id"]),
                  paused=_pause_text(venue), checklist=checklist,
                  show_checklist=not all(done for _, done, _ in checklist[:3]))


@route
async def panel_live_fragment(request, conn):
    venue = require_venue(request)
    active = orders.live_orders(conn, venue["id"])
    return render(request, "_live.html", fragment=True, venue=venue, active=active, paused=_pause_text(venue),
                  new_count=sum(o["status"] == "new" for o in active))


@route
async def panel_pause(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    minutes = str(form.get("minutes", "0"))
    if minutes not in ("0", "15", "30", "60"):
        raise HTTPException(400, "Invalid pause length.")
    with conn:
        if minutes == "0":
            conn.execute("UPDATE venues SET paused_until = NULL WHERE id = ?", (venue["id"],))
        else:
            conn.execute("UPDATE venues SET paused_until = datetime('now', ?) WHERE id = ?",
                         (f"+{int(minutes)} minutes", venue["id"]))
    invalidate_index()
    return go(request, "/panel", msg="Orders resumed." if minutes == "0" else f"Orders paused for {minutes} minutes.")


@route
async def panel_orders(request, conn):
    venue = require_venue(request)
    qp = request.query_params
    q = clean(qp.get("q", ""), 60)
    status = qp.get("status", "all")
    period = qp.get("period", "all")
    status = status if status in orders.STATUS_FILTERS else "all"
    period = period if period in orders.PERIODS else "all"
    try:
        page = int(qp.get("page", "1"))
    except ValueError:
        page = 1
    rows, total, pages, page = orders.list_orders(conn, venue["id"], q, status, period, page)

    def page_url(p: int) -> str:
        return "/panel/orders?" + urlencode({"q": q, "status": status, "period": period, "page": p})

    ctx = dict(venue=venue, orders=rows, total=total, pages=pages, page=page, q=q, status=status, period=period,
               page_url=page_url, STATUS_FILTERS=orders.STATUS_FILTERS, PERIODS=orders.PERIODS,
               next_url=page_url(page))
    if qp.get("partial"):
        return render(request, "_order_rows.html", fragment=True, **ctx)
    return render(request, "panel_orders.html", **ctx)


@route
async def order_status(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    order_id = request.path_params["order_id"]
    if conn.execute("SELECT 1 FROM orders WHERE id = ? AND venue_id = ?", (order_id, venue["id"])).fetchone() is None:
        raise HTTPException(404, "Order not found")  # other venues' orders look like they do not exist
    row, error = orders.transition(conn, venue["id"], order_id, str(form.get("status", "")), str(form.get("reason", "")))
    if row:
        await events.announce(order_id)
        return go(request, _panel_next(form))
    return go(request, _panel_next(form), error=error)


async def ws_panel(ws: WebSocket):
    venue = ws_venue(ws) if ws_origin_ok(ws) else None
    if not venue:
        return await ws.close(code=1008)
    await realtime.serve(ws, [f"venue:{venue['id']}"], client_ip(ws))


# ---------------------------------------------------------------- menu

@route
async def panel_menu(request, conn):
    venue = require_venue(request)
    return render(request, "panel_menu.html", venue=venue, menu=load_menu(conn, venue["id"]),
                  groups=load_groups(conn, venue["id"]))


@route
async def add_category(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    name = clean(str(form.get("name", "")), 60)
    if not name:
        return go(request, "/panel/menu")
    existing = [normalize(r["name"]) for r in conn.execute("SELECT name FROM categories WHERE venue_id = ?", (venue["id"],))]
    if len(existing) >= cfg.MAX_CATEGORIES:
        return go(request, "/panel/menu", error=f"You can have at most {cfg.MAX_CATEGORIES} categories.")
    if normalize(name) in existing:
        return go(request, "/panel/menu", error=f"You already have a category called “{name}”.")
    with conn:
        conn.execute("INSERT INTO categories (venue_id, name) VALUES (?, ?)", (venue["id"], name))
    return go(request, "/panel/menu")


@route
async def delete_category(request, conn):
    await read_form(request)
    venue = require_venue(request)
    photos = [r["image"] for r in conn.execute(
        "SELECT i.image FROM items i JOIN categories c ON c.id = i.category_id WHERE c.id = ? AND c.venue_id = ?",
        (request.path_params["category_id"], venue["id"]))]
    with conn:
        conn.execute("DELETE FROM categories WHERE id = ? AND venue_id = ?", (request.path_params["category_id"], venue["id"]))
    for name in photos:
        media.delete_image(name)
    invalidate_index()
    return go(request, "/panel/menu")


@route
async def starter_categories(request, conn):
    await read_form(request)
    venue = require_venue(request)
    if not conn.execute("SELECT 1 FROM categories WHERE venue_id = ?", (venue["id"],)).fetchone():
        with conn:
            conn.executemany("INSERT INTO categories (venue_id, name) VALUES (?, ?)",
                             [(venue["id"], n) for n in ("Coffee", "Tea", "Desserts")])
    return go(request, "/panel/menu")


def _save_variants(conn, item_id: int, prices):
    conn.execute("DELETE FROM variants WHERE item_id = ?", (item_id,))
    conn.executemany("INSERT INTO variants (item_id, label, price) VALUES (?, ?, ?)",
                     [(item_id, label, price) for label, price in prices])


def _item_name_taken(conn, category_id: int, name: str, exclude_id: int = 0) -> bool:
    return any(normalize(r["name"]) == normalize(name) for r in conn.execute(
        "SELECT name FROM items WHERE category_id = ? AND id != ?", (category_id, exclude_id)))


@route
async def add_item(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    try:
        category_id = int(form.get("category_id", ""))
    except ValueError:
        raise HTTPException(404, "Category not found")
    if not conn.execute("SELECT 1 FROM categories WHERE id = ? AND venue_id = ?", (category_id, venue["id"])).fetchone():
        raise HTTPException(404, "Category not found")
    anchor = f"/panel/menu#cat-{category_id}"
    name = clean(str(form.get("name", "")), 80)
    if not name:
        return go(request, anchor, error="Enter an item name.")
    if conn.execute("SELECT COUNT(*) FROM items WHERE venue_id = ?", (venue["id"],)).fetchone()[0] >= cfg.MAX_ITEMS:
        return go(request, anchor, error=f"A venue can have at most {cfg.MAX_ITEMS} items.")
    if _item_name_taken(conn, category_id, name):
        return go(request, anchor, error=f"“{name}” is already in this category. Edit it instead of adding it twice.")
    try:
        parsed = parse_variants([str(x) for x in form.getlist("size_label")], [str(x) for x in form.getlist("size_price")])
    except ValueError as exc:
        return go(request, anchor, error=f"{name}: {exc}")
    with conn:
        item_id = conn.execute("INSERT INTO items (venue_id, category_id, name, description) VALUES (?, ?, ?, ?)",
                               (venue["id"], category_id, name, clean(str(form.get("description", "")), 200))).lastrowid
        _save_variants(conn, item_id, parsed)
        save_item_groups(conn, venue["id"], item_id, form)
    invalidate_index()
    return go(request, anchor)


@route
async def update_item(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    item_id = request.path_params["item_id"]
    item = conn.execute("SELECT category_id FROM items WHERE id = ? AND venue_id = ?", (item_id, venue["id"])).fetchone()
    if not item:
        raise HTTPException(404, "Item not found")
    anchor = f"/panel/menu#cat-{item['category_id']}"
    name = clean(str(form.get("name", "")), 80)
    if not name:
        return go(request, anchor, error="Enter an item name.")
    if _item_name_taken(conn, item["category_id"], name, item_id):
        return go(request, anchor, error=f"Another item in this category is already called “{name}”.")
    try:
        parsed = parse_variants([str(x) for x in form.getlist("size_label")], [str(x) for x in form.getlist("size_price")])
    except ValueError as exc:
        return go(request, anchor, error=f"{name}: {exc}")
    with conn:
        conn.execute("UPDATE items SET name = ?, description = ? WHERE id = ?",
                     (name, clean(str(form.get("description", "")), 200), item_id))
        _save_variants(conn, item_id, parsed)
        save_item_groups(conn, venue["id"], item_id, form)
    invalidate_index()
    return go(request, anchor)


@route
async def toggle_item(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("UPDATE items SET is_available = 1 - is_available WHERE id = ? AND venue_id = ?",
                     (request.path_params["item_id"], venue["id"]))
    invalidate_index()
    return go(request, "/panel/menu")


@route
async def delete_item(request, conn):
    await read_form(request)
    venue = require_venue(request)
    row = conn.execute("SELECT image FROM items WHERE id = ? AND venue_id = ?", (request.path_params["item_id"], venue["id"])).fetchone()
    with conn:
        conn.execute("DELETE FROM items WHERE id = ? AND venue_id = ?", (request.path_params["item_id"], venue["id"]))
    if row:
        media.delete_image(row["image"])
    invalidate_index()
    return go(request, "/panel/menu")


# ---------------------------------------------------------------- settings, customers, Telegram

@route
async def panel_settings(request, conn):
    venue = require_venue(request)
    code = conn.execute("SELECT code FROM tg_link_codes WHERE venue_id = ? AND expires_at > datetime('now')",
                        (venue["id"],)).fetchone()
    chats = conn.execute("SELECT * FROM tg_chats WHERE venue_id = ? ORDER BY id", (venue["id"],)).fetchall()
    flags = conn.execute(
        "SELECT f.*, u.name, u.email FROM venue_customer_flags f JOIN users u ON u.id = f.user_id "
        "WHERE f.venue_id = ? AND f.warnings > 0 ORDER BY f.blocked DESC, u.name", (venue["id"],)).fetchall()
    link = None
    if code and cfg.TELEGRAM_BOT_USERNAME:
        link = f"https://t.me/{cfg.TELEGRAM_BOT_USERNAME}?start={code['code']}"
    return render(request, "panel_settings.html", venue=venue, chats=chats, tg_link=link, flags=flags,
                  tg_enabled=bool(cfg.TELEGRAM_BOT_TOKEN), tg_ready=bool(cfg.TELEGRAM_BOT_USERNAME),
                  PREP_CHOICES=PREP_CHOICES, max_no_shows=cfg.MAX_NO_SHOWS, grace=cfg.NO_SHOW_GRACE_MIN)


@route
async def change_email(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    _throttle(f"vemail:{venue['id']}", 5, 3600)
    new = clean(str(form.get("email", "")), 254).lower()
    if not await verify_async(str(form.get("password", ""))[:129], venue["password_hash"]):
        return go(request, "/panel/settings", error="Wrong password. The e-mail was not changed.")
    if not valid_email(new):
        return go(request, "/panel/settings", error="Enter a valid e-mail address.")
    if new == venue["email"]:
        return go(request, "/panel/settings", error="This is already your e-mail.")
    if conn.execute("SELECT 1 FROM venues WHERE email = ?", (new,)).fetchone():
        return go(request, "/panel/settings", error="This e-mail is already used by another venue.")
    with conn:
        conn.execute("UPDATE venues SET email = ? WHERE id = ?", (new, venue["id"]))
    send_mail_bg(venue["email"], "Your venue login e-mail was changed",
                 f"The login e-mail of {venue['name']} was changed to {new}. "
                 "If this was not you, contact us at once and reset your password.")
    return go(request, "/panel/settings", msg="E-mail changed. Use the new address to log in.")


@route
async def save_settings(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    name = clean(str(form.get("name", "")), 80)
    open_t, close_t = str(form.get("open_time", "")).strip(), str(form.get("close_time", "")).strip()
    try:
        prep, max_active = int(form.get("prep_minutes", "")), int(form.get("max_active", ""))
        slot_cap = int(str(form.get("slot_capacity", "0")).strip() or 0)
    except ValueError:
        return go(request, "/panel/settings", error="Preparation time and order limit must be numbers.")
    if len(name) < 2:
        return go(request, "/panel/settings", error="Enter the venue name.")
    if parse_hhmm(open_t) is None or parse_hhmm(close_t) is None:
        return go(request, "/panel/settings", error="Enter opening hours like 08:00 and 18:00.")
    if prep not in PREP_CHOICES:
        return go(request, "/panel/settings", error="Choose a preparation time from the list.")
    if not 1 <= max_active <= 100:
        return go(request, "/panel/settings", error="The order limit must be between 1 and 100.")
    if not 0 <= slot_cap <= 50:
        return go(request, "/panel/settings", error="Orders per time slot must be between 0 and 50.")
    with conn:
        conn.execute(
            "UPDATE venues SET name = ?, city = ?, address = ?, description = ?, accepting_orders = ?, "
            "open_time = ?, close_time = ?, prep_minutes = ?, max_active = ?, slot_capacity = ? WHERE id = ?",
            (name, clean(str(form.get("city", "")), 60), clean(str(form.get("address", "")), 120),
             clean(str(form.get("description", "")), 300), 1 if form.get("accepting_orders") else 0,
             open_t, close_t, prep, max_active, slot_cap, venue["id"]))
    invalidate_index()
    return go(request, "/panel/settings", msg="Saved.")


@route
async def reset_customer(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("DELETE FROM venue_customer_flags WHERE venue_id = ? AND user_id = ?",
                     (venue["id"], request.path_params["user_id"]))
    return go(request, "/panel/settings", msg="Customer unblocked.")


@route
async def telegram_link(request, conn):
    await read_form(request)
    venue = require_venue(request)
    if not cfg.TELEGRAM_BOT_TOKEN:
        raise HTTPException(404, "Telegram is not configured.")
    if venue["status"] != "approved":
        return go(request, "/panel/settings", error="Telegram can be connected once your venue is approved.")
    _throttle(f"tg-code:{venue['id']}", 10, 3600)
    with conn:
        conn.execute("DELETE FROM tg_link_codes WHERE venue_id = ?", (venue["id"],))
        conn.execute("INSERT INTO tg_link_codes (code, venue_id, expires_at) VALUES (?, ?, datetime('now', '+15 minutes'))",
                     (orders.new_link_code(), venue["id"]))
    return go(request, "/panel/settings")


@route
async def telegram_disconnect(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("DELETE FROM tg_chats WHERE id = ? AND venue_id = ?", (request.path_params["chat_pk"], venue["id"]))
    return go(request, "/panel/settings", msg="Chat disconnected.")
