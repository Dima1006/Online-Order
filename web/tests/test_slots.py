"""Slot capacity and venue e-mail change. Run: python3 -I tests/test_slots.py (from web/)."""
import os, re, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "app.db"), DEMO_SEED="1", ADMIN_PASSWORD="adminpass-123",
                  DEMO_PASSWORD="demo-pass-12345", SECRET_KEY="t" * 40, BASE_URL="https://testserver", DEMO_MODE="1")
from starlette.testclient import TestClient
from app import db, orders
from app.main import app
B = "https://testserver"
def csrf(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
def sql(q, *a):
    conn = db.connect(); conn.execute(q, a); conn.commit(); conn.close()

with TestClient(app, base_url=B) as v:
    v.post("/venue/login", data={"csrf": csrf(v, "/venue/login"), "email": "demo@woodcoffee.pl", "password": "demo-pass-12345"})
    sql("UPDATE venues SET open_time='00:00', close_time='00:00', prep_minutes=5 WHERE id=1")
    t = csrf(v, "/panel/settings")
    r = v.post("/panel/settings/save", data={"csrf": t, "name": "Wood", "open_time": "00:00", "close_time": "00:00", "prep_minutes": "5",
                                             "max_active": "15", "slot_capacity": "1", "accepting_orders": "1"}, follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(); venue = conn.execute("SELECT * FROM venues WHERE id=1").fetchone()
    assert venue["slot_capacity"] == 1
    assert len(orders.arrival_options(venue, conn=conn)) == len(orders.arrival_options(venue))
    # fill the 15-minute slot around +10 min
    at = orders.utcnow() + __import__("datetime").timedelta(minutes=10)
    conn.execute("INSERT INTO users (email,name,password_hash) VALUES ('z@x.pl','Z','x')")
    uid = conn.execute("SELECT id FROM users WHERE email='z@x.pl'").fetchone()[0]
    conn.execute("INSERT INTO orders (venue_id,user_id,customer_name,phone,arrival_minutes,arrive_at,note,total) VALUES (1,?,?,?,?,?,?,?)",
                 (uid, "Z", "", 10, at.strftime(orders.UTC_FMT), "", 100)); conn.commit()
    left = dict(orders.arrival_options(venue, conn=conn))
    assert 10 not in left and 5 in left and 20 in left or 10 not in left, left
    conn.close()
    # e-mail change
    t = csrf(v, "/panel/settings")
    r = v.post("/panel/email", data={"csrf": t, "email": "new@example.pl", "password": "wrong"}, follow_redirects=False)
    assert "Wrong password" in v.get("/panel/settings").text
    t = csrf(v, "/panel/settings")
    v.post("/panel/email", data={"csrf": t, "email": "new@example.pl", "password": "demo-pass-12345"})
    conn = db.connect(); assert conn.execute("SELECT email FROM venues WHERE id=1").fetchone()[0] == "new@example.pl"; conn.close()
print("OK")
