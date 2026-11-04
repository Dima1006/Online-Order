"""Order rules shared by the website and the Telegram bot. Every rule lives here, once."""
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config as cfg
from .utils import format_zl, is_open_at, normalize, parse_hhmm

WARSAW = ZoneInfo("Europe/Warsaw")
ARRIVAL_CHOICES = (5, 10, 15, 20, 30, 45, 60)

STATUS_LABELS = {
    "new": "New", "preparing": "Preparing", "ready": "Ready for pickup", "completed": "Picked up",
    "declined": "Declined", "no_show": "No-show", "cancelled": "Cancelled", "expired": "Expired",
}
ACTIVE_STATUSES = ("new", "preparing", "ready")
_ACTIVE_SQL = "('new', 'preparing', 'ready')"
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
UTC_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def hhmm(dt: datetime) -> str:
    return dt.astimezone(WARSAW).strftime("%H:%M")


# ---------------------------------------------------------------- is the venue open for orders?

def minutes_of_day(now: datetime) -> int:
    local = now.astimezone(WARSAW)
    return local.hour * 60 + local.minute


def hours_text(venue) -> str:
    if venue["open_time"] == venue["close_time"]:
        return "open all day"
    return f"{venue['open_time']}–{venue['close_time']}"


def venue_availability(venue, now: datetime | None = None) -> tuple[bool, str]:
    """(can order right now?, reason shown to the customer)."""
    now = now or utcnow()
    if venue["status"] != "approved" or not venue["accepting_orders"]:
        return False, "This venue is not accepting orders right now."
    if venue["paused_until"] and venue["paused_until"] > now.strftime(UTC_FMT):
        return False, f"Orders are paused until {hhmm(_parse_utc(venue['paused_until']))}. Please try again then."
    o, c = parse_hhmm(venue["open_time"]), parse_hhmm(venue["close_time"])
    if o is not None and c is not None and not is_open_at(o, c, minutes_of_day(now)):
        return False, f"Closed right now. Opening hours: {hours_text(venue)}."
    return True, ""


def arrival_options(venue, now: datetime | None = None) -> list[tuple[int, str]]:
    """Arrival choices a customer may pick: not earlier than the venue's preparation time and inside opening hours."""
    now = now or utcnow()
    o, c = parse_hhmm(venue["open_time"]), parse_hhmm(venue["close_time"])
    result = []
    for m in ARRIVAL_CHOICES:
        if m < venue["prep_minutes"]:
            continue
        at = now + timedelta(minutes=m)
        if o is not None and c is not None and not is_open_at(o, c, minutes_of_day(at)):
            continue
        result.append((m, hhmm(at)))
    return result


def venue_state(venue, now: datetime | None = None) -> tuple[bool, str]:
    """For lists: (open?, short label)."""
    ok, why = venue_availability(venue, now)
    if ok:
        return True, "Taking orders"
    if venue["status"] == "approved" and venue["accepting_orders"]:
        return False, "Paused" if "paused" in why else f"Closed · {hours_text(venue)}"
    return False, "Closed for orders"


# ---------------------------------------------------------------- loading orders

def hydrate(conn: sqlite3.Connection, rows) -> list[dict]:
    orders = [dict(r) for r in rows]
    if not orders:
        return orders
    ids = [o["id"] for o in orders]
    items: dict[int, list] = {i: [] for i in ids}
    for it in conn.execute(
            f"SELECT * FROM order_items WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
        items[it["order_id"]].append(it)
    now = utcnow()
    for o in orders:
        created, arrive = _parse_utc(o["created_at"]), _parse_utc(o["arrive_at"])
        o["items"] = items[o["id"]]
        o["created_local"] = hhmm(created)
        o["created_date"] = created.astimezone(WARSAW).strftime("%d.%m.%Y")
        o["arrival_local"] = hhmm(arrive)
        o["status_label"] = STATUS_LABELS[o["status"]]
        active = o["status"] in ACTIVE_STATUSES
        o["overdue"] = active and arrive < now
        no_show_from = arrive + timedelta(minutes=cfg.NO_SHOW_GRACE_MIN)
        o["can_no_show"] = no_show_from <= now
        o["no_show_from"] = hhmm(no_show_from)
        o["can_cancel"] = o["status"] == "new"
    return orders


# ---------------------------------------------------------------- placing an order

def customer_flag(conn, venue_id: int, user_id: int):
    return conn.execute("SELECT * FROM venue_customer_flags WHERE venue_id = ? AND user_id = ?",
                        (venue_id, user_id)).fetchone()


def create_order(conn, venue, user, arrival_minutes: int, note: str, cart_json: str, submit_key: str = ""):
    """Validate everything and store the order. Returns (order_id, None) or (None, message for the customer).
    Only variant ids and quantities are read from the browser: prices always come from the database.
    This function never awaits, so in a single-process server the checks and the INSERT cannot interleave."""
    now = utcnow()
    submit_key = submit_key[:64] if re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", submit_key or "") else ""
    if submit_key:  # the same form sent twice (double click, reload, retry): answer with the first order
        existing = conn.execute("SELECT id FROM orders WHERE submit_key = ? AND user_id = ?",
                                (submit_key, user["id"])).fetchone()
        if existing:
            return existing["id"], None
    ok, why = venue_availability(venue, now)
    if not ok:
        return None, why
    flag = customer_flag(conn, venue["id"], user["id"])
    if flag and flag["blocked"]:
        return None, "This venue blocked your orders after repeated no-shows. Please contact the venue."
    if arrival_minutes not in dict(arrival_options(venue, now)):
        return None, "Please choose one of the available arrival times."

    active_mine = conn.execute(f"SELECT COUNT(*) FROM orders WHERE user_id = ? AND status IN {_ACTIVE_SQL}",
                               (user["id"],)).fetchone()[0]
    active_here = conn.execute(f"SELECT COUNT(*) FROM orders WHERE user_id = ? AND venue_id = ? AND status IN {_ACTIVE_SQL}",
                               (user["id"], venue["id"])).fetchone()[0]
    if active_here >= cfg.MAX_ACTIVE_PER_USER_VENUE:
        return None, (f"You already have {active_here} active orders here. "
                      "Wait until they are picked up, or cancel one that is still “New”.")
    if active_mine >= cfg.MAX_ACTIVE_PER_USER:
        return None, "You have too many active orders. Finish or cancel one first."
    busy = conn.execute(f"SELECT COUNT(*) FROM orders WHERE venue_id = ? AND status IN {_ACTIVE_SQL}",
                        (venue["id"],)).fetchone()[0]
    if busy >= venue["max_active"]:
        return None, "This venue is very busy right now. Please try again in a few minutes."
    last_hour = conn.execute("SELECT COUNT(*) FROM orders WHERE venue_id = ? AND created_at >= ?",
                             (venue["id"], (now - timedelta(hours=1)).strftime(UTC_FMT))).fetchone()[0]
    if last_hour >= cfg.VENUE_ORDERS_PER_HOUR:
        return None, "This venue received too many orders in the last hour. Please try again later."

    wanted: dict[tuple, int] = {}   # (variant id, sorted add-on ids) -> quantity
    try:
        lines = json.loads(cart_json)
        if not isinstance(lines, list):
            raise ValueError
        for line in lines[:cfg.MAX_CART_LINES + 1]:
            vid, qty = int(line["variant_id"]), int(line["qty"])
            raw = line.get("options") or []
            if not isinstance(raw, list) or len(raw) > 20:
                raise ValueError
            key = (vid, tuple(sorted({int(x) for x in raw})))
            if 1 <= qty <= 20:
                wanted[key] = min(20, wanted.get(key, 0) + qty)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, "Your cart could not be read. Please try again."
    if not wanted:
        return None, "Your cart is empty."
    if len(wanted) > cfg.MAX_CART_LINES:
        return None, f"An order can have at most {cfg.MAX_CART_LINES} different items."
    vids = sorted({k[0] for k in wanted})
    rows = {r["id"]: r for r in conn.execute(
        f"""SELECT v.id, v.label, v.price, i.id AS item_id, i.name, i.is_available
            FROM variants v JOIN items i ON i.id = v.item_id
            WHERE i.venue_id = ? AND v.id IN ({','.join('?' * len(vids))})""",
        (venue["id"], *vids))}
    stale = "Some items in your cart are no longer available or their prices changed. Please review your order."
    lines_to_save, total = [], 0
    for (vid, oids), qty in wanted.items():
        r = rows.get(vid)
        if not r or not r["is_available"]:
            return None, stale
        checked = check_options(conn, venue["id"], r["item_id"], oids)
        if checked is None:
            return None, "Some choices (milk, syrup...) are missing or no longer available. Please review your order."
        names, extra = checked
        unit = r["price"] + extra
        total += unit * qty
        shown = ", ".join([x for x in [r["label"], *names] if x])
        lines_to_save.append((r["name"], shown, r["label"], ",".join(map(str, oids)), unit, qty))
    if total > cfg.MAX_ORDER_TOTAL:
        return None, f"An order can be at most {format_zl(cfg.MAX_ORDER_TOTAL)}. Please split it or call the venue."
    try:
        with conn:
            order_id = conn.execute(
                "INSERT INTO orders (venue_id, user_id, customer_name, phone, arrival_minutes, arrive_at, note, total, submit_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (venue["id"], user["id"], user["name"], user["phone"], arrival_minutes,
                 (now + timedelta(minutes=arrival_minutes)).strftime(UTC_FMT), note, total,
                 submit_key or None)).lastrowid
            conn.executemany(
                "INSERT INTO order_items (order_id, name, variant_label, size_label, option_ids, price, qty) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(order_id, *line) for line in lines_to_save])
    except sqlite3.IntegrityError:  # lost a race on submit_key
        existing = conn.execute("SELECT id FROM orders WHERE submit_key = ?", (submit_key,)).fetchone()
        if existing:
            return existing["id"], None
        raise
    return order_id, None


def check_options(conn, venue_id: int, item_id: int, option_ids: tuple):
    """Are these add-on ids a valid choice for this item? Returns ([names], extra grosze) or None.
    The rules come from the database: options must belong to groups attached to the item, a required
    group needs a choice, and a single-choice group allows only one."""
    groups = conn.execute(
        "SELECT g.* FROM option_groups g JOIN item_option_groups ig ON ig.group_id = g.id "
        "WHERE ig.item_id = ? AND g.venue_id = ? ORDER BY g.id", (item_id, venue_id)).fetchall()
    chosen = []
    if option_ids:
        chosen = conn.execute(
            f"SELECT id, name, price, group_id FROM options WHERE id IN ({','.join('?' * len(option_ids))}) ORDER BY group_id, id",
            option_ids).fetchall()
        if len(chosen) != len(option_ids):
            return None
    allowed = {g["id"] for g in groups}
    if any(o["group_id"] not in allowed for o in chosen):
        return None
    for g in groups:
        n = sum(1 for o in chosen if o["group_id"] == g["id"])
        if (g["required"] and n == 0) or (not g["multi"] and n > 1):
            return None
    return [o["name"] for o in chosen], sum(o["price"] for o in chosen)


def reorder_cart(conn, venue_id: int, user_id: int, order_id: int) -> list[dict]:
    """Match the lines of an old order to the venue's current menu: [{"v": variant id, "o": [add-on ids], "q": qty}].
    Items or add-ons that are gone are skipped."""
    old = conn.execute("SELECT oi.name, oi.size_label, oi.option_ids, oi.qty FROM order_items oi JOIN orders o ON o.id = oi.order_id "
                       "WHERE o.id = ? AND o.user_id = ? AND o.venue_id = ?", (order_id, user_id, venue_id)).fetchall()
    cart = []
    for it in old:
        row = conn.execute(
            "SELECT v.id, i.id AS item_id FROM variants v JOIN items i ON i.id = v.item_id "
            "WHERE i.venue_id = ? AND i.is_available = 1 AND i.name = ? AND v.label = ?",
            (venue_id, it["name"], it["size_label"])).fetchone()
        if not row:
            continue
        oids = tuple(int(x) for x in it["option_ids"].split(",") if x.isdigit())
        if check_options(conn, venue_id, row["item_id"], oids) is None:
            continue
        cart.append({"v": row["id"], "o": list(oids), "q": it["qty"]})
    return cart


# ---------------------------------------------------------------- changing status

def transition(conn, venue_id: int, order_id: int, new_status: str, reason: str = ""):
    """The venue (website or Telegram) moves an order on. Returns (order_row, None) or (None, message).
    'No-show' is possible only NO_SHOW_GRACE_MIN after the promised arrival time, and counts against the
    customer at this venue only; after MAX_NO_SHOWS warnings they are blocked there."""
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND venue_id = ?", (order_id, venue_id)).fetchone()
    if not order:
        return None, "Order not found."
    if new_status not in ALLOWED.get(order["status"], ()):
        return None, "This order was already updated."
    if new_status == "no_show":
        ready_at = _parse_utc(order["arrive_at"]) + timedelta(minutes=cfg.NO_SHOW_GRACE_MIN)
        if ready_at > utcnow():
            return None, (f"Too early for a no-show: the customer may still arrive. "
                          f"You can mark it from {hhmm(ready_at)}.")
    reason = (reason.strip()[:200] or "Declined by the venue") if new_status == "declined" else ""
    with conn:
        cur = conn.execute(
            "UPDATE orders SET status = ?, decline_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = ?",
            (new_status, reason, order_id, order["status"]))
        if cur.rowcount != 1:  # somebody else (web or Telegram) was faster
            return None, "This order was already updated."
        if new_status == "no_show" and order["user_id"]:
            conn.execute(
                """INSERT INTO venue_customer_flags (venue_id, user_id, warnings, blocked) VALUES (?, ?, 1, ?)
                   ON CONFLICT(venue_id, user_id) DO UPDATE SET warnings = warnings + 1,
                   blocked = CASE WHEN warnings + 1 >= ? THEN 1 ELSE blocked END""",
                (venue_id, order["user_id"], int(cfg.MAX_NO_SHOWS <= 1), cfg.MAX_NO_SHOWS))
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone(), None


def cancel_by_customer(conn, user_id: int, order_id: int):
    """A customer may cancel while the venue has not started: status 'new'. No warning is given."""
    with conn:
        cur = conn.execute("UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
                           "WHERE id = ? AND user_id = ? AND status = 'new'", (order_id, user_id))
    return cur.rowcount == 1


def expire_stale(conn) -> list[int]:
    """Close orders nobody handled: EXPIRE_AFTER_MIN after the promised arrival. No warning for anyone."""
    cutoff = (utcnow() - timedelta(minutes=cfg.EXPIRE_AFTER_MIN)).strftime(UTC_FMT)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM orders WHERE status IN {_ACTIVE_SQL} AND arrive_at < ?", (cutoff,))]
    if ids:
        with conn:
            conn.execute(f"UPDATE orders SET status = 'expired', updated_at = CURRENT_TIMESTAMP "
                         f"WHERE id IN ({','.join('?' * len(ids))}) AND status IN {_ACTIVE_SQL}", ids)
    conn.execute("DELETE FROM tg_link_codes WHERE expires_at < datetime('now')")
    conn.commit()
    return ids


# ---------------------------------------------------------------- lists and search

PERIODS = {"today": "Today", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}
STATUS_FILTERS = {"active": "Active", "all": "All", **STATUS_LABELS}
PER_PAGE = 15


def _period_start(period: str) -> str | None:
    now = datetime.now(WARSAW)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period in ("7d", "30d"):
        start = now - timedelta(days=int(period[:-1]))
    else:
        return None
    return start.astimezone(timezone.utc).strftime(UTC_FMT)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_orders(conn, venue_id: int, q: str = "", status: str = "all", period: str = "all", page: int = 1):
    """Filtered, paginated orders of one venue. Returns (orders, total, pages, page)."""
    where, args = ["o.venue_id = ?"], [venue_id]
    if status == "active":
        where.append(f"o.status IN {_ACTIVE_SQL}")
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
    rows = conn.execute(f"SELECT * FROM orders WHERE venue_id = ? AND status IN {_ACTIVE_SQL} ORDER BY arrive_at, id",
                        (venue_id,)).fetchall()
    return hydrate(conn, rows)


def user_orders(conn, user_id: int, limit: int = 50):
    rows = conn.execute(
        "SELECT o.*, v.name AS venue_name, v.slug AS venue_slug FROM orders o "
        "JOIN venues v ON v.id = o.venue_id WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
        (user_id, limit)).fetchall()
    return hydrate(conn, rows)


def active_order_of(conn, user_id: int):
    return conn.execute(
        f"SELECT o.id, o.status, v.name AS venue_name FROM orders o JOIN venues v ON v.id = o.venue_id "
        f"WHERE o.user_id = ? AND o.status IN {_ACTIVE_SQL} ORDER BY o.id DESC LIMIT 1", (user_id,)).fetchone()


def new_link_code() -> str:
    return secrets.token_urlsafe(9)
