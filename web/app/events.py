"""One place that tells everybody an order changed: browsers (WebSocket) and Telegram."""
import asyncio
import logging

from . import config as cfg
from . import db
from .realtime import hub

log = logging.getLogger("coffee")
_tasks: set = set()  # keep references so background tasks are not garbage-collected


def spawn(coro) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe(coro):
    try:
        await coro
    except Exception as exc:
        log.error("background task failed: %s", type(exc).__name__)


async def announce(order_id: int, *, new: bool = False) -> None:
    conn = db.connect()
    try:
        o = conn.execute("SELECT id, venue_id, user_id, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    finally:
        conn.close()
    if not o:
        return
    msg = {"type": "order", "order_id": o["id"], "status": o["status"], "new": new}
    hub.publish(f"venue:{o['venue_id']}", msg)
    if o["user_id"]:
        hub.publish(f"user:{o['user_id']}", msg)
    if cfg.TELEGRAM_BOT_TOKEN:
        from . import telegram  # late import: telegram itself calls announce()
        spawn(_safe(telegram.sync_order(order_id, new)))
