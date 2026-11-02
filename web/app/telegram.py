"""Telegram bot for venue staff: new-order notifications with buttons, decline with a reason.
Uses long polling inside the web process, so website and bot share one database and one set of rules."""
import asyncio
import html
import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import config as cfg
from . import db, events, orders
from .security import limiter
from .utils import clean, format_zl

log = logging.getLogger("coffee")
API = "https://api.telegram.org/bot{token}/{method}"
PENDING_DECLINE: dict[int, tuple[int, float]] = {}   # chat_id -> (order_id, deadline)
CALLBACK = re.compile(r"^o:(\d{1,9}):(preparing|ready|completed|no_show|decline)$")


class TelegramError(Exception):
    pass


def _call_sync(method: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        API.format(token=cfg.TELEGRAM_BOT_TOKEN, method=method),
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read())
        except Exception:
            raise TelegramError(f"HTTP {exc.code}") from None
    except Exception as exc:  # do not echo the exception: its text could contain the bot token URL
        raise TelegramError(type(exc).__name__) from None
    if not data.get("ok"):
        raise TelegramError(data.get("description", "unknown error"))
    return data["result"]


async def call(method: str, _timeout: float = 15, **payload):
    return await asyncio.to_thread(_call_sync, method, payload, _timeout)


# ---------------------------------------------------------------- message building

def order_text(o: dict, venue_name: str) -> str:
    e = html.escape
    lines = [f"<b>Order #{o['id']}</b> · {e(venue_name)}",
             f"👤 {e(o['customer_name'])}" + (f" · {e(o['phone'])}" if o["phone"] else ""),
             f"⏱ Arrives at <b>{o['arrival_local']}</b> (in {o['arrival_minutes']} min from {o['created_local']})", ""]
    for it in o["items"]:
        label = f" ({e(it['variant_label'])})" if it["variant_label"] else ""
        lines.append(f"• {it['qty']} × {e(it['name'])}{label}, {format_zl(it['price'] * it['qty'])}")
    lines.append(f"\n<b>Total {format_zl(o['total'])}</b>, pay at the venue")
    if o["note"]:
        lines.append(f"📝 {e(o['note'])}")
    status = f"\nStatus: <b>{e(o['status_label'])}</b>"
    if o["status"] == "declined":
        status += f" ({e(o['decline_reason'])})"
    return "\n".join(lines) + status


def order_keyboard(o: dict) -> dict:
    nexts = orders.ALLOWED.get(o["status"], ())
    rows = []
    main = [n for n in nexts if n not in ("no_show", "declined")]
    if main:
        rows.append([{"text": "✅ " + orders.ACTION_LABELS[n], "callback_data": f"o:{o['id']}:{n}"} for n in main])
    rest = []
    if "no_show" in nexts:
        rest.append({"text": "🟨 No-show", "callback_data": f"o:{o['id']}:no_show"})
    if "declined" in nexts:
        rest.append({"text": "❌ Decline", "callback_data": f"o:{o['id']}:decline"})
    if rest:
        rows.append(rest)
    return {"inline_keyboard": rows}


def _load(conn, order_id: int):
    row = conn.execute(
        "SELECT o.*, v.name AS venue_name FROM orders o JOIN venues v ON v.id = o.venue_id WHERE o.id = ?",
        (order_id,)).fetchone()
    if not row:
        return None, ""
    return orders.hydrate(conn, [row])[0], row["venue_name"]


async def sync_order(order_id: int, new: bool) -> None:
    """Send the order to the venue's chats (new) or refresh the messages we already sent."""
    conn = db.connect()
    try:
        o, venue_name = _load(conn, order_id)
        if not o:
            return
        chats = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM tg_chats WHERE venue_id = ?", (o["venue_id"],))]
        sent = {r["chat_id"]: r["message_id"] for r in conn.execute(
            "SELECT chat_id, message_id FROM tg_messages WHERE order_id = ?", (order_id,))}
    finally:
        conn.close()
    text, kb = order_text(o, venue_name), order_keyboard(o)
    for chat_id in chats:
        try:
            if chat_id in sent:
                await call("editMessageText", chat_id=chat_id, message_id=sent[chat_id], text=text,
                           parse_mode="HTML", reply_markup=kb)
            elif new:
                res = await call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
                c = db.connect()
                try:
                    with c:
                        c.execute("INSERT OR REPLACE INTO tg_messages (order_id, chat_id, message_id) VALUES (?, ?, ?)",
                                  (order_id, chat_id, res["message_id"]))
                finally:
                    c.close()
        except TelegramError as exc:
            if "not modified" not in str(exc):
                log.warning("Telegram send/edit failed for chat %s: %s", chat_id, exc)


# ---------------------------------------------------------------- incoming updates

HELP = ("This bot sends new orders to your venue.\n\n"
        "To connect a chat, open the venue panel → Settings → Telegram, press “Connect Telegram” "
        "and follow the link.\n\n/orders: active orders\n/stop: disconnect this chat")


def _linked_venue_ids(conn, chat_id: int) -> list[int]:
    return [r["venue_id"] for r in conn.execute("SELECT venue_id FROM tg_chats WHERE chat_id = ?", (chat_id,))]


async def _reply(chat_id: int, text: str, **extra):
    try:
        await call("sendMessage", chat_id=chat_id, text=text, **extra)
    except TelegramError as exc:
        log.warning("Telegram reply failed: %s", exc)


async def _on_message(msg: dict) -> None:
    chat = msg["chat"]
    chat_id, text = chat["id"], msg["text"].strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    conn = db.connect()
    try:
        if cmd == "/start" and arg.strip():
            if not limiter.allow(f"tg-link:{chat_id}", 10, 60):
                return await _reply(chat_id, "Too many attempts. Wait a minute and try again.")
            row = conn.execute("SELECT c.venue_id, v.name FROM tg_link_codes c JOIN venues v ON v.id = c.venue_id "
                               "WHERE c.code = ? AND c.expires_at > datetime('now') AND v.status = 'approved'",
                               (arg.strip()[:64],)).fetchone()
            if not row:
                return await _reply(chat_id, "This link is invalid or expired. Create a new one in the venue panel.")
            title = clean(chat.get("title") or chat.get("first_name") or "chat", 60)
            with conn:
                conn.execute("INSERT OR IGNORE INTO tg_chats (venue_id, chat_id, title) VALUES (?, ?, ?)",
                             (row["venue_id"], chat_id, title))
                conn.execute("DELETE FROM tg_link_codes WHERE code = ?", (arg.strip()[:64],))
            return await _reply(chat_id, f"✅ Connected to {row['name']}. New orders will appear here with buttons.")
        if cmd == "/stop":
            with conn:
                conn.execute("DELETE FROM tg_chats WHERE chat_id = ?", (chat_id,))
            PENDING_DECLINE.pop(chat_id, None)
            return await _reply(chat_id, "Disconnected. You will no longer receive orders here.")
        if cmd == "/orders":
            venues = _linked_venue_ids(conn, chat_id)
            if not venues:
                return await _reply(chat_id, HELP)
            active = [o for v in venues for o in orders.live_orders(conn, v)]
            if not active:
                return await _reply(chat_id, "No active orders.")
            for o in active[:10]:
                full, name = _load(conn, o["id"])
                await _reply(chat_id, order_text(full, name), parse_mode="HTML", reply_markup=order_keyboard(full))
            return
        pending = PENDING_DECLINE.get(chat_id)
        if pending and not text.startswith("/"):
            order_id, deadline = pending
            PENDING_DECLINE.pop(chat_id, None)
            if time.time() > deadline:
                return await _reply(chat_id, "That took too long. Press Decline again.")
            o = conn.execute("SELECT venue_id FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not o or o["venue_id"] not in _linked_venue_ids(conn, chat_id):
                return
            if orders.transition(conn, o["venue_id"], order_id, "declined", clean(text, 200)):
                await events.announce(order_id)
                return await _reply(chat_id, f"Order #{order_id} declined. The customer can see your reason.")
            return await _reply(chat_id, "This order was already updated.")
        await _reply(chat_id, HELP)
    finally:
        conn.close()


async def _on_callback(cb: dict) -> None:
    cb_id = cb["id"]
    m = CALLBACK.match(cb.get("data", ""))
    message = cb.get("message")
    if not m or not message:
        return await _answer(cb_id, "Unknown action")
    order_id, action, chat_id = int(m.group(1)), m.group(2), message["chat"]["id"]
    conn = db.connect()
    try:
        o = conn.execute("SELECT venue_id FROM orders WHERE id = ?", (order_id,)).fetchone()
        # Authorization: this chat must be connected to the venue that owns the order.
        if not o or o["venue_id"] not in _linked_venue_ids(conn, chat_id):
            return await _answer(cb_id, "Not allowed", alert=True)
        if action == "decline":
            PENDING_DECLINE[chat_id] = (order_id, time.time() + 300)
            await _answer(cb_id, "Send the reason")
            return await _reply(chat_id, f"Why is order #{order_id} declined? Reply with a short reason "
                                         "(the customer will see it).", reply_markup={"force_reply": True})
        if orders.transition(conn, o["venue_id"], order_id, action):
            await _answer(cb_id, "Done")
        else:
            await _answer(cb_id, "Already updated")
    finally:
        conn.close()
    await events.announce(order_id)


async def _answer(cb_id: str, text: str, alert: bool = False):
    try:
        await call("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=alert)
    except TelegramError:
        pass


async def handle_update(upd: dict) -> None:
    if "callback_query" in upd:
        return await _on_callback(upd["callback_query"])
    msg = upd.get("message")
    if msg and isinstance(msg.get("text"), str):
        await _on_message(msg)


async def run() -> None:
    """Long-polling loop. Started from the app lifespan when TELEGRAM_BOT_TOKEN is set."""
    try:
        me = await call("getMe")
        cfg.TELEGRAM_BOT_USERNAME = me.get("username", "")
        log.info("Telegram bot @%s is running", cfg.TELEGRAM_BOT_USERNAME)
    except TelegramError as exc:
        log.error("Telegram disabled: getMe failed (%s). Check TELEGRAM_BOT_TOKEN.", exc)
        return
    offset = None
    while True:
        try:
            updates = await call("getUpdates", _timeout=35, offset=offset, timeout=25,
                                 allowed_updates=["message", "callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("getUpdates failed: %s", exc)
            await asyncio.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                await handle_update(upd)
            except Exception as exc:
                log.error("Telegram update failed: %s", type(exc).__name__)
