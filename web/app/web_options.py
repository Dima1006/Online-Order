"""Venue panel: add-on groups (milk, syrup...) and photos of the venue and of menu items."""
import re
from decimal import Decimal

from starlette.exceptions import HTTPException

from . import config as cfg
from . import media
from .core import go, read_form, render, require_venue, route
from .orders import utcnow
from .security import limiter
from .utils import clean, normalize
from .web_public import invalidate_index

_LINE = re.compile(r"^(?P<name>.*?)(?:\s*[+|:;]\s*(?P<price>\d{1,3}(?:[.,]\d{1,2})?)\s*(?:zł|zl|pln)?)?$", re.I)
MAX_OPTION_PRICE = 5000  # 50 zł


def parse_options(text: str) -> list[tuple[str, int]]:
    """One option per line: "Oat milk +2.50", "Vanilla syrup + 3", or just "Soy milk" (free)."""
    result = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _LINE.match(raw)
        name = clean(m.group("name") if m else raw, 40)
        if not name:
            raise ValueError(f"“{raw}” has no name.")
        price = int(Decimal(m.group("price").replace(",", ".")) * 100) if m and m.group("price") else 0
        if price > MAX_OPTION_PRICE:
            raise ValueError(f"“{name}”: the extra price is too high (at most {MAX_OPTION_PRICE // 100} zł).")
        result.append((name, price))
    if not result:
        raise ValueError("Add at least one option, one per line.")
    if len(result) > cfg.MAX_OPTIONS_PER_GROUP:
        raise ValueError(f"At most {cfg.MAX_OPTIONS_PER_GROUP} options in a group.")
    names = [normalize(n) for n, _ in result]
    if len(set(names)) != len(names):
        raise ValueError("Two options have the same name.")
    return result


def load_groups(conn, venue_id: int) -> list[dict]:
    groups = [dict(g, options=[], item_ids=[]) for g in conn.execute(
        "SELECT * FROM option_groups WHERE venue_id = ? ORDER BY id", (venue_id,))]
    by_id = {g["id"]: g for g in groups}
    for o in conn.execute("SELECT o.* FROM options o JOIN option_groups g ON g.id = o.group_id "
                          "WHERE g.venue_id = ? ORDER BY o.id", (venue_id,)):
        by_id[o["group_id"]]["options"].append(o)
    for link in conn.execute("SELECT ig.* FROM item_option_groups ig JOIN option_groups g ON g.id = ig.group_id "
                             "WHERE g.venue_id = ?", (venue_id,)):
        by_id[link["group_id"]]["item_ids"].append(link["item_id"])
    return groups


def save_item_groups(conn, venue_id: int, item_id: int, form) -> None:
    """The item editor's checkboxes: which add-on groups this item offers (only the venue's own groups count)."""
    wanted = set()
    for x in form.getlist("group_id"):
        if str(x).isdigit():
            wanted.add(int(x))
    own = {r["id"] for r in conn.execute("SELECT id FROM option_groups WHERE venue_id = ?", (venue_id,))}
    conn.execute("DELETE FROM item_option_groups WHERE item_id = ?", (item_id,))
    conn.executemany("INSERT INTO item_option_groups (item_id, group_id) VALUES (?, ?)",
                     [(item_id, g) for g in sorted(wanted & own)])


def _group_form(form):
    name = clean(str(form.get("name", "")), 40)
    if not name:
        raise ValueError("Enter a name for the group, for example “Milk”.")
    return name, 1 if form.get("required") else 0, 1 if form.get("multi") else 0, parse_options(str(form.get("options", "")))


@route
async def panel_options(request, conn):
    venue = require_venue(request)
    return render(request, "panel_options.html", venue=venue, groups=load_groups(conn, venue["id"]),
                  max_groups=cfg.MAX_OPTION_GROUPS)


def _write_options(conn, group_id: int, options):
    conn.execute("DELETE FROM options WHERE group_id = ?", (group_id,))
    conn.executemany("INSERT INTO options (group_id, name, price) VALUES (?, ?, ?)",
                     [(group_id, n, p) for n, p in options])


@route
async def add_group(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    try:
        name, required, multi, options = _group_form(form)
    except ValueError as exc:
        return go(request, "/panel/options", error=str(exc))
    if conn.execute("SELECT COUNT(*) FROM option_groups WHERE venue_id = ?", (venue["id"],)).fetchone()[0] >= cfg.MAX_OPTION_GROUPS:
        return go(request, "/panel/options", error=f"At most {cfg.MAX_OPTION_GROUPS} groups.")
    if any(normalize(r["name"]) == normalize(name) for r in conn.execute("SELECT name FROM option_groups WHERE venue_id = ?", (venue["id"],))):
        return go(request, "/panel/options", error=f"You already have a group called “{name}”.")
    with conn:
        gid = conn.execute("INSERT INTO option_groups (venue_id, name, required, multi) VALUES (?, ?, ?, ?)",
                           (venue["id"], name, required, multi)).lastrowid
        _write_options(conn, gid, options)
    return go(request, "/panel/options", msg="Group added. Now attach it to items in the Menu.")


@route
async def update_group(request, conn):
    form = await read_form(request)
    venue = require_venue(request)
    gid = request.path_params["group_id"]
    if not conn.execute("SELECT 1 FROM option_groups WHERE id = ? AND venue_id = ?", (gid, venue["id"])).fetchone():
        raise HTTPException(404, "Group not found")
    try:
        name, required, multi, options = _group_form(form)
    except ValueError as exc:
        return go(request, "/panel/options", error=str(exc))
    with conn:
        conn.execute("UPDATE option_groups SET name = ?, required = ?, multi = ? WHERE id = ?", (name, required, multi, gid))
        _write_options(conn, gid, options)
    return go(request, "/panel/options", msg="Saved.")


@route
async def delete_group(request, conn):
    await read_form(request)
    venue = require_venue(request)
    with conn:
        conn.execute("DELETE FROM option_groups WHERE id = ? AND venue_id = ?", (request.path_params["group_id"], venue["id"]))
    return go(request, "/panel/options", msg="Group deleted.")


# ---------------------------------------------------------------- photos

async def _read_upload(request, venue):
    """Returns (form, bytes) after the CSRF check; refuses uploads above the hourly limit."""
    form = await request.form(max_fields=20, max_files=1, max_part_size=cfg.MAX_UPLOAD_BYTES + 10_000)
    from .security import csrf_valid
    if not csrf_valid(request, form):
        raise HTTPException(403, "Your session expired or the form was invalid. Go back, reload the page and try again.")
    if not limiter.allow(f"upload:{venue['id']}", cfg.UPLOADS_PER_HOUR, 3600):
        raise HTTPException(429, "Too many uploads. Please try again later.")
    file = form.get("photo")
    data = b""
    if file is not None and hasattr(file, "read"):
        data = await file.read(cfg.MAX_UPLOAD_BYTES + 1)
    return form, data


@route
async def item_photo(request, conn):
    venue = require_venue(request)
    item_id = request.path_params["item_id"]
    form, data = await _read_upload(request, venue)
    item = conn.execute("SELECT category_id, image FROM items WHERE id = ? AND venue_id = ?", (item_id, venue["id"])).fetchone()
    if not item:
        raise HTTPException(404, "Item not found")
    anchor = f"/panel/menu#cat-{item['category_id']}"
    if form.get("remove"):
        media.delete_image(item["image"])
        with conn:
            conn.execute("UPDATE items SET image = '' WHERE id = ?", (item_id,))
        invalidate_index()
        return go(request, anchor, msg="Photo removed.")
    try:
        name = await media.save_image(data, "item")
    except media.ImageError as exc:
        return go(request, anchor, error=str(exc))
    with conn:
        conn.execute("UPDATE items SET image = ? WHERE id = ?", (name, item_id))
    media.delete_image(item["image"])
    invalidate_index()
    return go(request, anchor, msg="Photo saved.")


@route
async def venue_photo(request, conn):
    venue = require_venue(request)
    form, data = await _read_upload(request, venue)
    if form.get("remove"):
        media.delete_image(venue["image"])
        with conn:
            conn.execute("UPDATE venues SET image = '' WHERE id = ?", (venue["id"],))
        invalidate_index()
        return go(request, "/panel/settings", msg="Photo removed.")
    try:
        name = await media.save_image(data, "venue")
    except media.ImageError as exc:
        return go(request, "/panel/settings", error=str(exc))
    with conn:
        conn.execute("UPDATE venues SET image = ? WHERE id = ?", (name, venue["id"]))
    media.delete_image(venue["image"])
    invalidate_index()
    return go(request, "/panel/settings", msg="Photo saved.")
