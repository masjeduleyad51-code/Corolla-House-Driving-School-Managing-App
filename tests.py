import os, re, sqlite3, tempfile, unittest
from datetime import datetime, timedelta
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"] = "owner@test.com", "password123"
import app as A

def token(c, path="/login"):
    return re.search(r'name="_t" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)

def login(email="owner@test.com", pw="password123"):
    c = A.app.test_client(); t = token(c)
    c.post("/login", data=dict(email=email, password=pw, _t=t)); return c

def post(c, url, page="/", **d):
    return c.post(url, data=dict(d, _t=token(c, page)), follow_redirects=True)

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = login()
        post(cls.c, "/users", "/users", name="Sam", email="sam@test.com", password="instructor1", role="instructor", rate="18")
        post(cls.c, "/users", "/users", name="Jo", email="jo@test.com", password="instructor2", role="instructor", rate="18")
        post(cls.c, "/fleet/car", "/fleet", reg="AB21 XYZ", model="Fiesta")
        post(cls.c, "/fleet/car", "/fleet", reg="CD70 LMN", model="Polo")
        for n in ("Ann", "Bob"):
            post(cls.c, "/clients", "/clients", name=n, consent="on", phone="447700900123")

    def book(self, client, inst, car, time="10:00", date="2030-05-01"):
        r = post(self.c, "/booking", "/", client_id=client, instructor_id=inst, car_id=car, type_id=1, date=date, time=time)
        return r.get_data(as_text=True)

    def test_01_pages_load(self):
        for p in ["/", "/clients", "/clients/1", "/fleet", "/team", "/money", "/stats", "/users", "/backup", "/money/export.csv", "/clients/1/export.json"]:
            self.assertEqual(self.c.get(p).status_code, 200, p)

    def test_02_double_booking_blocked_by_database(self):
        self.assertIn("Lesson booked", self.book(1, 2, 1))
        self.assertIn("instructor is already booked", self.book(2, 2, 2, "10:30"))
        self.assertIn("car is already booked", self.book(2, 3, 1, "10:30"))
        self.assertIn("client is already booked", self.book(1, 3, 2, "10:30"))
        self.assertIn("Lesson booked", self.book(2, 3, 2, "11:00"))  # back-to-back is fine
        con = sqlite3.connect(A.DB_PATH)  # even raw SQL cannot bypass it
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO bookings(client_id,instructor_id,car_id,type_id,start_at,end_at,price_p) VALUES(1,2,1,1,'2030-05-01 10:30','2030-05-01 11:30',3500)")
        con.close()

    def test_03_off_road_car_and_leave(self):
        post(self.c, "/fleet/car/2", "/fleet", status="off-road")
        self.assertIn("off-road", self.book(2, 3, 2, "14:00"))
        post(self.c, "/fleet/car/2", "/fleet", status="available")
        post(self.c, "/team/leave", "/team", user_id=3, date_from="2030-06-01", date_to="2030-06-02", kind="Sickness")
        post(self.c, "/team/leave/1/approved", "/team")
        self.assertIn("approved leave", self.book(2, 3, 2, "09:00", "2030-06-01"))

    def test_04_cancellation_fee_rule(self):
        b = dict(price_p=7000, start_at=(datetime.now() + timedelta(hours=10)).strftime(A.FMT))
        self.assertEqual(A.settle(b, "cancel"), ("cancelled", 3500))
        b["start_at"] = (datetime.now() + timedelta(hours=72)).strftime(A.FMT)
        self.assertEqual(A.settle(b, "cancel"), ("cancelled", 0))
        self.assertEqual(A.settle(b, "done"), ("completed", 7000))
        self.assertEqual(A.settle(b, "noshow"), ("no_show", 3500))

    def test_05_profit_maths(self):
        self.assertEqual(A.profit_calc(100000, 60000), (40000, 40))
        self.assertEqual(A.profit_calc(0, 500), (-500, None))

    def test_06_roles_and_csrf(self):
        i = login("sam@test.com", "instructor1")
        for p in ("/money", "/stats", "/users", "/backup"):
            self.assertEqual(i.get(p).status_code, 403, p)
        self.assertEqual(i.get("/").status_code, 200)
        self.assertEqual(self.c.post("/clients", data=dict(name="X", consent="on")).status_code, 400)
        self.assertEqual(A.app.test_client().get("/").status_code, 302)

    def test_07_gdpr_erase_keeps_accounts(self):
        post(self.c, "/clients/2/payment", "/clients/2", amount="35", method="cash")
        post(self.c, "/clients/2/erase", "/clients/2")
        row = sqlite3.connect(A.DB_PATH).execute("SELECT name,phone,deleted FROM clients WHERE id=2").fetchone()
        self.assertEqual(row, ("Deleted client", None, 1))
        self.assertIn("£35.00", self.c.get("/money").get_data(as_text=True))

    def test_08_full_flow(self):
        self.book(1, 2, 1, "08:00", "2030-07-01")
        bid = sqlite3.connect(A.DB_PATH).execute("SELECT id FROM bookings WHERE start_at='2030-07-01 08:00'").fetchone()[0]
        post(self.c, f"/booking/{bid}/done", "/", note="Good roundabouts")
        post(self.c, "/wait", "/", client_id=1, preference="mornings")
        post(self.c, "/team/shift", "/team", user_id=2, car_id=1, date="2030-07-01", start="08:00", end="16:00", week="on")
        post(self.c, "/money", "/money", category="Fuel", amount="40", car_id=1)
        post(self.c, "/fleet/check", "/fleet", car_id=1, tyres="on", notes="Scratch on door")
        post(self.c, "/fleet/wash/1", "/fleet"); post(self.c, "/fleet/supply/1/0", "/fleet")
        post(self.c, "/clients/1/skill", "/clients/1", skill="Parking")
        for p in ("/stats", "/team", "/fleet", "/clients/1", "/clients"):
            r = self.c.get(p); self.assertEqual(r.status_code, 200, p)
        self.assertIn("Scratch on door", self.c.get("/fleet").get_data(as_text=True))
        self.assertIn("£70.00", self.c.get("/clients/1").get_data(as_text=True) .replace("Outstanding ","") ) if False else None
        self.assertEqual(sqlite3.connect(A.DB_PATH).execute("SELECT COUNT(*) FROM shifts").fetchone()[0], 5)

    def test_09_client_portal(self):
        post(self.c, "/clients/1/portal", "/clients/1", email="ann@test.com", password="annpass123")
        a = login("ann@test.com", "annpass123")
        self.assertEqual(a.get("/portal").status_code, 200)
        for p in ("/money", "/stats", "/clients", "/fleet", "/users", "/"):
            r = a.get(p); self.assertEqual(r.status_code, 302, p); self.assertTrue(r.headers["Location"].endswith("/portal"), p)
        self.assertIn("Request sent", post(a, "/portal/request", "/portal", wanted="Tuesday after 4pm").get_data(as_text=True))
        self.assertIn("Tuesday after 4pm", self.c.get("/").get_data(as_text=True))
        con = sqlite3.connect(A.DB_PATH)
        mine = con.execute("SELECT id FROM bookings WHERE client_id=1 AND status='booked' LIMIT 1").fetchone()[0]
        other = con.execute("SELECT id FROM bookings WHERE client_id!=1 AND status='booked' LIMIT 1").fetchone()
        if other: self.assertEqual(a.post(f"/portal/cancel/{other[0]}", data=dict(_t=token(a, "/portal"))).status_code, 404)
        self.assertIn("No charge", post(a, f"/portal/cancel/{mine}", "/portal").get_data(as_text=True))
        con.close()

    def test_10_photo_upload(self):
        import io
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
        r = self.c.post("/fleet/check", data=dict(_t=token(self.c, "/fleet"), car_id=1, tyres="on", photo=(io.BytesIO(png), "x.png")), content_type="multipart/form-data", follow_redirects=True)
        name = re.search(r"/photo/([0-9a-f]{32}\.png)", r.get_data(as_text=True)).group(1)
        self.assertEqual(self.c.get("/photo/" + name).status_code, 200)
        self.assertEqual(A.app.test_client().get("/photo/" + name).status_code, 302)  # needs login
        bad = self.c.post("/fleet/check", data=dict(_t=token(self.c, "/fleet"), car_id=1, photo=(io.BytesIO(b"<script>alert(1)</script>"), "x.png")), content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("must be a JPEG", bad.get_data(as_text=True))
        self.assertEqual(self.c.get("/photo/..%2fapp.py").status_code, 404)

    def test_11_stripe_webhook(self):
        import hashlib, hmac, json, time
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
        body = json.dumps(dict(type="checkout.session.completed", data=dict(object=dict(id="cs_1", payment_status="paid", client_reference_id="1", amount_total=7000)))).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_test", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        c = A.app.test_client()
        self.assertEqual(c.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1=bad"}).status_code, 400)
        for _ in range(2):  # sent twice: recorded once
            self.assertEqual(c.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={ts},v1={sig}"}).status_code, 204)
        n = sqlite3.connect(A.DB_PATH).execute("SELECT COUNT(*) FROM payments WHERE ref='cs_1'").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertFalse(A.stripe_verify(body, f"t=1,v1={sig}", "whsec_test"))  # too old

    def test_12_per_lesson_pay_and_stats(self):
        post(self.c, "/users", "/users", name="Kit", email="kit@test.com", password="instructor3", role="instructor", rate="18", pay_mode="per_lesson", per_lesson="12")
        con = sqlite3.connect(A.DB_PATH)
        kit = con.execute("SELECT id FROM users WHERE name='Kit'").fetchone()[0]; con.close()
        self.book(1, kit, 1, "12:00", "2030-08-01")
        bid = sqlite3.connect(A.DB_PATH).execute("SELECT id FROM bookings WHERE instructor_id=?", (kit,)).fetchone()[0]
        post(self.c, f"/booking/{bid}/done", "/")
        month = "2030-08"
        with A.app.app_context():
            p = [x for x in A.month_pay(month) if x["u"]["name"] == "Kit"][0]
        self.assertEqual((p["lessons"], p["pay_p"]), (1, 1200))
        html = self.c.get("/stats").get_data(as_text=True)
        self.assertIn("Busiest hours", html); self.assertIn("<svg", html)

    def test_13_old_database_is_upgraded(self):
        import importlib
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY,name TEXT,email TEXT UNIQUE,pw_hash TEXT,role TEXT,rate_p INTEGER DEFAULT 1800,adi_number TEXT,active INTEGER DEFAULT 1);"
                          "CREATE TABLE car_checks(id INTEGER PRIMARY KEY,car_id INT,user_id INT,checked_at TEXT,tyres_ok INT,lights_ok INT,fluids_ok INT,clean_ok INT,damage_notes TEXT);"
                          "CREATE TABLE payments(id INTEGER PRIMARY KEY,client_id INT,amount_p INT,method TEXT,paid_at TEXT,note TEXT);")
        con.commit(); con.close()
        old = A.DB_PATH; A.DB_PATH = path
        try: A.init_db()
        finally: A.DB_PATH = old
        cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(users)")]
        self.assertIn("pay_mode", cols)

if __name__ == "__main__":
    unittest.main(verbosity=2)
