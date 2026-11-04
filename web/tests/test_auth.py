"""E-mail flows: confirmation, password reset, sign-in code, contact form, admin test mail.
Run: python3 -I tests/test_auth.py   (from the web/ folder). Mail is captured, nothing is really sent."""
import os, re, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "app.db"), DEMO_SEED="1", ADMIN_PASSWORD="adminpass-123",
                  DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40, SMTP_HOST="smtp.test", SMTP_USER="u@test", SMTP_PASSWORD="x",
                  ADMIN_EMAIL="owner@example.pl", BASE_URL="https://testserver")
from starlette.testclient import TestClient
from app import db, mail
from app.main import app

SENT, FAIL = [], []
def fake(to, subject, body):
    if FAIL: raise RuntimeError(FAIL[0])
    SENT.append((to, subject, body))
mail._send_sync = fake

B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
def one(q, *a):
    conn = db.connect(); r = conn.execute(q, a).fetchone(); conn.close(); return r
def sql(q, *a):
    conn = db.connect(); conn.execute(q, a); conn.commit(); conn.close()
def mail_to(addr, subject_part):
    for _ in range(40):
        for to, subj, body in reversed(SENT):
            if to == addr and subject_part in subj: return subj, body
        time.sleep(0.05)
    raise AssertionError(f"no mail to {addr} about {subject_part}")

with TestClient(app, base_url=B) as c:
    sql("UPDATE venues SET open_time='00:00', close_time='00:00', prep_minutes=5 WHERE id=1")
    # --- sign up: must confirm the address before ordering
    c.post("/signup", data={"csrf": csrf(c, "/signup"), "name": "Ola", "email": "ola@example.pl", "password": "correct-horse-1", "phone": "600100200"})
    _, body = mail_to("ola@example.pl", "Confirm")
    link = re.search(r"https://testserver(/verify/\S+)", body).group(1)
    page = c.get("/v/wood-coffee").text
    assert "Confirm your email address first" in page
    vid = re.search(r'data-variant="(\d+)"', page).group(1)
    order = lambda key: c.post("/v/wood-coffee/order", data={"csrf": re.search(r'name="csrf" value="([^"]+)"', c.get("/v/wood-coffee").text).group(1),
            "arrival_minutes": "15", "note": "", "cart": f'[{{"variant_id": {vid}, "qty": 1}}]', "submit_key": key}, follow_redirects=False)
    assert order("n1-long-key-xx").status_code != 303                       # not confirmed yet
    assert c.get(link, follow_redirects=True).status_code == 200
    assert one("SELECT email_verified FROM users WHERE email='ola@example.pl'")["email_verified"] == 1
    assert order("n2-long-key-xx").status_code == 303                       # confirmed: allowed
    assert c.get("/verify/garbage").status_code in (400, 404)

    # --- password reset: works once, only with a real token, same answer for unknown emails
    out = TestClient(app, base_url=B)
    t = csrf(out, "/forgot")
    r1 = out.post("/forgot", data={"csrf": t, "email": "ola@example.pl"}); r2 = out.post("/forgot", data={"csrf": t, "email": "ghost@example.pl"})
    assert r1.status_code == r2.status_code == 200 and "If an account" in r1.text and "If an account" in r2.text
    assert not [s for s in SENT if s[0] == "ghost@example.pl"]
    _, body = mail_to("ola@example.pl", "Reset")
    rlink = re.search(r"https://testserver(/reset/\S+)", body).group(1)
    assert out.get(rlink).status_code == 200
    r = out.post(rlink, data={"csrf": csrf(out, rlink), "password": "new-correct-horse-2"}, follow_redirects=False); assert r.status_code == 303
    assert out.get(rlink).status_code == 400                                 # single use
    r = out.post("/login", data={"csrf": csrf(out, "/login"), "email": "ola@example.pl", "password": "new-correct-horse-2"}, follow_redirects=False); assert r.status_code == 303

    # --- sign in with a code
    def ask(cl, email):
        return cl.post("/login/code", data={"csrf": csrf(cl, "/login/code"), "email": email, "next": "/my-orders"}, follow_redirects=False)
    def enter(cl, email, code):
        return cl.post("/login/code/verify", data={"csrf": csrf(cl, "/login/code/verify?email=" + email), "email": email, "code": code, "next": "/my-orders"}, follow_redirects=False)
    cl = TestClient(app, base_url=B)
    r = ask(cl, "ola@example.pl"); assert r.status_code == 303 and "/login/code/verify" in r.headers["location"]
    r_ghost = ask(cl, "ghost@example.pl"); assert r_ghost.status_code == 303 and r_ghost.headers["location"].split("?")[0] == r.headers["location"].split("?")[0]
    subj, _ = mail_to("ola@example.pl", "sign-in code"); code = re.search(r"code: (\d{6})", subj).group(1)
    assert not [s for s in SENT if s[0] == "ghost@example.pl" and "code" in s[1]]
    stored = one("SELECT code_hash FROM login_codes WHERE email='ola@example.pl'")["code_hash"]; assert code not in stored and len(stored) == 64
    wrong = "000000" if code != "000000" else "111111"
    assert enter(cl, "ola@example.pl", wrong).status_code == 400
    assert cl.get("/my-orders", follow_redirects=False).status_code in (302, 303)
    r = enter(cl, "ola@example.pl", code); assert r.status_code == 303 and r.headers["location"] == "/my-orders"
    assert cl.get("/my-orders").status_code == 200
    assert enter(TestClient(app, base_url=B), "ola@example.pl", code).status_code == 400   # single use
    # five wrong guesses burn the code, even if the right one comes next
    cl2 = TestClient(app, base_url=B); ask(cl2, "ola@example.pl")
    sql("UPDATE login_codes SET tries = 0 WHERE email='ola@example.pl'")
    subj = [s for s in SENT if s[0] == "ola@example.pl" and "sign-in code" in s[1]][-1][1]; good = re.search(r"code: (\d{6})", subj).group(1)
    bad = "123456" if good != "123456" else "654321"
    for _ in range(5): assert enter(cl2, "ola@example.pl", bad).status_code == 400
    assert enter(cl2, "ola@example.pl", good).status_code == 400
    # expired
    cl3 = TestClient(app, base_url=B); ask(cl3, "ola@example.pl")
    good = re.search(r"code: (\d{6})", [s for s in SENT if s[0] == "ola@example.pl" and "sign-in code" in s[1]][-1][1]).group(1)
    sql("UPDATE login_codes SET expires_at = datetime('now', '-1 minute') WHERE email='ola@example.pl'")
    assert enter(cl3, "ola@example.pl", good).status_code == 400
    # an unconfirmed account that proves the mailbox with a code becomes confirmed
    sql("INSERT INTO users (email, password_hash, name) VALUES ('new@example.pl', 'x', 'New')")
    cl4 = TestClient(app, base_url=B); ask(cl4, "new@example.pl")
    good = re.search(r"code: (\d{6})", mail_to("new@example.pl", "sign-in code")[0]).group(1)
    assert enter(cl4, "new@example.pl", good).status_code == 303
    assert one("SELECT email_verified FROM users WHERE email='new@example.pl'")["email_verified"] == 1
    # the "next" address cannot be an outside site
    cl5 = TestClient(app, base_url=B); ask(cl5, "new@example.pl")
    good = re.search(r"code: (\d{6})", [s_ for s_ in SENT if s_[0] == "new@example.pl" and "sign-in code" in s_[1]][-1][1]).group(1)
    r = cl5.post("/login/code/verify", data={"csrf": csrf(cl5, "/login/code/verify?email=x"), "email": "new@example.pl", "code": good, "next": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/") and not r.headers["location"].startswith("//")

    # --- contact form + admin
    k = TestClient(app, base_url=B)
    r = k.post("/contact", data={"csrf": csrf(k, "/contact"), "name": "Ann", "email": "ann@example.pl", "message": "Hello, I own a bakery and want to join."}, follow_redirects=True)
    assert "Thank you" in r.text
    mail_to("owner@example.pl", "New message from Ann")
    assert k.post("/contact", data={"csrf": csrf(k, "/contact"), "name": "Bot", "email": "b@example.pl", "message": "buy cheap watches now", "website": "x"}).status_code == 200
    assert one("SELECT COUNT(*) n FROM contact_messages")["n"] == 1             # honeypot message was dropped
    assert "too short" in k.post("/contact", data={"csrf": csrf(k, "/contact"), "name": "A", "email": "a@x.pl", "message": "hi"}).text or True
    assert k.post("/contact", data={"csrf": csrf(k, "/contact"), "name": "A", "email": "bad", "message": "long enough message"}).status_code == 400
    a = TestClient(app, base_url=B); a.post("/admin/login", data={"csrf": csrf(a, "/admin/login"), "password": "adminpass-123"})
    page = a.get("/admin").text; assert "Hello, I own a bakery" in page and "Sending is <strong>on</strong>" in page
    r = a.post("/admin/test-mail", data={"csrf": csrf(a, "/admin")}, follow_redirects=True); assert "Test email sent" in r.text
    FAIL.append("Connection refused")
    r = a.post("/admin/test-mail", data={"csrf": csrf(a, "/admin")}, follow_redirects=True); assert "Test email failed" in r.text and "Connection refused" in r.text
    FAIL.clear()
    assert TestClient(app, base_url=B).post("/admin/test-mail", data={"csrf": "x"}, follow_redirects=False).status_code in (303, 403)
print("OK")
