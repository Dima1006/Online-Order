"""Pure helpers with no web-framework imports (easy to test)."""
import hashlib
import hmac
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_SCRYPT = dict(n=2**14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(calc.hex(), digest)


def slugify(text: str) -> str:
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "venue"


def format_zl(grosze: int) -> str:
    return f"{grosze // 100},{grosze % 100:02d} zł"


# "Small 6.50", "Latte 0.5l 12,00 zł", or just "5.00"
_PRICE_LINE = re.compile(r"^(.*?)[\s:=]*(\d+(?:[.,]\d{1,2})?)\s*(?:zł|zl|pln)?$", re.I)


def parse_prices(text: str) -> list[tuple[str, int]]:
    """Parse one 'label price' pair per line into [(label, grosze)]."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Add at least one price.")
    if len(lines) > 10:
        raise ValueError("At most 10 sizes per item.")
    result = []
    for line in lines:
        match = _PRICE_LINE.match(line)
        if not match:
            raise ValueError(f"Can't read the price in: “{line}”. Use e.g. “Small 6.50”.")
        label = match.group(1).strip()[:40]
        try:
            grosze = int(Decimal(match.group(2).replace(",", ".")) * 100)
        except InvalidOperation:
            raise ValueError(f"Invalid price in: “{line}”.")
        if not 0 < grosze <= 100_000:
            raise ValueError(f"Price out of range in: “{line}”.")
        result.append((label, grosze))
    if len(result) > 1 and any(not label for label, _ in result):
        raise ValueError("When there are several prices, give each one a name (e.g. “Small 6.50”).")
    return result


def prices_to_text(variants) -> str:
    return "\n".join(
        f"{v['label']} {v['price'] // 100}.{v['price'] % 100:02d}".strip() for v in variants
    )


def normalize(text: str) -> str:
    """Lowercase and strip diacritics, so “Łódź”, “lodz” and “LODZ” all match."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


_PRICE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:zł|zl|pln)?", re.I)


def parse_variants(labels, prices) -> list[tuple[str, int]]:
    """Pair size names with prices from the editor's rows -> [(label, grosze)]."""
    labels, prices = labels or [], prices or []
    rows = []
    for i, raw in enumerate(prices):
        label = (labels[i] if i < len(labels) else "").strip()[:40]
        raw = raw.strip()
        if not label and not raw:
            continue
        if not raw:
            raise ValueError(f"Enter a price for “{label}”.")
        match = _PRICE.fullmatch(raw)
        if not match:
            raise ValueError(f"“{raw}” is not a valid price. Use a number like 6.50.")
        grosze = int(Decimal(match.group(1).replace(",", ".")) * 100)
        if not 0 < grosze <= 100_000:
            raise ValueError(f"Price “{raw}” is out of range.")
        rows.append((label, grosze))
    if not rows:
        raise ValueError("Add at least one price.")
    if len(rows) > 10:
        raise ValueError("At most 10 sizes per item.")
    if len(rows) > 1 and any(not label for label, _ in rows):
        raise ValueError("When there are several sizes, give each one a name (e.g. Small).")
    return rows


_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.][^@\s]{0,253}\.[^@\s.]{2,}$")


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL.match(email))


def password_problem(password: str, email: str = "") -> str | None:
    """Return a message if the password is unacceptable, else None."""
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    if email and password.lower() == email.lower():
        return "Password must differ from your email."
    return None


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths (prevents open redirects after login)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url \
            or any(ord(c) < 32 for c in url):
        return default
    return url


def clean(text: str, limit: int) -> str:
    """Trim, strip control characters, cap length."""
    return "".join(ch for ch in text.strip() if ch == "\n" or ord(ch) >= 32)[:limit]


_dummy = None


def dummy_hash() -> str:
    """A real hash to verify against when the account does not exist, so response time does not reveal it."""
    global _dummy
    if _dummy is None:
        _dummy = hash_password("not-a-real-password")
    return _dummy


def session_fingerprint(password_hash: str | None) -> str:
    """Stored in the session; changes when the password changes, which logs out every other session."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:12]
