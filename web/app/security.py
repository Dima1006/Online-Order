"""CSRF, rate limiting, client IP, security headers. No business logic here."""
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse

from . import config as cfg

# ---------------------------------------------------------------- CSRF

def csrf_token(request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


def same_origin(request) -> bool:
    """If the browser sends an Origin header it must match our host."""
    origin = request.headers.get("origin")
    if origin is None:
        return True  # old clients; the CSRF token is still required
    return urlparse(origin).netloc == request.headers.get("host", "")


def ws_origin_ok(ws) -> bool:
    """Browsers always send Origin on WebSocket handshakes; it must be our own host (blocks cross-site hijacking)."""
    origin = ws.headers.get("origin")
    return origin is not None and urlparse(origin).netloc == ws.headers.get("host", "")


def csrf_valid(request, form) -> bool:
    expected = request.session.get("csrf")
    sent = form.get("csrf")
    if not expected or not isinstance(sent, str):
        return False
    return hmac.compare_digest(sent.encode(), expected.encode()) and same_origin(request)


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Sliding window in memory. One process only (see README: run a single worker)."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 20_000:  # keep memory bounded
                for k in [k for k, q in self.hits.items() if not q or q[-1] < now - 3600]:
                    del self.hits[k]
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self):
        with self.lock:
            self.hits.clear()


limiter = RateLimiter()


def client_ip(request) -> str:
    if cfg.TRUST_PROXY:
        ip = request.headers.get("cf-connecting-ip")
        if not ip:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if ip:
            return ip[:64]
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- headers

_HOST_OK = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


class _BodyTooLarge(Exception):
    pass


class SecurityMiddleware:
    """Per-IP rate limits, a hard cap on request size (counted while reading, so chunked bodies are covered
    too), security headers on every response and long-lived caching for versioned static files."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        path, method = scope["path"], scope["method"]
        is_static = path.startswith("/static/")
        if not is_static and path != "/healthz":
            ip = client_ip(conn)
            limits = [("all", cfg.RATE_ALL)]
            if method == "POST":
                limits.append(("post", cfg.RATE_POST))
            if path == "/" and b"partial" in scope.get("query_string", b""):
                limits.append(("search", cfg.RATE_SEARCH))
            for name, (limit, window) in limits:
                if not limiter.allow(f"{name}:{ip}", limit, window):
                    return await PlainTextResponse("Too many requests. Please slow down.", status_code=429,
                                                   headers={"Retry-After": "30"})(scope, receive, send)
        # Photo uploads are the only requests allowed to be big (they are shrunk and re-encoded on the server).
        body_limit = cfg.MAX_UPLOAD_BYTES + 50_000 if (method == "POST" and path.startswith("/panel/photo/")) else cfg.MAX_BODY_BYTES
        length = conn.headers.get("content-length", "")
        if length.isdigit() and int(length) > body_limit:
            return await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
        host = conn.headers.get("host", "")
        # Safari needs the websocket host spelled out; 'self' alone does not cover ws:// there.
        ws = f" ws://{host} wss://{host}" if _HOST_OK.match(host) else ""
        csp = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
               f"connect-src 'self'{ws}; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")

        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise _BodyTooLarge
            return message

        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                h = MutableHeaders(scope=message)
                h["Content-Security-Policy"] = csp
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "same-origin"
                h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                if cfg.HTTPS:
                    h["Strict-Transport-Security"] = "max-age=31536000"
                if is_static and message.get("status") == 200:
                    h["Cache-Control"] = "public, max-age=31536000, immutable"  # URLs carry ?v=<file time>
                elif h.get("content-type", "").startswith("text/html"):
                    h["Cache-Control"] = "no-store"  # never cache pages that may hold personal data
            await send(message)

        try:
            await self.app(scope, counting_receive, send_wrapper)
        except _BodyTooLarge:
            if not started:
                await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
