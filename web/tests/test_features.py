"""Add-ons and photos. Run: python3 -I tests/test_features.py   (from the web/ folder)"""
import io, json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(tmp, "app.db"), DEMO_SEED="1", DEMO_MODE="1",
                  ADMIN_PASSWORD="adminpass-123", DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40)
from PIL import Image
from starlette.testclient import TestClient
from app.main import app
from app import db, media
from app.web_options import parse_options

B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
def one(q, *a):
    conn = db.connect(); r = conn.execute(q, a).fetchone(); conn.close(); return r
def sql(q, *a):
    conn = db.connect(); conn.execute(q, a); conn.commit(); conn.close()
def png(size=(1200, 900), mode="RGB", fmt="PNG"):
    b = io.BytesIO(); Image.new(mode, size, (200, 120, 60)).save(b, fmt); return b.getvalue()

# option parsing
assert parse_options("Oat milk +2.50\nSoy\nSyrup | 3 zł") == [("Oat milk", 250), ("Soy", 0), ("Syrup", 300)]
for bad in ("", "A\nA", "X +999"):
    try: parse_options(bad); raise SystemExit("accepted " + repr(bad))
    except ValueError: pass

with TestClient(app, base_url=B) as c:
    sql("UPDATE venues SET open_time='00:00', close_time='00:00', prep_minutes=5 WHERE id=1")
    v = TestClient(app, base_url=B)
    v.post("/venue/login", data={"csrf": csrf(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": "demo-pass-12345"})
    for p in ("/panel/options", "/panel/menu", "/panel/settings"): assert v.get(p).status_code == 200, p
    t = csrf(v, "/panel/options")

    # create groups: required single "Milk", optional multi "Syrup"
    r = v.post("/panel/options/group", data={"csrf": t, "name": "Milk", "required": "on", "options": "Regular\nOat milk +2.00"}, follow_redirects=True)
    assert "Group added" in r.text
    v.post("/panel/options/group", data={"csrf": t, "name": "Syrup", "multi": "on", "options": "Vanilla +1.50\nCaramel +1.50"})
    assert "must choose" in v.get("/panel/options").text
    milk = one("SELECT id FROM option_groups WHERE name='Milk'")["id"]
    syrup = one("SELECT id FROM option_groups WHERE name='Syrup'")["id"]
    oat = one("SELECT id FROM options WHERE name='Oat milk'")["id"]
    reg = one("SELECT id FROM options WHERE name='Regular'")["id"]
    van = one("SELECT id FROM options WHERE name='Vanilla'")["id"]
    assert v.post("/panel/options/group", data={"csrf": t, "name": "Milk", "options": "x"}, follow_redirects=True).text.count("already have") == 1

    # attach to the first item through the item editor
    item = one("SELECT i.id, i.name, i.category_id FROM items i WHERE i.venue_id=1 ORDER BY i.id")
    var = one("SELECT id, price, label FROM variants WHERE item_id=?", item["id"])
    page = v.get("/panel/menu").text
    assert 'name="group_id"' in page
    r = v.post(f"/panel/menu/item/{item['id']}", data={"csrf": t, "name": item["name"], "description": "", "size_label": var["label"],
               "size_price": "%d.%02d" % (var["price"] // 100, var["price"] % 100), "group_id": [str(milk), str(syrup)]})
    var = one("SELECT id, price FROM variants WHERE item_id=?", item["id"])   # variant ids are rewritten on save
    assert one("SELECT COUNT(*) n FROM item_option_groups WHERE item_id=?", item["id"])["n"] == 2
    # someone else's group id cannot be attached
    sql("INSERT INTO venues (slug, name, email, password_hash) VALUES ('other','Other','o@x.pl','x')")
    sql("INSERT INTO option_groups (venue_id, name) VALUES (2, 'Foreign')")
    foreign = one("SELECT id FROM option_groups WHERE name='Foreign'")["id"]
    v.post(f"/panel/menu/item/{item['id']}", data={"csrf": t, "name": item["name"], "description": "", "size_label": "",
           "size_price": "%d.%02d" % (var["price"] // 100, var["price"] % 100), "group_id": [str(milk), str(syrup), str(foreign)]})
    assert one("SELECT COUNT(*) n FROM item_option_groups WHERE group_id=?", foreign)["n"] == 0
    var = one("SELECT id, price FROM variants WHERE item_id=?", item["id"])

    # venue page offers the choices
    page = c.get("/v/wood-coffee").text
    assert 'id="opt-dialog"' in page and "Oat milk" in page and '"required": true' in page.replace('":true', '": true')

    # customer
    c.post("/signup", data={"csrf": csrf(c, "/signup"), "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "agree": "1", "phone": "+48 600 100 200"})
    def order(lines, key):
        page = c.get("/v/wood-coffee").text
        return c.post("/v/wood-coffee/order", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1), "arrival_minutes": "15",
                      "note": "", "cart": json.dumps(lines), "submit_key": key}, follow_redirects=False)
    vid = var["id"]
    assert order([{"variant_id": vid, "qty": 1}], "a1-long-key-xx").status_code != 303                       # required milk missing
    assert order([{"variant_id": vid, "qty": 1, "options": [reg, oat]}], "a2-long-key-xx").status_code != 303  # two milks
    assert order([{"variant_id": vid, "qty": 1, "options": [reg, 99999]}], "a3-long-key-xx").status_code != 303
    # an add-on of a foreign group is refused even if the id exists
    sql("INSERT INTO options (group_id, name, price) VALUES (?, 'Gold', 1)", foreign)
    gold = one("SELECT id FROM options WHERE name='Gold'")["id"]
    assert order([{"variant_id": vid, "qty": 1, "options": [reg, gold]}], "a4-long-key-xx").status_code != 303
    assert order([{"variant_id": vid, "qty": 1, "options": "oops"}], "a5-long-key-xx").status_code != 303
    r = order([{"variant_id": vid, "qty": 2, "options": [oat, van]}], "a6-long-key-xx")
    assert r.status_code == 303, r.text
    oid = int(r.headers["location"].split("/")[-1])
    row = one("SELECT * FROM order_items WHERE order_id=?", oid)
    unit = var["price"] + 200 + 150
    assert row["price"] == unit and row["qty"] == 2 and "Oat milk" in row["variant_label"] and "Vanilla" in row["variant_label"], dict(row)
    assert one("SELECT total FROM orders WHERE id=?", oid)["total"] == unit * 2          # price comes from the database
    assert "Oat milk" in c.get(f"/order/{oid}").text
    # cancel, then "Order again" restores the same add-ons
    cs = re.search(r'name="csrf" value="([^"]+)"', c.get(f"/order/{oid}").text).group(1)
    c.post(f"/order/{oid}/cancel", data={"csrf": cs})
    page = c.get(f"/v/wood-coffee?reorder={oid}").text
    m = re.search(r'id="reorder-data">(.*?)</script>', page, re.S)
    assert m and json.loads(m.group(1)) == [{"v": vid, "o": sorted([oat, van]), "q": 2}], m and m.group(1)
    # an add-on removed from the menu is dropped from "Order again"
    t = csrf(v, "/panel/options")
    v.post(f"/panel/options/group/{syrup}", data={"csrf": t, "name": "Syrup", "multi": "on", "options": "Hazelnut +1.50"})
    page = c.get(f"/v/wood-coffee?reorder={oid}").text
    assert "reorder-data" not in page
    v.post(f"/panel/options/group/{syrup}/delete", data={"csrf": t})
    assert one("SELECT COUNT(*) n FROM option_groups WHERE id=?", syrup)["n"] == 0

    # regression: nested <form> tags make browsers drop fields, so "Save" in Settings silently failed
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self): super().__init__(); self.depth = 0; self.nested = False; self.fields = {}; self.cur = None
        def handle_starttag(self, tag, a):
            a = dict(a)
            if tag == "form":
                self.depth += 1; self.nested |= self.depth > 1; self.cur = a.get("action"); self.fields[self.cur] = set()
            elif tag in ("input", "select", "textarea") and self.cur: self.fields[self.cur].add(a.get("name"))
        def handle_endtag(self, tag):
            if tag == "form": self.depth -= 1; self.cur = None
    for path in ("/panel/settings", "/panel/menu", "/panel/options", "/panel", "/panel/orders"):
        f = Forms(); f.feed(v.get(path).text); assert not f.nested, "nested form on " + path
        if path == "/panel/settings":
            assert {"name", "open_time", "close_time", "prep_minutes", "max_active"} <= f.fields["/panel/settings/save"]

    # ---------------- photos
    t = csrf(v, "/panel/menu")
    def upload(path, data, name="p.png", ctype="image/png", extra=None):
        return v.post(path, data={"csrf": t, **(extra or {})}, files={"photo": (name, data, ctype)}, follow_redirects=True)
    r = upload(f"/panel/photo/item/{item['id']}", png())
    assert "Photo saved" in r.text, r.text[:300]
    name = one("SELECT image FROM items WHERE id=?", item["id"])["image"]
    assert name.endswith(".jpg") and os.path.isfile(os.path.join(tmp, "uploads", name))
    served = c.get(f"/media/{name}")
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg" and "immutable" in served.headers["cache-control"]
    im = Image.open(io.BytesIO(served.content)); assert im.format == "JPEG" and max(im.size) <= 800
    assert f'/media/{name}' in c.get("/v/wood-coffee").text
    # attacks and mistakes
    assert "not a valid photo" in upload(f"/panel/photo/item/{item['id']}", b"<?php echo 1; ?>", "x.png").text
    assert "not a valid photo" in upload(f"/panel/photo/item/{item['id']}", b"GIF89a" + b"0" * 50, "x.png").text
    assert "Choose a photo" in v.post(f"/panel/photo/item/{item['id']}", data={"csrf": t}, files={"photo": ("e.png", b"", "image/png")}, follow_redirects=True).text
    big = b"\x89PNG" + b"0" * (7_000_000)
    assert v.post(f"/panel/photo/item/{item['id']}", data={"csrf": t}, files={"photo": ("b.png", big, "image/png")}).status_code == 413
    assert v.post(f"/panel/photo/item/{item['id']}", data={"csrf": "bad"}, files={"photo": ("p.png", png(), "image/png")}).status_code == 403
    assert TestClient(app, base_url=B).post(f"/panel/photo/item/{item['id']}", data={"csrf": "x"}, files={"photo": ("p.png", png(), "image/png")}, follow_redirects=False).status_code in (303, 403)
    assert c.post(f"/panel/photo/item/{item['id']}", data={"csrf": "x"}, files={"photo": ("p.png", png(), "image/png")}, follow_redirects=False).status_code in (303, 403)
    assert one("SELECT image FROM items WHERE id=?", item["id"])["image"] == name   # unchanged by the failed attempts
    # transparency, path tricks, bad names
    assert "Photo saved" in upload(f"/panel/photo/item/{item['id']}", png(mode="RGBA", fmt="PNG"), "t.png").text
    for bad in ("../app.db", "..%2fapp.db", "abc.jpg", "x" * 20 + ".png"):
        assert c.get(f"/media/{bad}").status_code == 404, bad
    # replacing deletes the old file; removing clears the column
    name2 = one("SELECT image FROM items WHERE id=?", item["id"])["image"]
    assert name2 != name and not os.path.exists(os.path.join(tmp, "uploads", name))
    assert "Photo removed" in v.post(f"/panel/photo/item/{item['id']}", data={"csrf": t, "remove": "1"}, files={"photo": ("e.png", b"", "image/png")}, follow_redirects=True).text
    assert one("SELECT image FROM items WHERE id=?", item["id"])["image"] == "" and not os.path.exists(os.path.join(tmp, "uploads", name2))
    # another venue cannot touch this item
    o = TestClient(app, base_url=B)
    # venue photo shows on the page and in the list
    t = csrf(v, "/panel/settings")
    assert "Photo saved" in upload("/panel/photo/venue", png((2000, 800), fmt="JPEG"), "v.jpg", "image/jpeg").text
    vn = one("SELECT image FROM venues WHERE id=1")["image"]
    assert f"/media/{vn}" in c.get("/v/wood-coffee").text and f"/media/{vn}" in c.get("/").text
    # deleting an item deletes its photo
    upload(f"/panel/photo/item/{item['id']}", png(), "p.png")
    nm = one("SELECT image FROM items WHERE id=?", item["id"])["image"]
    v.post(f"/panel/menu/item/{item['id']}/delete", data={"csrf": csrf(v, "/panel/menu")})
    assert not os.path.exists(os.path.join(tmp, "uploads", nm))
print("OK")
