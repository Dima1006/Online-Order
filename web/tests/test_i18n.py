"""Polish: every page must be fully translated. Run: python3 -I tests/test_i18n.py (from web/). Prints missing texts."""
import os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "app.db"), DEMO_SEED="1", ADMIN_PASSWORD="adminpass-123",
                  DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40, BASE_URL="https://testserver", DEMO_MODE="1",
                  SMTP_HOST="smtp.test", SMTP_USER="u@test", SMTP_PASSWORD="x")
from starlette.testclient import TestClient
from app import db, i18n, mail
from app.main import app
mail._send_sync = lambda *a: None
B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

MISSING = {}
def check(c, path, label=None, status=200):
    r = c.get(path); assert r.status_code == status, (path, r.status_code)
    for t in i18n.untranslated(r.text): MISSING.setdefault(t, path)
    return r

with TestClient(app, base_url=B, headers={"accept-language": "en"}) as c:
    for p in ("/", "/?q=latte", "/?q=zzzzqqq", "/for-venues", "/login", "/signup", "/forgot", "/login/code", "/contact",
              "/venue/register", "/venue/login", "/venue/forgot", "/v/wood-coffee"):
        check(c, p)
    c.post("/signup", data={"csrf": csrf(c, "/signup"), "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "phone": "", "agree": "1"})
    for p in ("/", "/v/wood-coffee", "/account", "/my-orders", "/contact"):
        check(c, p)
    # place an order, view it
    conn = db.connect(); v = conn.execute("SELECT id FROM variants LIMIT 1").fetchone()[0]; conn.close()
    import json
    r = c.post("/v/wood-coffee/order", data={"csrf": csrf(c, "/v/wood-coffee"), "arrival_minutes": "10", "note": "x",
               "cart": json.dumps([{"variant_id": v, "qty": 1}]), "submit_key": "abcdefgh12345"}, follow_redirects=True)
    for t in i18n.untranslated(r.text): MISSING.setdefault(t, "order page")
    c.post("/account/update", data={"csrf": csrf(c, "/account"), "name": "", "phone": ""})
    for t in i18n.untranslated(c.get("/account").text): MISSING.setdefault(t, "/account after error")
    # some error messages
    r = c.post("/contact", data={"csrf": csrf(c, "/contact"), "name": "", "email": "x", "message": "a"})
    for t in i18n.untranslated(r.text): MISSING.setdefault(t, "contact error")
    check(c, "/nope", status=404)

with TestClient(app, base_url=B, headers={"accept-language": "en"}) as v:
    v.post("/venue/login", data={"csrf": csrf(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": "demo-pass-12345"})
    for p in ("/panel", "/panel/orders", "/panel/orders?q=x&status=new", "/panel/menu", "/panel/options", "/panel/settings"):
        check(v, p)
    r = v.post("/panel/settings/save", data={"csrf": csrf(v, "/panel/settings"), "name": "Wood", "open_time": "zz", "close_time": "00:00", "prep_minutes": "5", "max_active": "15"}, follow_redirects=True)
    for t in i18n.untranslated(r.text): MISSING.setdefault(t, "settings error")
    v.post("/venue/register", data={"csrf": csrf(v, "/venue/login"), "venue_name": "x"})
with TestClient(app, base_url=B, headers={"accept-language": "en"}) as n:   # a brand-new venue, with the checklist
    n.post("/venue/register", data={"csrf": csrf(n, "/venue/register"), "venue_name": "Nowa Kawa", "city": "Łódź", "email": "n@example.pl", "password": "correct-horse-1", "agree": "1"})
    for p in ("/panel", "/panel/settings", "/panel/menu", "/panel/options", "/v/nowa-kawa"):
        check(n, p)

# English must stay English
with TestClient(app, base_url=B, headers={"accept-language": "en"}) as e:
    assert "Order ahead." in e.get("/").text
    e.get("/lang/pl?next=/"); assert "Zamów" in e.get("/").text
    e.get("/lang/en?next=/"); assert "Order ahead." in e.get("/").text
    assert e.get("/lang/xx", follow_redirects=False).status_code == 404

skip = re.compile(r"^(Ahead|English|Polski|/.*|#\d+ .*|ul\. .*|Milk drinks|Small|Medium|Large|Americano|Black Tea|Green Tea|Cocoa|Milkshake|Strawberry|Chocolate|Cheesecake|Demo venue.*|Wood Coffee,.*|\d+ × .*|.* · Ahead|.*\d zł.*|Wood Coffee|Nowa Kawa|Ola|Latte|Cappuccino|Flat white|Espresso|Croissant|Coffee|Tea|Desserts|Pastries|[\W\d]*|.*@.*)$")
bad = {t: p for t, p in MISSING.items() if not skip.match(t)}
for t, p in sorted(bad.items(), key=lambda x: x[1]): print(f"{p}\t{t}")
print("MISSING:", len(bad))
