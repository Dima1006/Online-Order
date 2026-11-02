"""Order rules shared by the website and the Telegram bot."""
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config as cfg
from .utils import normalize

WARSAW = ZoneInfo("Europe/Warsaw")
ARRIVAL_CHOICES = (5, 10, 15, 30, 45, 60)

STATUS_LABELS = {
    "new": "New", "preparing": "Preparing", "ready": "Ready for pickup",
    "completed": "Picked up", "declined": "Declined", "no_show": "No-show",
}
ACTIVE_STATUSES = ("new", "preparing", "ready")
# status -> statuses the venue may move it to
ALLOWED = {
    "new": ("preparing", "no_show", "declined"),
    "preparing": ("ready", "no_show", "declined"),
    "ready": ("completed", "no_show", "declined"),
}
ACTION_LABELS = {
    "preparing": "Start preparing", "ready": "Mark ready", "completed": "Picked up",
    "no_show": "No-show", "declined": "Decline",
}


def _local(created: str) -> datetime:
    return datetime.fromisoformat(created).replace(tzinfo=timezone.utc).astimezone(WARSAW)


def hydrate(conn: sqlite3.Connection, rows) -> list[dict]:
    orders = [dict(r) for r in rows]
    if not orders:
        return orders
    ids = [o["id"] for o in orders]
    items: dict[int, list] = {i: [] for i in ids}
    for it in conn.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
        items[it["order_id"]].append(it)
    for o in orders:
        created = _local(o["created_at"])
        o["items"] = items[o["id"]]
        o["created_local"] = created.strftime("%H:%M")
        o["created_date"] = created.strftime("%d.%m.%Y")
        o["arrival_local"] = (created + timedelta(minutes=o["arrival_minutes"])).strftime("%H:%M")
        o["status_label"] = STATUS_LABELS[o["status"]]
    return orders


def create_order(conn, venue, user, arrival_minutes: int, note: str, cart_json: str):
    """Validate a cart and store the order. Returns (order_id, None) or (None, error message).
    Only variant ids and quantities are read from the browser: prices always come from the database."""
    if venue["status"] != "approved" or not venue["accepting_orders"]:
        return None, "This venue is not accepting orders right now."
    if user["is_blocked"]:
        return None, "Your account is blocked after repeated no-shows. Please contact the venue."
    if arrival_minutes not in ARRIVAL_CHOICES:
        return None, "Choose when you will arrive."
    wanted: dict[int, int] = {}
    try:
        for line in json.loads(cart_json)[:50]:
            vid, qty = int(line["variant_id"]), int(line["qty"])
            if 1 <= qty <= 20:
                wanted[vid] = min(20, wanted.get(vid, 0) + qty)
    except (ValueError, TypeError, KeyError):
        return None, "Your cart could not be read. Please try again."
    if not wanted:
        return None, "Your cart is empty."
    rows = conn.execute(
        f"""SELECT v.id, v.label, v.price, i.name, i.is_available
            FROM variants v JOIN items i ON i.id = v.item_id
            WHERE i.venue_id = ? AND v.id IN ({','.join('?' * len(wanted))})""",
        (venue["id"], *wanted)).fetchall()
    if len(rows) != len(wanted) or not all(r["is_available"] for r in rows):
        return None, "Some items in your cart are no longer available. Please review your order."
    total = sum(r["price"] * wanted[r["id"]] for r in rows)
    with conn:
        order_id = conn.execute(
            "INSERT INTO orders (venue_id, user_id, customer_name, phone, arrival_minutes, note, total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (venue["id"], user["id"], user["name"], user["phone"], arrival_minutes, note, total)).lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, name, variant_label, price, qty) VALUES (?, ?, ?, ?, ?)",
            [(order_id, r["name"], r["label"], r["price"], wanted[r["id"]]) for r in rows])
    return order_id, None


def transition(conn, venue_id: int, order_id: int, new_status: str, reason: str = ""):
    """Move an order to a new status if the rules allow it. Returns the order row, or None.
    A no-show adds a warning to the customer; after MAX_NO_SHOWS they are blocked."""
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND venue_id = ?", (order_id, venue_id)).fetchone()
    if not order or new_status not in ALLOWED.get(order["status"], ()):
        return None
    if new_status == "declined":
        reason = (reason.strip()[:200] or "Declined by the venue")
    else:
        reason = ""
    with conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, decline_reason = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = ?", (new_status, reason, order_id, order["status"]))
        if cur.rowcount != 1:  # somebody else (web or Telegram) was faster
            return None
        if new_status == "no_show" and order["user_id"]:
            conn.execute(
                "UPDATE users SET warning_count = warning_count + 1, "
                "is_blocked = CASE WHEN warning_count + 1 >= ? THEN 1 ELSE is_blocked END WHERE id = ?",
                (cfg.MAX_NO_SHOWS, order["user_id"]))
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


# ---------------------------------------------------------------- lists and search

PERIODS = {"today": "Today", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}
STATUS_FILTERS = {"active": "Active", "all": "All", **{k: v for k, v in STATUS_LABELS.items()}}
PER_PAGE = 15


def _period_start(period: str) -> str | None:
    now = datetime.now(WARSAW)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period in ("7d", "30d"):
        start = now - timedelta(days=int(period[:-1]))
    else:
        return None
    return start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_orders(conn, venue_id: int, q: str = "", status: str = "all", period: str = "all", page: int = 1):
    """Filtered, paginated orders of one venue. Returns (orders, total, pages, page)."""
    where, args = ["o.venue_id = ?"], [venue_id]
    if status == "active":
        where.append("o.status IN ('new', 'preparing', 'ready')")
    elif status in STATUS_LABELS:
        where.append("o.status = ?")
        args.append(status)
    start = _period_start(period)
    if start:
        where.append("o.created_at >= ?")
        args.append(start)
    q = q.strip()
    if q:
        m = re.fullmatch(r"#?(\d{1,9})", q)
        like = "%" + _like_escape(normalize(q)) + "%"
        clause = ("norm(o.customer_name) LIKE ? ESCAPE '\\' OR o.phone LIKE ? ESCAPE '\\' OR EXISTS "
                  "(SELECT 1 FROM order_items oi WHERE oi.order_id = o.id AND norm(oi.name) LIKE ? ESCAPE '\\')")
        cargs = [like, "%" + _like_escape(q) + "%", like]
        if m:
            clause += " OR o.id = ?"
            cargs.append(int(m.group(1)))
        where.append("(" + clause + ")")
        args += cargs
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM orders o WHERE {sql_where}", args).fetchone()[0]
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 1), pages)
    rows = conn.execute(
        f"SELECT o.* FROM orders o WHERE {sql_where} ORDER BY o.id DESC LIMIT ? OFFSET ?",
        (*args, PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
    return hydrate(conn, rows), total, pages, page


def live_orders(conn, venue_id: int):
    rows = conn.execute(
        "SELECT * FROM orders WHERE venue_id = ? AND status IN ('new', 'preparing', 'ready') "
        "ORDER BY datetime(created_at, '+' || arrival_minutes || ' minutes')", (venue_id,)).fetchall()
    return hydrate(conn, rows)


def user_orders(conn, user_id: int, limit: int = 50):
    rows = conn.execute(
        "SELECT o.*, v.name AS venue_name, v.slug AS venue_slug FROM orders o "
        "JOIN venues v ON v.id = o.venue_id WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
        (user_id, limit)).fetchall()
    return hydrate(conn, rows)


def new_link_code() -> str:
    return secrets.token_urlsafe(9)
