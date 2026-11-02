"""Google sign-in (OpenID Connect authorization-code flow) using only the standard library."""
import asyncio
import base64
import json
import time
import urllib.parse
import urllib.request

from . import config as cfg

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def enabled() -> bool:
    return bool(cfg.GOOGLE_CLIENT_ID and cfg.GOOGLE_CLIENT_SECRET)


def auth_url(redirect_uri: str, state: str, nonce: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
    })


def _post_sync(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


async def exchange_code(code: str, redirect_uri: str) -> dict:
    return await asyncio.to_thread(_post_sync, TOKEN_URL, {
        "code": code, "client_id": cfg.GOOGLE_CLIENT_ID, "client_secret": cfg.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})


def claims_from_id_token(id_token: str) -> dict:
    """The token arrives straight from Google's token endpoint over TLS, so (per the OpenID Connect
    spec, section 3.1.3.7) its signature need not be re-verified; the claims are still checked."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception as exc:
        raise ValueError("Unreadable id_token") from exc


def validate_claims(claims: dict, nonce: str, now: float | None = None) -> dict:
    now = now or time.time()
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise ValueError("Wrong issuer")
    if claims.get("aud") != cfg.GOOGLE_CLIENT_ID:
        raise ValueError("Wrong audience")
    if float(claims.get("exp", 0)) < now:
        raise ValueError("Token expired")
    if claims.get("nonce") != nonce:
        raise ValueError("Wrong nonce")
    if claims.get("email_verified") not in (True, "true"):
        raise ValueError("Google account e-mail is not verified")
    if not claims.get("sub") or not claims.get("email"):
        raise ValueError("Missing sub/email")
    return claims
