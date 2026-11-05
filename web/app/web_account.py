"""Customer accounts: sign up, log in (password or Google), verify e-mail, reset password, My account, My orders."""
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse
from starlette.websockets import WebSocket

from . import config as cfg
from . import google, orders, realtime
from .core import (current_user, go, hash_async, login_session, read_form, render, require_user, route,
                   verify_async, ws_user)
from .events import announce
from .legal import CAPTCHA_ERROR, captcha_ok
from .mail import public_url, send_mail_bg
from .security import client_ip, limiter, ws_origin_ok
from .utils import (clean, dummy_hash, name_problem, normalize_phone, password_problem, safe_next,
                    session_fingerprint, valid_email)

PHONE_HELP = "Enter a valid phone number, for example 600 123 456 or +48 600 123 456."

TOO_MANY = "Too many attempts. Please wait a few minutes and try again."


def _signer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.SECRET_KEY, salt=salt)


def _throttle(key: str, limit: int, window: float) -> None:
    if not limiter.allow(key, limit, window):
        raise HTTPException(429, TOO_MANY)


# ---------------------------------------------------------------- sign up / log in

@route
async def login(request, conn):
    if request.method == "GET":
        return render(request, "login.html", next=safe_next(request.query_params.get("next")), email="")
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    password = str(form.get("password", ""))[:129]
    nxt = safe_next(str(form.get("next", "")))
    ip = client_ip(request)
    # Per IP+account, so a stranger cannot lock somebody out by typing wrong passwords from elsewhere;
    # the looser per-account and per-IP caps still slow down distributed guessing.
    _throttle(f"login-ip:{ip}", 20, 600)
    _throttle(f"login-pair:{ip}:{email}", 8, 600)
    _throttle(f"login-email:{email}", 40, 3600)
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    stored = user["password_hash"] if user and user["password_hash"] else dummy_hash()
    ok = await verify_async(password, stored)
    if not (user and user["password_hash"] and ok):
        return render(request, "login.html", 400, next=nxt, email=email, error="Wrong email or password.")
    login_session(request, user=user)
    return go(request, nxt)


# ---------------------------------------------------------------- sign in with a code sent by e-mail

def _code_hash(email: str, code: str) -> str:
    return hmac.new(cfg.SECRET_KEY.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


@route
async def login_code(request, conn):
    """Step 1: ask for a 6-digit code. The answer is the same whether or not the account exists."""
    nxt = safe_next(request.query_params.get("next") if request.method == "GET" else None)
    if request.method == "GET":
        return render(request, "login_code.html", next=nxt, email="")
    form = await read_form(request)
    nxt = safe_next(str(form.get("next", "")))
    email = clean(str(form.get("email", "")), 254).lower()
    _throttle(f"code-ip:{client_ip(request)}", 10, 3600)
    _throttle(f"code-email:{email}", 3, 600)
    if not cfg.MAIL_ENABLED:
        return render(request, "login_code.html", 400, next=nxt, email=email,
                      error="Sign-in by email is not available on this server yet. Please use your password.")
    if not valid_email(email):
        return render(request, "login_code.html", 400, next=nxt, email=email, error="Enter a valid email address.")
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with conn:
            conn.execute("INSERT OR REPLACE INTO login_codes (email, code_hash, expires_at, tries) "
                         "VALUES (?, ?, datetime('now', ?), 0)", (email, _code_hash(email, code), f"+{cfg.CODE_TTL_MIN} minutes"))
        send_mail_bg(email, f"Your sign-in code: {code}",
                     f"Hi {user['name']},\n\nYour sign-in code is {code}\n\n"
                     f"It works for {cfg.CODE_TTL_MIN} minutes. If you did not ask for it, ignore this message: "
                     "nobody can sign in without it.")
    return go(request, "/login/code/verify?" + urlencode({"email": email, "next": nxt}))


@route
async def login_code_verify(request, conn):
    """Step 2: check the code. Five wrong guesses throw the code away."""
    if request.method == "GET":
        return render(request, "login_code_verify.html", email=clean(request.query_params.get("email", ""), 254).lower(),
                      next=safe_next(request.query_params.get("next")))
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    nxt = safe_next(str(form.get("next", "")))
    code = re.sub(r"\D", "", str(form.get("code", "")))[:6]
    ip = client_ip(request)
    _throttle(f"codev-ip:{ip}", 30, 600)
    _throttle(f"codev-email:{email}", 15, 600)
    row = conn.execute("SELECT * FROM login_codes WHERE email = ? AND expires_at > datetime('now')", (email,)).fetchone()
    good = bool(row and row["tries"] < cfg.CODE_MAX_TRIES and len(code) == 6
                and hmac.compare_digest(row["code_hash"], _code_hash(email, code)))
    if good:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        with conn:
            conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
            if user:   # the code proves control of the mailbox
                conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user["id"],))
        if user:
            login_session(request, user=conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone())
            return go(request, nxt)
    elif row:
        with conn:
            conn.execute("UPDATE login_codes SET tries = tries + 1 WHERE email = ?", (email,))
    return render(request, "login_code_verify.html", 400, email=email, next=nxt,
                  error="That code is wrong or has expired. Check the latest email, or request a new code.")


def _send_verification(request, user) -> None:
    token = _signer("verify-email").dumps({"uid": user["id"], "email": user["email"]})
    send_mail_bg(user["email"], "Confirm your email",
                 f"Hi {user['name']},\n\nConfirm your email address:\n{public_url('/verify/' + token, request)}\n\n"
                 "The link is valid for 3 days. If you did not sign up, ignore this message.")


@route
async def signup(request, conn):
    if request.method == "GET":
        return render(request, "signup.html", form={}, next=safe_next(request.query_params.get("next")))
    form = await read_form(request)
    _throttle(f"signup-ip:{client_ip(request)}", 10, 3600)
    if form.get("website"):  # honeypot: invisible to people, bots fill it in
        raise HTTPException(400, "Something went wrong. Please try again.")
    name = clean(str(form.get("name", "")), 60)
    email = clean(str(form.get("email", "")), 254).lower()
    raw_phone = clean(str(form.get("phone", "")), 40)
    phone = normalize_phone(raw_phone)
    password = str(form.get("password", ""))
    nxt = safe_next(str(form.get("next", "")))
    error = None
    if (problem := name_problem(name)):
        error = problem
    elif phone is None:
        error = PHONE_HELP
    elif not valid_email(email):
        error = "Enter a valid email address."
    elif not form.get("agree"):
        error = "Please accept the Terms and the Privacy Policy."
    elif not await captcha_ok(request, form):
        error = CAPTCHA_ERROR
    elif (problem := password_problem(password, email)):
        error = problem
    elif conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        error = "An account with this email already exists. Log in, or use “Forgot password”."
    if error:
        return render(request, "signup.html", 400, error=error, next=nxt,
                      form={"name": name, "email": email, "phone": raw_phone})
    hashed = await hash_async(password)
    with conn:
        uid = conn.execute("INSERT INTO users (email, password_hash, name, phone) VALUES (?, ?, ?, ?)",
                           (email, hashed, name, phone)).lastrowid
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    _send_verification(request, user)
    login_session(request, user=user)
    msg = ("Account created. Check your inbox and confirm your email to place orders."
           if cfg.VERIFY_REQUIRED else "Account created.")
    return go(request, nxt, msg=msg)


@route
async def logout(request, conn):
    await read_form(request)
    request.session.clear()
    return go(request, "/")


@route
async def verify_email(request, conn):
    try:
        data = _signer("verify-email").loads(request.path_params["token"], max_age=3 * 86400)
    except (BadSignature, SignatureExpired):
        return render(request, "message.html", 400, title="Link expired",
                      text="This confirmation link is invalid or has expired. Log in and request a new one in My account.")
    with conn:
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ? AND email = ?", (data["uid"], data["email"]))
    return go(request, "/account" if current_user(request) else "/login", msg="Email confirmed. Thank you!")


@route
async def resend_verification(request, conn):
    await read_form(request)
    user = require_user(request)
    _throttle(f"resend:{user['id']}", 3, 3600)
    if not user["email_verified"]:
        _send_verification(request, user)
    return go(request, "/account", msg="If your address is not confirmed yet, a new link is on its way.")


# ---------------------------------------------------------------- password reset

def _reset_fingerprint(user) -> str:
    return hashlib.sha256((user["password_hash"] or "").encode()).hexdigest()[:16]


@route
async def forgot(request, conn):
    if request.method == "GET":
        return render(request, "forgot.html")
    form = await read_form(request)
    email = clean(str(form.get("email", "")), 254).lower()
    _throttle(f"forgot-ip:{client_ip(request)}", 5, 3600)
    _throttle(f"forgot-email:{email}", 3, 3600)
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        token = _signer("reset-password").dumps({"uid": user["id"], "fp": _reset_fingerprint(user)})
        send_mail_bg(email, "Reset your password",
                     f"Hi {user['name']},\n\nChoose a new password:\n{public_url('/reset/' + token, request)}\n\n"
                     "The link works once and is valid for 1 hour. If you did not ask for it, ignore this message.")
    # Same answer whether or not the account exists, so this form cannot be used to find out who has one.
    return render(request, "message.html", title="Check your inbox",
                  text="If an account with this email exists, we sent a link to reset the password.")


def _user_from_reset_token(conn, token: str):
    try:
        data = _signer("reset-password").loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        return None
    user = conn.execute("SELECT * FROM users WHERE id = ?", (data.get("uid"),)).fetchone()
    if user and hmac.compare_digest(_reset_fingerprint(user), str(data.get("fp"))):  # single use
        return user
    return None


@route
async def reset_password(request, conn):
    token = request.path_params["token"]
    user = _user_from_reset_token(conn, token)
    if not user:
        return render(request, "message.html", 400, title="Link expired",
                      text="This reset link is invalid, already used or expired. Request a new one.")
    if request.method == "GET":
        return render(request, "reset.html", token=token)
    form = await read_form(request)
    _throttle(f"reset-ip:{client_ip(request)}", 10, 3600)
    password = str(form.get("password", ""))
    if (problem := password_problem(password, user["email"])):
        return render(request, "reset.html", 400, token=token, error=problem)
    hashed = await hash_async(password)
    with conn:  # proving control of the mailbox also confirms the address
        conn.execute("UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?", (hashed, user["id"]))
    return go(request, "/login", msg="Password changed. Log in with the new password.")


# ---------------------------------------------------------------- Google

@route
async def google_start(request, conn):
    if not google.enabled():
        raise HTTPException(404, "Google sign-in is not configured.")
    _throttle(f"google-ip:{client_ip(request)}", 30, 600)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session.update(g_state=state, g_nonce=nonce,
                           g_next=safe_next(request.query_params.get("next")))
    return RedirectResponse(google.auth_url(public_url("/auth/google/callback", request), state, nonce), status_code=303)


@route
async def google_callback(request, conn):
    if not google.enabled():
        raise HTTPException(404, "Google sign-in is not configured.")
    state, nonce = request.session.pop("g_state", None), request.session.pop("g_nonce", None)
    nxt = safe_next(request.session.pop("g_next", "/"))
    sent = request.query_params.get("state", "")
    code = request.query_params.get("code")
    if not state or not code or not hmac.compare_digest(sent.encode(), state.encode()):
        return go(request, "/login", error="Google sign-in failed. Please try again.")
    try:
        tokens = await google.exchange_code(code, public_url("/auth/google/callback", request))
        claims = google.validate_claims(google.claims_from_id_token(tokens["id_token"]), nonce)
    except Exception:
        return go(request, "/login", error="Google sign-in failed. Please try again.")
    email, sub = claims["email"].lower(), claims["sub"]
    name = clean(claims.get("name") or email.split("@")[0], 60)
    user = conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
    with conn:
        if not user:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user and not user["email_verified"]:
                # Somebody registered this address with a password but never proved they own it.
                # Drop that password so it cannot be used to enter the real owner's account.
                conn.execute("UPDATE users SET password_hash = NULL, google_sub = ?, email_verified = 1 WHERE id = ?",
                             (sub, user["id"]))
            elif user:
                conn.execute("UPDATE users SET google_sub = ?, email_verified = 1 WHERE id = ?", (sub, user["id"]))
            else:
                conn.execute("INSERT INTO users (email, google_sub, name, email_verified) VALUES (?, ?, ?, 1)",
                             (email, sub, name))
    user = conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
    login_session(request, user=user)
    return go(request, nxt)


# ---------------------------------------------------------------- My account

@route
async def account(request, conn):
    user = require_user(request)
    flags = conn.execute(
        "SELECT f.warnings, f.blocked, v.name AS venue_name FROM venue_customer_flags f "
        "JOIN venues v ON v.id = f.venue_id WHERE f.user_id = ? AND f.warnings > 0 ORDER BY v.name", (user["id"],)).fetchall()
    return render(request, "account.html", user=user, flags=flags, max_no_shows=cfg.MAX_NO_SHOWS)


@route
async def account_update(request, conn):
    form = await read_form(request)
    user = require_user(request)
    name, phone = clean(str(form.get("name", "")), 60), normalize_phone(clean(str(form.get("phone", "")), 40))
    if (problem := name_problem(name)):
        return go(request, "/account", error=problem)
    if phone is None:
        return go(request, "/account", error=PHONE_HELP)
    with conn:
        conn.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone, user["id"]))
    return go(request, "/account", msg="Saved. New orders will use these details.")


@route
async def account_password(request, conn):
    form = await read_form(request)
    user = require_user(request)
    _throttle(f"pw-change:{user['id']}", 5, 3600)
    new = str(form.get("new_password", ""))
    if user["password_hash"]:
        current = str(form.get("current_password", ""))[:129]
        if not await verify_async(current, user["password_hash"]):
            return go(request, "/account", error="Current password is wrong.")
    if (problem := password_problem(new, user["email"])):
        return go(request, "/account", error=problem)
    hashed = await hash_async(new)
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user["id"]))
    request.session["pv"] = session_fingerprint(hashed)  # this device stays logged in, all others are logged out
    return go(request, "/account", msg="Password changed. Other devices were logged out.")


@route
async def account_delete(request, conn):
    form = await read_form(request)
    user = require_user(request)
    _throttle(f"acct-delete:{user['id']}", 5, 3600)
    if orders.active_order_of(conn, user["id"]):
        return go(request, "/account", error="You have an active order. Wait until it is finished, or cancel it, "
                                              "before deleting your account.")
    if user["password_hash"]:
        ok = await verify_async(str(form.get("password", ""))[:129], user["password_hash"])
    else:
        ok = str(form.get("confirm_email", "")).strip().lower() == user["email"]
    if not ok:
        return go(request, "/account", error="Confirmation did not match. Your account was not deleted.")
    with conn:
        # Venues keep their order history for bookkeeping, but without personal data.
        conn.execute("UPDATE orders SET customer_name = 'Deleted customer', phone = '', note = '' WHERE user_id = ?",
                     (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    request.session.clear()
    return go(request, "/", msg="Your account and personal data were deleted.")


# ---------------------------------------------------------------- my orders

@route
async def my_orders(request, conn):
    user = require_user(request)
    return render(request, "my_orders.html", orders=orders.user_orders(conn, user["id"]))


def _own_order(conn, user, order_id: int):
    row = conn.execute(
        "SELECT o.*, v.name AS venue_name, v.address AS venue_address, v.slug AS venue_slug FROM orders o "
        "JOIN venues v ON v.id = o.venue_id WHERE o.id = ? AND o.user_id = ?", (order_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Order not found")  # also hides other people's order numbers
    return orders.hydrate(conn, [row])[0]


@route
async def order_page(request, conn):
    user = require_user(request)
    return render(request, "order.html", order=_own_order(conn, user, request.path_params["order_id"]))


@route
async def order_fragment(request, conn):
    user = require_user(request)
    return render(request, "_order.html", fragment=True,
                  order=_own_order(conn, user, request.path_params["order_id"]))


@route
async def order_cancel(request, conn):
    await read_form(request)
    user = require_user(request)
    order_id = request.path_params["order_id"]
    _own_order(conn, user, order_id)  # 404 for somebody else's order
    if orders.cancel_by_customer(conn, user["id"], order_id):
        await announce(order_id)
        return go(request, f"/order/{order_id}", msg="Order cancelled.")
    return go(request, f"/order/{order_id}", error="The venue already started on this order, so it can no longer be cancelled.")


async def ws_me(ws: WebSocket):
    """Pushes 'your order changed' events to a logged-in customer."""
    user = ws_user(ws) if ws_origin_ok(ws) else None
    if not user:
        return await ws.close(code=1008)
    await realtime.serve(ws, [f"user:{user['id']}"], client_ip(ws))
