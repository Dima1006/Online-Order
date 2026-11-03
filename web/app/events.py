"""One place that tells everybody an order changed: browsers (WebSocket), Telegram and the customer's e-mail."""
import logging

from . import config as cfg
from . import db
from .mail import send_mail_bg
from .realtime import hub
from .tasks import spawn

log = logging.getLogger("coffee")
__all__ = ["announce", "spawn"]


async def announce(order_id: int, *, new: bool = False) -> None:
    conn = db.connect()
    try:
        o = conn.execute(
            "SELECT o.id, o.venue_id, o.user_id, o.status, o.decline_reason, v.name AS venue_name, u.email, u.email_verified "
            "FROM orders o JOIN venues v ON v.id = o.venue_id LEFT JOIN users u ON u.id = o.user_id WHERE o.id = ?",
            (order_id,)).fetchone()
    finally:
        conn.close()
    if not o:
        return
    msg = {"type": "order", "order_id": o["id"], "status": o["status"], "new": new}
    hub.publish(f"venue:{o['venue_id']}", msg)
    if o["user_id"]:
        hub.publish(f"user:{o['user_id']}", msg)
    # The customer may have closed the page: tell them by e-mail when it matters.
    if not new and o["email"] and (o["email_verified"] or cfg.DEMO_MODE) and o["status"] in ("ready", "declined"):
        if o["status"] == "ready":
            send_mail_bg(o["email"], f"Order #{o['id']} is ready",
                         f"Your order #{o['id']} at {o['venue_name']} is ready for pickup.")
        else:
            send_mail_bg(o["email"], f"Order #{o['id']} was declined",
                         f"{o['venue_name']} could not accept your order #{o['id']}.\nReason: {o['decline_reason']}")
    if cfg.TELEGRAM_BOT_TOKEN:
        from . import telegram  # late import: telegram itself calls announce()
        spawn(telegram.sync_order(order_id, new))
