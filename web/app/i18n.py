"""Polish translation of the whole site.

The templates and messages in the code are written in English. When the visitor's language is Polish, the finished
HTML page is translated just before it is sent: every piece of text is looked up in lang_pl.PL (exact text, or a
pattern with {} for the parts that change, such as names and numbers). Text without a translation stays English, and
tests/test_i18n.py lists any such text, so nothing is silently missed. Text typed by users is never translated."""
import functools
import html
import re

from .lang_pl import PL

LANGS = ("en", "pl")
COOKIE = "lang"

_exact: dict[str, object] = {}
_patterns: list[tuple[re.Pattern, object]] = []
for _k, _v in PL.items():
    if "{}" in _k:
        _rx = "(.*?)".join(re.escape(p) for p in _k.split("{}"))
        _patterns.append((re.compile(_rx, re.S), _v))
    else:
        _exact[_k] = _v
_patterns.sort(key=lambda p: -len(p[0].pattern))   # most specific first


def _norm(s: str) -> str:
    return " ".join(s.split())


@functools.lru_cache(maxsize=8192)
def tr(text: str) -> str | None:
    """Polish version of one piece of text, or None when there is none."""
    key = _norm(html.unescape(text))
    if key in _exact:
        out = _exact[key]
        return out if isinstance(out, str) else out()
    if " · " in key and key not in _exact:   # page titles and "x · y" lines: translate each part
        parts = key.split(" · ")
        done = [tr(p) for p in parts]
        if any(d is not None for d in done):
            return " · ".join(d if d is not None else p for d, p in zip(done, parts))
    for rx, out in _patterns:
        m = rx.fullmatch(key)
        if m:
            parts = m.groups()
            if callable(out):
                return out(*parts)
            for p in parts:
                out = out.replace("{}", p, 1)
            return out
    return None


def get_lang(request) -> str:
    c = request.cookies.get(COOKIE, "")
    if c in LANGS:
        return c
    accept = request.headers.get("accept-language", "").lower()
    return "pl" if accept.startswith("pl") else "en"


_TAG = re.compile(r"(<[^>]*>)")
_ATTR = re.compile(r'''\b(placeholder|title|aria-label|alt|data-confirm)="([^"]*)"''')
_SKIP_OPEN = re.compile(r"<(script|style|textarea|pre)\b", re.I)
_SKIP_CLOSE = re.compile(r"</(script|style|textarea|pre)\b", re.I)


def _attr(m):
    out = tr(m.group(2))
    if out is None:
        return m.group(0)
    return f'{m.group(1)}="{html.escape(out, quote=True).replace(chr(10), "&#10;")}"'


def translate_html(page: str) -> str:
    """Translate a finished HTML page into Polish."""
    out, skipping = [], 0
    for part in _TAG.split(page):
        if part.startswith("<") and part.endswith(">"):
            if _SKIP_OPEN.match(part):
                skipping += 1
            elif _SKIP_CLOSE.match(part):
                skipping = max(0, skipping - 1)
            out.append(_ATTR.sub(_attr, part) if " " in part else part)
        elif skipping or not part.strip():
            out.append(part)
        else:
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            new = tr(part)
            out.append(part if new is None else lead + html.escape(new, quote=False) + trail)
    return "".join(out)


def untranslated(page: str) -> list[str]:
    """Texts of a page that have no Polish version (used by the tests)."""
    missing = []
    skipping = 0
    for part in _TAG.split(page):
        if part.startswith("<") and part.endswith(">"):
            if _SKIP_OPEN.match(part):
                skipping += 1
            elif _SKIP_CLOSE.match(part):
                skipping = max(0, skipping - 1)
            for m in _ATTR.finditer(part):
                if re.search(r"[A-Za-z]{3}", m.group(2)) and tr(m.group(2)) is None:
                    missing.append(_norm(html.unescape(m.group(2))))
        elif not skipping and part.strip():
            t = _norm(html.unescape(part))
            if re.search(r"[A-Za-z]{3}", t) and tr(t) is None:
                missing.append(t)
    return missing
