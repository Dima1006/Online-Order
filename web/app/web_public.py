"""Pages everybody can see: search, venue menu, placing an order."""
import json
import secrets
import sqlite3
import time

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse

from . import config as cfg
from . import events, orders
from .core import admin_ok, current_user, current_venue, go, read_form, render, route
from .security import limiter
from .utils import clean, normalize

SEARCH_LIMIT = 50
INDEX_TTL = 15  # seconds; menu and approval changes call invalidate_index() so they show up at once

_index: dict = {"at": 0.0, "venues": [], "dishes": {}}


def invalidate_index() -> None:
    _index["at"] = 0.0


def _load_index(conn: sqlite3.Connection) -> None:
    """One pass over the approved venues and their dishes, kept in memory for a few seconds,
    so typing in the search box does not hit the database on every keystroke."""
    venues = conn.execute(
        """SELECT v.*, (SELECT COUNT(*) FROM items i WHERE i.venue_id = v.id AND i.is_available = 1) AS item_count
           FROM venues v WHERE v.status = 'approved'""").fetchall()
    dishes: dict[int, list[tuple[str, str]]] = {}
    for r in conn.execute(
            "SELECT i.venue_id, i.name AS item, c.name AS category FROM items i "
            "JOIN categories c ON c.id = i.category_id WHERE i.is_available = 1"):
        dishes.setdefault(r["venue_id"], []).append((r["item"], normalize(r["item"] + " " + r["category"])))
    _index.update(at=time.monotonic(), venues=[
        (dict(v), normalize(" ".join((v["name"], v["city"], v["address"], v["description"]))), normalize(v["name"]))
        for v in venues if v["item_count"]], dishes=dishes)


def search_venues(conn: sqlite3.Connection, q: str):
    """Approved venues matching every word of the query against venue fields, categories and dish names."""
    if time.monotonic() - _index["at"] > INDEX_TTL:
        _load_index(conn)
    tokens = normalize(q).split()[:8]
    results = []
    for v, own, name_norm in _index["venues"]:
        matched, ok = [], True
        for token in tokens:
            if token in own:
                continue
            hits = [name for name, text in _index["dishes"].get(v["id"], []) if token in text]
            if not hits:
                ok = False
                break
            matched.extend(hits)
        if ok:
            is_open, state = orders.venue_state(v)  # depends on the clock, so never cached
            results.append((-sum(1 for t in tokens if t in name_norm), not is_open, name_norm,
                            dict(v, matched=list(dict.fromkeys(matched))[:3], is_open=is_open, state=state)))
    results.sort(key=lambda r: r[:3])
    return [r[3] for r in results[:SEARCH_LIMIT]]


@route
async def home(request, conn):
    q = clean(request.query_params.get("q", ""), 60)
    venues = search_venues(conn, q)
    if request.query_params.get("partial"):
        return render(request, "_venues.html", fragment=True, q=q, venues=venues)
    return render(request, "home.html", q=q, venues=venues)


def load_menu(conn: sqlite3.Connection, venue_id: int):
    """Categories -> items -> variants for one venue."""
    categories = [dict(c, items=[]) for c in conn.execute(
        "SELECT * FROM categories WHERE venue_id = ? ORDER BY id", (venue_id,))]
    by_cat = {c["id"]: c for c in categories}
    items = {}
    for row in conn.execute("SELECT * FROM items WHERE venue_id = ? ORDER BY id", (venue_id,)):
        item = dict(row, variants=[])
        items[row["id"]] = item
        by_cat[row["category_id"]]["items"].append(item)
    for v in conn.execute(
            "SELECT v.* FROM variants v JOIN items i ON i.id = v.item_id WHERE i.venue_id = ? ORDER BY v.id",
            (venue_id,)):
        items[v["item_id"]]["variants"].append(v)
    return categories


def visible_venue(request, conn, slug: str):
    """Approved venues are public; any other venue is visible only to its owner and the admin (preview)."""
    venue = conn.execute("SELECT * FROM venues WHERE slug = ?", (slug,)).fetchone()
    owner = current_venue(request)
    if not venue or (venue["status"] != "approved" and not admin_ok(request)
                     and not (owner and owner["id"] == venue["id"])):
        raise HTTPException(404, "Venue not found")
    return venue


def venue_context(request, conn, venue, form=None, reorder=None):
    menu = [c for c in load_menu(conn, venue["id"]) if c["items"]]
    ok, why = orders.venue_availability(venue)
    options = orders.arrival_options(venue) if ok else []
    if ok and not options:
        why = f"Too late to order for today. Opening hours: {orders.hours_text(venue)}."
    menu_data = {str(v["id"]): {"name": i["name"], "label": v["label"], "price": v["price"]}
                 for c in menu for i in c["items"] if i["is_available"] for v in i["variants"]}
    user = current_user(request)
    minutes = [m for m, _ in options]
    wanted = (form or {}).get("arrival")
    default_arrival = wanted if wanted in minutes else (15 if 15 in minutes else (minutes[0] if minutes else 0))
    return dict(venue=venue, menu=menu, menu_data=menu_data, form=form or {},
                can_order=ok and bool(options), closed_reason=why, arrival_options=options,
                default_arrival=default_arrival, submit_key=secrets.token_urlsafe(16),
                hours=orders.hours_text(venue), reorder=reorder,
                needs_verification=bool(user and not user["email_verified"] and not cfg.DEMO_MODE))


@route
async def venue_page(request, conn):
    venue = visible_venue(request, conn, request.path_params["slug"])
    reorder = None
    user = current_user(request)
    raw = request.query_params.get("reorder", "")
    if user and raw.isdigit():
        reorder = orders.reorder_cart(conn, venue["id"], user["id"], int(raw)) or None
    return render(request, "venue.html", **venue_context(request, conn, venue, reorder=reorder))


@route
async def place_order(request, conn):
    venue = visible_venue(request, conn, request.path_params["slug"])
    form = await read_form(request)
    user = current_user(request)
    if not user:
        return go(request, f"/login?next=/v/{venue['slug']}", error="Log in to place your order. Your cart is saved.")
    if not limiter.allow(f"order:{user['id']}", 10, 600):
        raise HTTPException(429, "Too many orders in a short time. Please wait a few minutes.")
    try:
        arrival = int(form.get("arrival_minutes", ""))
    except ValueError:
        arrival = -1
    note = clean(str(form.get("note", "")), 300)
    prefill = {"arrival": arrival, "note": note}

    def fail(message: str):
        ctx = venue_context(request, conn, venue, prefill)
        request.session["flash"] = [["error", message]]
        return render(request, "venue.html", 400, **ctx)

    if not user["email_verified"] and not cfg.DEMO_MODE:
        return fail("Please confirm your email address first (we sent you a link). You can resend it in My account.")
    order_id, error = orders.create_order(conn, venue, user, arrival, note, str(form.get("cart", "[]")),
                                          str(form.get("submit_key", "")))
    if error:
        return fail(error)
    await events.announce(order_id, new=True)
    return RedirectResponse(f"/order/{order_id}", status_code=303)
