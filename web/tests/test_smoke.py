import os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["DEMO_SEED"] = "1"; os.environ["DEMO_MODE"] = "1"; os.environ["ADMIN_PASSWORD"] = "adminpass-123"
from starlette.testclient import TestClient
from app.main import app
from app import config as cfg

def tok(c, path="/login"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

with TestClient(app) as c:
    assert "Wood Coffee" in c.get("/").text
    assert "latte" in c.get("/?q=latte&partial=1").text.lower()
    h = c.get("/").headers; assert "frame-ancestors 'none'" in h["content-security-policy"]
    page = c.get("/v/wood-coffee").text; assert "Log in to place your order" in page
    vid = re.search(r'data-variant="(\d+)"', page).group(1)

    # CSRF is enforced
    assert c.post("/login", data={"email": "a@b.pl", "password": "x"}).status_code == 403

    # sign up -> order
    r = c.post("/signup", data={"csrf": tok(c, "/signup"), "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "phone": "+48123"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = c.get("/v/wood-coffee").text; assert "Ordering as" in page and "Ola" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/v/wood-coffee/order", data={"csrf": csrf, "arrival_minutes": "15", "note": "x", "cart": f'[{{"variant_id":{vid},"qty":2,"price":1}}]'}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/order/"), r.text
    oid = r.headers["location"].split("/")[-1]
    assert "New" in c.get(f"/order/{oid}").text and "My orders" in c.get("/my-orders").text

    # venue panel + live websocket
    v = TestClient(app, base_url="http://testserver")
    v.get("/venue/login")
    r = v.post("/venue/login", data={"csrf": tok(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": os.environ.get("DEMO_PASSWORD", "")}, follow_redirects=False)
    assert r.status_code == 400  # random demo password unknown here -> wrong login rejected
    r = v.post("/venue/register", data={"csrf": tok(v, "/venue/register"), "venue_name": "Café Łódź", "city": "Łódź", "email": "cafe@example.pl", "password": "correct-horse-1"}, follow_redirects=False)
    assert r.status_code == 303
    assert "waiting for approval" in v.get("/panel/settings").text
    assert c.get("/?q=cafe").text.count("Café") == 0  # pending venue is not public
    for p in ("/panel", "/panel/orders", "/panel/menu", "/panel/settings", "/panel/live/fragment"): assert v.get(p).status_code == 200, p
    # approve via admin
    a = TestClient(app)
    a.post("/admin/login", data={"csrf": tok(a, "/admin/login"), "password": "adminpass-123"})
    assert "Café Łódź" in a.get("/admin").text
    acs = re.search(r'name="csrf" value="([^"]+)"', a.get("/admin").text).group(1)
    assert a.post("/admin/venues/2/approve", data={"csrf": acs}, follow_redirects=False).status_code == 303
    # Ola cannot see another customer's order; venue 2 cannot touch venue 1's orders
    assert v.post(f"/panel/orders/{oid}/status", data={"csrf": tok(v, "/panel"), "status": "preparing"}).status_code == 404

    # websocket needs auth + same origin
    from starlette.websockets import WebSocketDisconnect
    try:
        TestClient(app).websocket_connect("/ws/panel", headers={"origin": "http://testserver"}).__enter__(); raise SystemExit("anon ws accepted")
    except WebSocketDisconnect: pass
    with c.websocket_connect("/ws/me", headers={"origin": "http://testserver"}) as ws:
        # venue 1 (demo) is not logged in here, so update the order straight through the service layer
        import asyncio
        from app import orders, db, events
        conn = db.connect(); assert orders.transition(conn, 1, int(oid), "preparing"); conn.close()
        asyncio.get_event_loop_policy()
        c.portal.call(events.announce, int(oid))
        msg = ws.receive_json(); assert msg["status"] == "preparing", msg
    assert "Preparing" in c.get(f"/order/{oid}/fragment").text
    # logout
    assert c.post("/logout", data={"csrf": tok(c, "/account")}, follow_redirects=False).status_code == 303
    assert c.get("/account", follow_redirects=False).status_code == 303
print("SMOKE OK")
