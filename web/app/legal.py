"""Privacy Policy, Terms, personal-data export (GDPR) and the optional CAPTCHA (Cloudflare Turnstile)."""
import asyncio
import json
import urllib.parse
import urllib.request

from starlette.exceptions import HTTPException
from starlette.responses import Response

from . import config as cfg
from . import i18n
from .core import go, render, require_user, route
from .security import client_ip
from .utils import safe_next


async def captcha_ok(request, form) -> bool:
    """True when no CAPTCHA is configured, or the visitor solved it. Fails closed if the check cannot be made."""
    if not cfg.CAPTCHA_ENABLED:
        return True
    token = str(form.get("cf-turnstile-response", ""))[:2048]
    if not token:
        return False
    data = urllib.parse.urlencode({"secret": cfg.TURNSTILE_SECRET, "response": token,
                                   "remoteip": client_ip(request)}).encode()

    def call() -> bool:
        req = urllib.request.Request("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
        with urllib.request.urlopen(req, timeout=8) as r:
            return bool(json.loads(r.read(20_000)).get("success"))
    try:
        return await asyncio.to_thread(call)
    except Exception:
        return False


CAPTCHA_ERROR = "Please confirm that you are not a robot and try again."


@route
async def privacy(request, conn):
    return render(request, "privacy_pl.html" if i18n.get_lang(request) == "pl" else "privacy.html")


@route
async def terms(request, conn):
    return render(request, "terms_pl.html" if i18n.get_lang(request) == "pl" else "terms.html")


@route
async def set_lang(request, conn):
    """Remember the visitor's language (a plain cookie) and go back to where they were."""
    code = request.path_params["code"]
    if code not in i18n.LANGS:
        raise HTTPException(404, "Not found")
    response = go(request, safe_next(request.query_params.get("next")))
    response.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, samesite="lax", secure=request.url.scheme == "https", path="/")
    return response


@route
async def account_export(request, conn):
    """Everything we hold about the signed-in customer, as a JSON download."""
    user = require_user(request)
    profile = {k: user[k] for k in ("id", "email", "name", "phone", "email_verified", "created_at")}
    orders_out = []
    for o in conn.execute("SELECT o.*, v.name AS venue_name FROM orders o JOIN venues v ON v.id = o.venue_id "
                          "WHERE o.user_id = ? ORDER BY o.id", (user["id"],)):
        row = dict(o)
        row["items"] = [dict(i) for i in conn.execute("SELECT * FROM order_items WHERE order_id = ?", (o["id"],))]
        orders_out.append(row)
    body = json.dumps({"account": profile, "orders": orders_out}, indent=2, ensure_ascii=False, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="my-data.json"'})
