"""Privacy/Terms pages, consent, data export, CAPTCHA. Run: python3 -I tests/test_legal.py (from web/)."""
import os, re, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "app.db"), DEMO_SEED="1", ADMIN_PASSWORD="adminpass-123",
                  DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40, BASE_URL="https://testserver",
                  COMPANY_NAME="Test Sp. z o.o.")
from starlette.testclient import TestClient
from app import legal
from app.main import app
B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

with TestClient(app, base_url=B) as c:
    for p in ("/privacy", "/terms"):
        r = c.get(p); assert r.status_code == 200 and "Test Sp. z o.o." in r.text, p
    assert "/privacy" in c.get("/").text
    base = {"name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "phone": ""}
    r = c.post("/signup", data={"csrf": csrf(c, "/signup"), **base})
    assert r.status_code == 400 and "Terms" in r.text            # consent is required
    r = c.post("/signup", data={"csrf": csrf(c, "/signup"), **base, "agree": "1"}, follow_redirects=False)
    assert r.status_code == 303
    r = c.get("/account/export"); assert r.status_code == 200
    data = json.loads(r.text); assert data["account"]["email"] == "ola@example.pl" and "password_hash" not in r.text
    assert "attachment" in r.headers["content-disposition"]
    assert c.get("/privacy").status_code == 200
with TestClient(app, base_url=B) as anon:
    assert anon.get("/account/export", follow_redirects=False).status_code in (303, 401, 403)

# CAPTCHA: when configured, a missing token blocks the form
from app import config as cfg
cfg.TURNSTILE_SITE_KEY, cfg.TURNSTILE_SECRET, cfg.CAPTCHA_ENABLED = "site", "secret", True
async def fake_ok(*a, **k): return False
with TestClient(app, base_url=B) as c:
    assert "cf-turnstile" in c.get("/contact").text
    r = c.post("/contact", data={"csrf": csrf(c, "/contact"), "name": "A", "email": "a@example.pl", "message": "hello there friend"})
    assert r.status_code == 400 and "robot" in r.text
print("OK")
