"""Security audit checks: CSRF on every POST, login required, no access to other people's data.
Run: python3 -I tests/test_audit.py (from web/)."""
import os, re, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "app.db"), DEMO_SEED="1", ADMIN_PASSWORD="adminpass-123",
                  DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40, BASE_URL="https://testserver", DEMO_MODE="1")
from starlette.routing import Route
from starlette.testclient import TestClient
from app import db
from app.main import app
B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
def sql(q, *a):
    conn = db.connect(); conn.execute(q, a); conn.commit(); conn.close()
def one(q, *a):
    conn = db.connect(); r = conn.execute(q, a).fetchone(); conn.close(); return r

problems = []
def expect(cond, what):
    if not cond: problems.append(what)

with TestClient(app, base_url=B) as anon:
    # 1. every POST route refuses a request without a CSRF token, and every private page needs a login
    for r in app.routes:
        if not isinstance(r, Route) or "POST" not in (r.methods or ()): continue
        path = re.sub(r"\{[^}:]+(:int)?\}", "1", r.path)
        resp = anon.post(path, data={}, follow_redirects=False)
        expect(resp.status_code in (400, 401, 403, 404, 413, 303, 429), f"POST {path} -> {resp.status_code} without CSRF")
        expect(resp.status_code != 200, f"POST {path} answered 200 without CSRF")
    for path in ("/account", "/my-orders", "/order/1", "/account/export", "/panel", "/panel/orders", "/panel/menu",
                 "/panel/settings", "/panel/options", "/admin"):
        resp = anon.get(path, follow_redirects=False)
        expect(resp.status_code in (303, 401, 403), f"GET {path} open to anonymous ({resp.status_code})")
    # 2. other people's uploads and open redirects
    expect(anon.get("/media/../../etc/passwd").status_code in (400, 404), "path traversal in /media")
    expect(anon.get("/lang/pl?next=//evil.example", follow_redirects=False).headers.get("location") == "/", "open redirect via /lang")
    expect("evil" not in anon.get("/login?next=https://evil.example", follow_redirects=False).text.split("action=")[-1][:80], "open redirect via login next")
    # 3. security headers
    h = anon.get("/").headers
    for k in ("content-security-policy", "x-content-type-options", "referrer-policy"):
        expect(k in h, f"missing header {k}")
    expect("frame-ancestors 'none'" in h.get("content-security-policy", ""), "CSP without frame-ancestors")
    expect("unsafe-inline" not in h.get("content-security-policy", ""), "CSP allows unsafe-inline")

# 4. a customer cannot see or change another customer's order
def signup(email):
    c = TestClient(app, base_url=B)
    c.__enter__()
    c.post("/signup", data={"csrf": csrf(c, "/signup"), "name": "Test User", "email": email, "password": "correct-horse-1", "phone": "", "agree": "1"})
    return c
a, b = signup("a@example.pl"), signup("b@example.pl")
sql("UPDATE venues SET open_time='00:00', close_time='00:00' WHERE id=1")
vid = one("SELECT id FROM variants LIMIT 1")[0]
r = a.post("/v/wood-coffee/order", data={"csrf": csrf(a, "/v/wood-coffee"), "arrival_minutes": "10", "note": "", "cart": json.dumps([{"variant_id": vid, "qty": 1}]), "submit_key": "key-aaaaaaaa1"}, follow_redirects=False)
oid = one("SELECT id FROM orders ORDER BY id DESC LIMIT 1")[0]
for path in (f"/order/{oid}", f"/order/{oid}/fragment"):
    expect(b.get(path).status_code in (403, 404), f"customer B can open A's {path}")
expect(b.post(f"/order/{oid}/cancel", data={"csrf": csrf(b, "/account")}).status_code in (403, 404) or one("SELECT status FROM orders WHERE id=?", oid)[0] == "new", "customer B cancelled A's order")
expect(json.loads(a.get("/account/export").text)["account"]["email"] == "a@example.pl" and "b@example.pl" not in a.get("/account/export").text, "export leaks")
# 5. a venue cannot touch another venue's data
conn = db.connect()
conn.execute("INSERT INTO venues (slug,name,email,password_hash,status) VALUES ('other','Other','other@example.pl',(SELECT password_hash FROM venues WHERE id=1),'approved')"); conn.commit(); conn.close()
other = TestClient(app, base_url=B); other.__enter__()
other.post("/venue/login", data={"csrf": csrf(other, "/venue/login"), "email": "other@example.pl", "password": "demo-pass-12345"})
item = one("SELECT id FROM items WHERE venue_id=1 LIMIT 1")[0]; cat = one("SELECT id FROM categories WHERE venue_id=1 LIMIT 1")[0]
t = csrf(other, "/panel/menu")
before = one("SELECT COUNT(*) FROM items")[0]
for path in (f"/panel/menu/item/{item}/delete", f"/panel/menu/category/{cat}/delete", f"/panel/menu/item/{item}/toggle", f"/panel/photo/item/{item}"):
    resp = other.post(path, data={"csrf": t}, follow_redirects=False)
    expect(resp.status_code in (303, 400, 403, 404, 405), f"{path} -> {resp.status_code}")
expect(one("SELECT COUNT(*) FROM items")[0] == before, "venue deleted another venue's item")
expect(one("SELECT 1 FROM categories WHERE id=?", cat) is not None, "venue deleted another venue's category")
expect(one("SELECT status FROM orders WHERE id=?", oid)[0] == "new", "start")
resp = other.post(f"/panel/orders/{oid}/status", data={"csrf": t, "status": "preparing"}, follow_redirects=False)
expect(one("SELECT status FROM orders WHERE id=?", oid)[0] == "new", "venue changed another venue's order")
expect(other.get(f"/panel/orders?q=Test").text.count(f"#{oid}") == 0, "venue lists another venue's orders")

for p in problems: print("PROBLEM:", p)
print("OK" if not problems else f"{len(problems)} PROBLEMS")
