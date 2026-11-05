import asyncio
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from . import config as cfg
from . import db, events, media, orders, telegram, web_account, web_admin, web_contact, web_options, web_public, web_venue
from .core import BASE_DIR, Redirect, http_error, redirect_handler, render, route, server_error
from .security import SecurityMiddleware
from .seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("coffee")


def _expire_once() -> list[int]:
    conn = db.connect()
    try:
        return orders.expire_stale(conn)
    finally:
        conn.close()


async def expire_loop() -> None:
    """Every minute: close orders nobody handled (and tell whoever is watching them)."""
    while True:
        await asyncio.sleep(60)
        try:
            for order_id in await asyncio.to_thread(_expire_once):
                await events.announce(order_id)
        except Exception as exc:
            log.error("expire loop failed: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    if cfg.env_bool("DEMO_SEED"):
        seed_demo()
    if not cfg.ADMIN_PASSWORD:
        log.info("ADMIN_PASSWORD is not set: the admin area (/admin) is disabled, new venues cannot be approved.")
    background = [asyncio.create_task(expire_loop())]
    if cfg.TELEGRAM_BOT_TOKEN:
        background.append(asyncio.create_task(telegram.run()))
    try:
        yield
    finally:
        for task in background:
            task.cancel()


@route
async def for_venues(request, conn):
    return render(request, "for_venues.html")


async def healthz(request):
    return PlainTextResponse("ok")


G, P, GP = ["GET"], ["POST"], ["GET", "POST"]
routes = [
    Route("/", web_public.home),
    Route("/healthz", healthz),
    Route("/for-venues", for_venues),
    Route("/v/{slug}", web_public.venue_page),
    Route("/v/{slug}/order", web_public.place_order, methods=P),
    # customers
    Route("/login", web_account.login, methods=GP),
    Route("/signup", web_account.signup, methods=GP),
    Route("/logout", web_account.logout, methods=P),
    Route("/verify/{token}", web_account.verify_email),
    Route("/resend-verification", web_account.resend_verification, methods=P),
    Route("/forgot", web_account.forgot, methods=GP),
    Route("/login/code", web_account.login_code, methods=GP),
    Route("/login/code/verify", web_account.login_code_verify, methods=GP),
    Route("/contact", web_contact.contact, methods=GP),
    Route("/reset/{token}", web_account.reset_password, methods=GP),
    Route("/auth/google", web_account.google_start),
    Route("/auth/google/callback", web_account.google_callback),
    Route("/account", web_account.account),
    Route("/account/update", web_account.account_update, methods=P),
    Route("/account/password", web_account.account_password, methods=P),
    Route("/account/delete", web_account.account_delete, methods=P),
    Route("/my-orders", web_account.my_orders),
    Route("/order/{order_id:int}", web_account.order_page),
    Route("/order/{order_id:int}/fragment", web_account.order_fragment),
    Route("/order/{order_id:int}/cancel", web_account.order_cancel, methods=P),
    WebSocketRoute("/ws/me", web_account.ws_me),
    # venues
    Route("/venue/register", web_venue.venue_register, methods=GP),
    Route("/venue/login", web_venue.venue_login, methods=GP),
    Route("/venue/forgot", web_venue.venue_forgot, methods=GP),
    Route("/venue/reset/{token}", web_venue.venue_reset, methods=GP),
    Route("/panel", web_venue.panel_live),
    Route("/panel/live/fragment", web_venue.panel_live_fragment),
    Route("/panel/pause", web_venue.panel_pause, methods=P),
    Route("/panel/orders", web_venue.panel_orders),
    Route("/panel/orders/{order_id:int}/status", web_venue.order_status, methods=P),
    WebSocketRoute("/ws/panel", web_venue.ws_panel),
    Route("/panel/menu", web_venue.panel_menu),
    Route("/panel/menu/starter", web_venue.starter_categories, methods=P),
    Route("/panel/menu/category", web_venue.add_category, methods=P),
    Route("/panel/menu/category/{category_id:int}/delete", web_venue.delete_category, methods=P),
    Route("/panel/menu/item", web_venue.add_item, methods=P),
    Route("/panel/menu/item/{item_id:int}", web_venue.update_item, methods=P),
    Route("/panel/menu/item/{item_id:int}/toggle", web_venue.toggle_item, methods=P),
    Route("/panel/menu/item/{item_id:int}/delete", web_venue.delete_item, methods=P),
    Route("/panel/options", web_options.panel_options),
    Route("/panel/options/group", web_options.add_group, methods=P),
    Route("/panel/options/group/{group_id:int}", web_options.update_group, methods=P),
    Route("/panel/options/group/{group_id:int}/delete", web_options.delete_group, methods=P),
    Route("/panel/photo/item/{item_id:int}", web_options.item_photo, methods=P),
    Route("/panel/photo/venue", web_options.venue_photo, methods=P),
    Route("/media/{name}", media.media),
    Route("/panel/settings", web_venue.panel_settings),
    Route("/panel/settings/save", web_venue.save_settings, methods=P),
    Route("/panel/password", web_venue.panel_password, methods=P),
    Route("/panel/customers/{user_id:int}/reset", web_venue.reset_customer, methods=P),
    Route("/panel/telegram/link", web_venue.telegram_link, methods=P),
    Route("/panel/telegram/{chat_pk:int}/disconnect", web_venue.telegram_disconnect, methods=P),
    # admin
    Route("/admin/login", web_admin.admin_login, methods=GP),
    Route("/admin", web_admin.admin_home),
    Route("/admin/test-mail", web_admin.admin_test_mail, methods=P),
    Route("/admin/mail-check", web_admin.admin_mail_check, methods=P),
    Route("/admin/venues/{venue_id:int}/{action}", web_admin.admin_venue_action, methods=P),
    Route("/admin/flags/{venue_id:int}/{user_id:int}/unblock", web_admin.admin_unblock_user, methods=P),
    Mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static"),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[
        Middleware(SecurityMiddleware),
        Middleware(SessionMiddleware, secret_key=cfg.SECRET_KEY, session_cookie="ahead_session",
                   same_site="lax", https_only=cfg.HTTPS, max_age=14 * 24 * 3600),
    ],
    exception_handlers={HTTPException: http_error, Redirect: redirect_handler, Exception: server_error},
)
