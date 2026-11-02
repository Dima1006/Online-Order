"""Venue side: registration, login and the panel (live orders, order list, menu, settings, Telegram)."""
import re
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.websockets import WebSocket

from . import config as cfg
from . import events, orders, realtime
from .core import go, login_session, read_form, render, require_venue, route, ws_venue
from .mail import public_url, send_mail
from .security import client_ip, limiter, ws_origin_ok
from .utils import (clean, dummy_hash, hash_password, parse_variants, password_problem, safe_next, slugify,
                    valid_email, verify_password)
from .web_public import load_menu

TOO_MANY = "Too many attempts. Please wait a few minutes and try again."


def _throttle(key: str, limit: int, window: float) -> None:
    if not limiter.allow(key, limit, window):
        raise HTTPException(429, TOO_MANY)


def unique_slug(conn, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM venues WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------- registration and login

@route
async def venue_register(request, conn):
    if request.method == "GET":
        return render(request, "venue_register.html", form={})
    form = await read_form(request)
    _throttle(f"venue-register-ip:{client_ip(request)}", 3, 3600)  # stops mass-creating fake venues
    name, city = clean(str(form.get("venue_name", "")), 80), clean(str(form.get("city", "")), 60)
    email = clean(str(form.get("email", "")), 254).lower()
    password = str(form.get("password", ""))
    error = None
    if not name:
        error = "Enter the venue name."
    elif not valid_email(email):
        error = "Enter a valid email address."
    elif (problem := password_problem(password, email)):
        error = problem
    elif conn.execute("SELECT 1 FROM venues WHERE email = ?", (email,)).fetchone():
        error = "This email is already registered."
    if error:
        return render(request, "venue_register.html", 400, error=error,
                      form={"venue_name": name, "city": city, "email": email})
    hashed = await run_in_threadpool(hash_password, password)
    with conn:
        vid = conn.execute("INSERT INTO venues (slug, name, city, email, password_hash) VALUES (?, ?, ?, ?, ?)",
                           (unique_slug(conn, name), name, city, email, hashed)).lastrowid
    venue = conn.execute("SELECT * FROM venues WHERE id = ?", (vid,)).fetchone()
    if cfg.ADMIN_EMAIL:
        await send_mail(cfg.ADMIN_EMAIL, f"New venue to approve: {name}",
                        f"A venue registered and is waiting for your approval.\n\nName: {name}\nCity: {city}\n"
                        f"Owner email: {email}\n\nReview it here: {public_url('/admin', request)}")
    login_session(request, venue=venue)
    return go(request, "/panel/settings",
              msg="Venue created. Fill in the address and menu while we review your venue: it becomes public once approved.")


@route
async def venue_login(request, conn):
    if request.method == "GET":
        return render(request, "venue_login.html", email="")
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    password = str(form.get("password", ""))[:129]
    _throttle(f"vlogin-ip:{client_ip(request)}", 20, 600)
    _throttle(f"vlogin-email:{email}", 8, 600)
    venue = conn.execute("SELECT * FROM venues WHERE email = ?", (email,)).fetchone()
    ok = await run_in_threadpool(verify_password, password, venue["password_hash"] if venue else dummy_hash())
    if not (venue and ok):
        return render(request, "venue_login.html", 400, email=email, error="Wrong email or password.")
    login_session(request, venue=venue)
    return go(request, "/panel")


# ---------------------------------------------------------------- live board and order list

def _panel_next(form) -> str:
    nxt = safe_next(str(form.get("next", "")), "/panel")
    return nxt if nxt.startswith("/panel") else "/panel"


@route
async def panel_live(request, conn):
    venue = require_venue(request)
    return render(request, "panel_live.html", venue=venue, active=orders.live_orders(conn, venue["id"]))


@route
async def panel_live_fragment(request, conn):
    venue = require_venue(request)
    active = orders.live_orders(conn, venue["id"])
    return render(request, "_live.html", fragment=True, venue=venue, active=active,
                  new_count=sum(o["status"] == "new" for o in active))


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
    try:
        order_id = int(request.path_params["order_id"])
    except ValueError:
        raise HTTPException(404, "Order not found")
    new_status = str(form.get("status", ""))
    if conn.execute("SELECT 1 FROM orders WHERE id = ? AND venue_id = ?", (order_id, venue["id"])).fetchone() is None:
        raise HTTPException(404, "Order not found")  # other venues' orders look like they do not exist
    if orders.transition(conn, venue["id"], order_id, new_status, str(form.get("reason", ""))):
        await events.announce(order_id)
    return go(request, _panel_next(form))


async def ws_panel(ws: WebSocket):
    venue = ws_venue(ws) if ws_origin_ok(ws) else None
    if not venue:
        return await ws.close(code=1008)
    await realtime.serve(ws, [f"venue:{venue['id']}"])


# ---------------------------------------------------------------- menu

@route
async def panel_menu(request, conn):
    venue = require_venue(request)
    return render(request, "panel_menu.html", venue=venue, menu=load_menu(conn, venue["id"]))


@route
async def add_category(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    name = clean(str(form.get("name", "")), 60)
    if name:
        with conn:
            conn.execute("INSERT INTO categories (venue_id, name) VALUES (?, ?)", (venue["id"], name))
    return go(request, "/panel/menu")


@route
async def delete_category(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("DELETE FROM categories WHERE id = ? AND venue_id = ?", (request.path_params["category_id"], venue["id"]))
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
    try:
        parsed = parse_variants([str(x) for x in form.getlist("size_label")], [str(x) for x in form.getlist("size_price")])
    except ValueError as exc:
        return go(request, anchor, error=f"{name}: {exc}")
    with conn:
        item_id = conn.execute("INSERT INTO items (venue_id, category_id, name, description) VALUES (?, ?, ?, ?)",
                               (venue["id"], category_id, name, clean(str(form.get("description", "")), 200))).lastrowid
        _save_variants(conn, item_id, parsed)
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
    try:
        parsed = parse_variants([str(x) for x in form.getlist("size_label")], [str(x) for x in form.getlist("size_price")])
    except ValueError as exc:
        return go(request, anchor, error=f"{name}: {exc}")
    with conn:
        conn.execute("UPDATE items SET name = ?, description = ? WHERE id = ?",
                     (name, clean(str(form.get("description", "")), 200), item_id))
        _save_variants(conn, item_id, parsed)
    return go(request, anchor)


@route
async def toggle_item(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("UPDATE items SET is_available = 1 - is_available WHERE id = ? AND venue_id = ?",
                     (request.path_params["item_id"], venue["id"]))
    return go(request, "/panel/menu")


@route
async def delete_item(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("DELETE FROM items WHERE id = ? AND venue_id = ?", (request.path_params["item_id"], venue["id"]))
    return go(request, "/panel/menu")


# ---------------------------------------------------------------- settings and Telegram

@route
async def panel_settings(request, conn):
    venue = require_venue(request)
    code = conn.execute("SELECT code FROM tg_link_codes WHERE venue_id = ? AND expires_at > datetime('now')",
                        (venue["id"],)).fetchone()
    chats = conn.execute("SELECT * FROM tg_chats WHERE venue_id = ? ORDER BY id", (venue["id"],)).fetchall()
    link = None
    if code and cfg.TELEGRAM_BOT_USERNAME:
        link = f"https://t.me/{cfg.TELEGRAM_BOT_USERNAME}?start={code['code']}"
    return render(request, "panel_settings.html", venue=venue, chats=chats, tg_link=link,
                  tg_enabled=bool(cfg.TELEGRAM_BOT_TOKEN), tg_ready=bool(cfg.TELEGRAM_BOT_USERNAME))


@route
async def save_settings(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    name = clean(str(form.get("name", "")), 80) or venue["name"]
    with conn:
        conn.execute("UPDATE venues SET name = ?, city = ?, address = ?, description = ?, accepting_orders = ? WHERE id = ?",
                     (name, clean(str(form.get("city", "")), 60), clean(str(form.get("address", "")), 120),
                      clean(str(form.get("description", "")), 300), 1 if form.get("accepting_orders") else 0, venue["id"]))
    return go(request, "/panel/settings", msg="Saved.")


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
