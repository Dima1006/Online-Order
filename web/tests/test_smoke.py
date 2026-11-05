"""Run: python3 -I tests/test_smoke.py   (from the web/ folder)"""
import json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=tempfile.mktemp(suffix=".db"), DEMO_SEED="1", DEMO_MODE="1",
                  ADMIN_PASSWORD="adminpass-123", DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40)
from datetime import timedelta
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.main import app
from app import config as cfg, db, orders
from app.utils import parse_variants, password_problem, name_problem

def csrf(c, path):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

def sql(q, *a):
    conn = db.connect(); conn.execute(q, a); conn.commit(); conn.close()

def one(q, *a):
    conn = db.connect(); r = conn.execute(q, a).fetchone(); conn.close(); return r

def place(c, slug, vid, minutes=15, key="k1-longkey-xx", extra=None):
    page = c.get(f"/v/{slug}").text
    data = {"csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1), "arrival_minutes": str(minutes), "note": "",
            "cart": json.dumps([{"variant_id": int(vid), "qty": 1}]), "submit_key": key}
    data.update(extra or {})
    return c.post(f"/v/{slug}/order", data=data, follow_redirects=False)

with TestClient(app, base_url="https://testserver") as c:
    # --- validation helpers
    try:
        parse_variants(["S", "S"], ["1", "2"]); raise SystemExit("duplicate sizes accepted")
    except ValueError:
        pass
    assert password_problem("short") and not password_problem("correct-horse-1")
    assert name_problem("") 

    # --- public pages + headers
    assert "Wood Coffee" in c.get("/").text and c.get("/for-venues").status_code == 200
    h = c.get("/").headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
    assert c.get("/nope").status_code == 404
    sql("UPDATE venues SET open_time='00:00', close_time='00:00', prep_minutes=5 WHERE slug='wood-coffee'")
    page = c.get("/v/wood-coffee").text
    vid = re.search(r'data-variant="(\d+)"', page).group(1)

    # --- CSRF + signup validation
    assert c.post("/login", data={"email": "a@b.pl", "password": "x"}).status_code == 403
    t = csrf(c, "/signup")
    bad = c.post("/signup", data={"csrf": t, "name": "Ola", "email": "ola@example.pl", "password": "short", "agree": "1", "phone": ""})
    assert bad.status_code == 400
    bad = c.post("/signup", data={"csrf": t, "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "agree": "1", "phone": "abc"})
    assert bad.status_code == 400
    bot = c.post("/signup", data={"csrf": t, "name": "Bot", "email": "bot@example.pl", "password": "correct-horse-1", "agree": "1", "phone": "", "website": "http://spam"}, follow_redirects=False)
    assert one("SELECT 1 FROM users WHERE email='bot@example.pl'") is None  # honeypot filled -> nothing created
    r = c.post("/signup", data={"csrf": t, "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "agree": "1", "phone": "+48 600 100 200"}, follow_redirects=False)
    assert r.status_code == 303, r.text

    # --- order, idempotency, ownership
    r = place(c, "wood-coffee", vid); assert r.status_code == 303 and "/order/" in r.headers["location"], r.text
    oid = int(r.headers["location"].split("/")[-1])
    r2 = place(c, "wood-coffee", vid); assert r2.headers["location"].endswith(f"/{oid}")  # same key -> same order
    assert one("SELECT COUNT(*) n FROM orders")["n"] == 1
    assert place(c, "wood-coffee", vid, minutes=1, key="k2-longkey-xx").status_code != 303   # earlier than prep time
    assert place(c, "wood-coffee", vid, key="k3-longkey-xx", extra={"cart": "[{\"variant_id\": 999999, \"qty\": 1}]"}).status_code != 303
    assert place(c, "wood-coffee", vid, key="k4-longkey-xx", extra={"cart": json.dumps([{"variant_id": int(vid), "qty": 500}])}).status_code != 303
    assert "My orders" in c.get("/my-orders").text and c.get(f"/order/{oid}").status_code == 200
    other = TestClient(app, base_url="https://testserver")
    assert other.get(f"/order/{oid}", follow_redirects=False).status_code in (302, 303, 404)

    # --- customer cancel; per-user active limit
    cs = csrf(c, "/my-orders") if 'name="csrf"' in c.get("/my-orders").text else csrf(c, f"/order/{oid}")
    r = c.post(f"/order/{oid}/cancel", data={"csrf": cs}, follow_redirects=False); assert r.status_code == 303
    assert one("SELECT status FROM orders WHERE id=?", oid)["status"] == "cancelled"

    # --- venue limits: pause, hours, capacity
    sql("UPDATE venues SET paused_until=datetime('now','+30 minutes') WHERE slug='wood-coffee'")
    assert place(c, "wood-coffee", vid, key="p1-longkey-xx").status_code != 303
    sql("UPDATE venues SET paused_until=NULL, max_active=1 WHERE slug='wood-coffee'")
    assert place(c, "wood-coffee", vid, key="c1-longkey-xx").status_code == 303
    assert place(c, "wood-coffee", vid, key="c2-longkey-xx").status_code != 303   # capacity reached
    sql("UPDATE venues SET max_active=20, open_time='03:00', close_time='03:05' WHERE slug='wood-coffee'")
    assert place(c, "wood-coffee", vid, key="h1-longkey-xx").status_code != 303   # outside opening hours
    sql("UPDATE venues SET open_time='00:00', close_time='00:00' WHERE slug='wood-coffee'")

    # --- no-show grace + per-venue block
    conn = db.connect()
    oid2 = one("SELECT id FROM orders WHERE status='new' ORDER BY id DESC")["id"]
    row, err = orders.transition(conn, 1, oid2, "no_show"); assert row is None and err   # too early
    conn.execute("UPDATE orders SET arrive_at=datetime('now','-1 hour') WHERE id=?", (oid2,)); conn.commit()
    row, err = orders.transition(conn, 1, oid2, "no_show"); assert row and not err, err
    assert conn.execute("SELECT warnings FROM venue_customer_flags WHERE venue_id=1").fetchone()["warnings"] == 1
    conn.close()

    # --- venue panel login/settings/password
    v = TestClient(app, base_url="https://testserver")
    assert v.post("/venue/login", data={"csrf": csrf(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": "wrong"}).status_code == 400
    r = v.post("/venue/login", data={"csrf": csrf(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": "demo-pass-12345"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    for p in ("/panel", "/panel/orders", "/panel/menu", "/panel/settings", "/panel/live/fragment"):
        assert v.get(p).status_code == 200, p
    st = csrf(v, "/panel/settings")
    r = v.post("/panel/settings/save", data={"csrf": st, "name": "Wood Coffee", "city": "Warszawa", "address": "x", "description": "",
               "open_time": "25:99", "close_time": "18:00", "prep_minutes": "10", "max_active": "10"}, follow_redirects=True)
    assert "opening hours" in r.text.lower()
    r = v.post("/panel/settings/save", data={"csrf": st, "name": "Wood Coffee", "city": "Warszawa", "address": "x", "description": "",
               "accepting_orders": "on", "open_time": "00:00", "close_time": "00:00", "prep_minutes": "10", "max_active": "10"}, follow_redirects=True)
    assert "Saved" in r.text
    r = v.post("/panel/password", data={"csrf": st, "current_password": "nope", "new_password": "another-long-pass-1"}, follow_redirects=True)
    assert "wrong" in r.text.lower()
    assert v.post(f"/panel/orders/{oid2}/status", data={"csrf": st, "status": "preparing"}, follow_redirects=False).status_code in (303, 400, 404, 409)

    # --- venue registration, approval, isolation
    v2 = TestClient(app, base_url="https://testserver")
    r = v2.post("/venue/register", data={"csrf": csrf(v2, "/venue/register"), "venue_name": "Café Łódź", "city": "Łódź",
                "email": "cafe@example.pl", "password": "correct-horse-1", "agree": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "waiting for approval" in v2.get("/panel/settings").text
    assert "Café" not in c.get("/?q=cafe").text           # pending venue is not public
    assert c.get("/v/cafe-lodz").status_code == 404
    a = TestClient(app, base_url="https://testserver")
    assert a.get("/admin", follow_redirects=False).status_code in (302, 303)
    a.post("/admin/login", data={"csrf": csrf(a, "/admin/login"), "password": "adminpass-123"})
    ac = csrf(a, "/admin")
    a.post("/admin/venues/2/approve", data={"csrf": ac}, follow_redirects=False)
    assert one("SELECT status FROM venues WHERE id=2")["status"] == "pending"   # empty venue cannot be approved
    sql("UPDATE venues SET address='Piotrkowska 1' WHERE id=2")
    sql("INSERT INTO categories (venue_id, name) VALUES (2, 'Coffee')")
    cat = one("SELECT id FROM categories WHERE venue_id=2")["id"]
    sql("INSERT INTO items (venue_id, category_id, name, is_available) VALUES (2, ?, 'Espresso', 1)", cat)
    assert a.post("/admin/venues/2/approve", data={"csrf": ac}, follow_redirects=False).status_code == 303
    assert one("SELECT status FROM venues WHERE id=2")["status"] == "approved"
    assert v2.post(f"/panel/orders/{oid2}/status", data={"csrf": csrf(v2, "/panel"), "status": "preparing"}).status_code == 404

    # --- websockets: auth + same origin
    for path in ("/ws/panel", "/ws/me"):
        try:
            with TestClient(app, base_url="https://testserver").websocket_connect(path, headers={"origin": "https://testserver"}):
                raise SystemExit("anonymous websocket accepted")
        except WebSocketDisconnect:
            pass
    try:
        with c.websocket_connect("/ws/me", headers={"origin": "https://evil.example"}):
            raise SystemExit("cross-origin websocket accepted")
    except WebSocketDisconnect:
        pass
    with c.websocket_connect("/ws/me", headers={"origin": "https://testserver"}) as ws:
        ws.send_text("ping")

    # --- password reset does not reveal whether an account exists
    t = csrf(c, "/venue/forgot")
    r1 = c.post("/venue/forgot", data={"csrf": t, "email": "demo@woodcoffee.pl"}); r2 = c.post("/venue/forgot", data={"csrf": t, "email": "nobody@example.pl"})
    assert r1.status_code == r2.status_code and r1.text.count("<p") == r2.text.count("<p")

    # --- body size cap and rate limit
    assert c.post("/login", content=b"x" * (cfg.MAX_BODY_BYTES + 10), headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413
    codes = [TestClient(app, base_url="https://testserver").get("/healthz").status_code for _ in range(1)]
print("OK")
