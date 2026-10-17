"""Corolla House driving school manager: Flask + SQLite."""
import csv, hashlib, hmac, io, json, os, re, secrets, smtplib, sqlite3, tempfile, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "corolla.db"))
SKILLS = ["Controls", "Junctions", "Roundabouts", "Parking", "Dual carriageway", "Test route"]
CATEGORIES = ["Instructor pay", "Fuel", "Insurance", "Maintenance", "Wash and supplies",
              "Lease", "Software", "Marketing", "Admin", "Other"]
FMT = "%Y-%m-%d %H:%M"
LATE_HOURS, LATE_FEE_PCT = 48, 50   # cancellation rule: under 48h notice = 50% charge
BOOKABLE_WEEK_HOURS = 35            # 7 bookable hours x 5 days
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(DB_PATH), "uploads"))
CLIENT_OK = {"portal", "portal_request", "portal_cancel", "portal_pay", "portal_password", "logout", "static", "sw", "login"}

app = Flask(__name__)
_kf = DB_PATH + ".secret"
if not os.environ.get("SECRET_KEY") and not os.path.exists(_kf):
    with open(_kf, "w") as f:
        f.write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(_kf).read()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS_ONLY")), MAX_CONTENT_LENGTH=8 * 1024 * 1024)

# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d:
        d.close()

def q(sql, a=()):  return db().execute(sql, a).fetchall()
def q1(sql, a=()): return db().execute(sql, a).fetchone()
def x(sql, a=()):
    c = db().execute(sql, a)
    db().commit()
    return c

def _overlap_trigger(name, event):
    checks = "".join(
        f"SELECT RAISE(ABORT,'That {label} is already booked at that time.') WHERE EXISTS("
        f"SELECT 1 FROM bookings WHERE status='booked' AND id!=NEW.id AND {col}=NEW.{col} "
        f"AND start_at<NEW.end_at AND end_at>NEW.start_at);"
        for label, col in (("instructor", "instructor_id"), ("car", "car_id"), ("client", "client_id")))
    return (f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {event} ON bookings "
            f"WHEN NEW.status='booked' BEGIN {checks} END;")

def ensure_col(con, table, col, ddl):
    if col not in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    with open(os.path.join(BASE, "schema.sql")) as f:
        con.executescript(f.read())
    ensure_col(con, "users", "pay_mode", "TEXT NOT NULL DEFAULT 'hourly'")
    ensure_col(con, "users", "per_lesson_p", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(con, "car_checks", "photo", "TEXT")
    ensure_col(con, "payments", "ref", "TEXT")
    # Double-booking is impossible at database level (instructor, car, client).
    con.execute(_overlap_trigger("bk_overlap_ins", "INSERT"))
    con.execute(_overlap_trigger("bk_overlap_upd", "UPDATE OF start_at,end_at,instructor_id,car_id,client_id,status"))
    if not con.execute("SELECT 1 FROM users").fetchone():
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        email = os.environ.get("ADMIN_EMAIL", "owner@corollahouse.local")
        con.execute(
            "INSERT INTO users(name,email,pw_hash,role) VALUES(?,?,?,'admin')",
            ("Owner", email, generate_password_hash(
                pw, method="pbkdf2:sha256"
            ))
        )
        print(f"\n*** First run. Log in with {email} / {pw} (change it under Users) ***\n")
    if not con.execute("SELECT 1 FROM lesson_types").fetchone():
        con.executemany("INSERT INTO lesson_types(name,minutes,price_p) VALUES(?,?,?)",
            [("1 hour", 60, 3500), ("1.5 hours", 90, 5250), ("2 hours", 120, 7000),
             ("Assessment lesson (1h)", 60, 3000), ("Mock test (2h)", 120, 7000),
             ("Test day (2h + car hire)", 120, 11000)])
        con.executemany("INSERT INTO supplies(name,qty,reorder_level) VALUES(?,?,?)",
            [("Car wash soap", 6, 3), ("L-plates (pairs)", 10, 4), ("First aid kits", 4, 2), ("Cleaning spray", 5, 2)])
    con.commit(); con.close()

# ---------------------------------------------------------------- helpers
def pence(s):
    try:
        return int(round(float(s) * 100))
    except (TypeError, ValueError):
        return None

@app.template_filter("gbp")
def gbp(p): return "£{:,.2f}".format((p or 0) / 100)

@app.template_filter("day")
def day_f(s): return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%a %d %b") if s else ""

@app.context_processor
def _ctx():
    def csrf_field():
        session.setdefault("t", secrets.token_hex(16))
        return Markup(f'<input type="hidden" name="_t" value="{session["t"]}">')
    return dict(csrf_field=csrf_field, today=date.today().isoformat(), SKILLS=SKILLS)

def is_admin(): return g.user and g.user["role"] == "admin"

def admin_only(f):
    @wraps(f)
    def w(*a, **k):
        if not is_admin():
            abort(403)
        return f(*a, **k)
    return w

_fails = {}
@app.before_request
def guard():
    g.user = g.client = None
    if session.get("uid"):
        g.user = q1("SELECT * FROM users WHERE id=? AND active=1", (session["uid"],))
    elif session.get("cid"):
        g.client = q1("SELECT l.*, c.name FROM client_logins l JOIN clients c ON c.id=l.client_id WHERE l.client_id=? AND c.deleted=0", (session["cid"],))
    ep = request.endpoint
    if request.method == "POST" and ep != "stripe_webhook":
        if not secrets.compare_digest(request.form.get("_t", ""), session.get("t", "x")):
            abort(400, "Form expired. Go back, reload the page and try again.")
    if ep not in ("login", "static", "stripe_webhook", "sw") and not (g.user or g.client):
        return redirect(url_for("login"))
    if g.client and ep not in CLIENT_OK:
        return redirect(url_for("portal"))

@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"; r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    if request.endpoint != "static": r.headers["Cache-Control"] = "no-store"
    return r

@app.errorhandler(413)
def too_big(_): return "That file is too large (max 8 MB).", 413

def back(default="today"):
    return redirect(request.referrer or url_for(default))

def tw(d): return d.strftime(FMT)

def car_alerts(c):
    out = []
    for lbl, col in (("MOT", "mot_due"), ("Insurance", "insurance_due"), ("Tax", "tax_due"), ("Service", "service_due")):
        if c[col]:
            days = (date.fromisoformat(c[col]) - date.today()).days
            if days <= 30:
                out.append((lbl, c[col], days))
    return out

# ---------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
        if len(recent) >= 5:
            flash("Too many attempts. Wait 10 minutes.", "err")
        else:
            email, pw = request.form["email"].strip().lower(), request.form["password"]
            u = q1("SELECT * FROM users WHERE email=? AND active=1", (email,))
            if u and check_password_hash(u["pw_hash"], pw):
                session.clear(); session["uid"] = u["id"]
                return redirect(url_for("today"))
            cl = q1("SELECT l.* FROM client_logins l JOIN clients c ON c.id=l.client_id WHERE l.email=? AND c.deleted=0", (email,))
            if cl and check_password_hash(cl["pw_hash"], pw):
                session.clear(); session["cid"] = cl["client_id"]
                return redirect(url_for("portal"))
            _fails[ip] = recent + [time.time()]
            flash("Wrong email or password.", "err")
    return render_template("login.html")

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/users", methods=["GET", "POST"])
@admin_only
def users():
    if request.method == "POST":
        f = request.form
        if len(f["password"]) < 8:
            flash("Password needs at least 8 characters.", "err")
        else:
            try:
                x("INSERT INTO users(name,email,pw_hash,role,rate_p,adi_number,pay_mode,per_lesson_p) VALUES(?,?,?,?,?,?,?,?)",
                  (f["name"].strip(), f["email"].strip().lower(), generate_password_hash(f["password"]),
                   f["role"], pence(f["rate"]) or 1800, f.get("adi"),
                   "per_lesson" if f.get("pay_mode") == "per_lesson" else "hourly", pence(f.get("per_lesson")) or 0))
                flash("User added.", "ok")
            except sqlite3.IntegrityError:
                flash("That email is already used.", "err")
        return back("users")
    return render_template("users.html", rows=q("SELECT * FROM users ORDER BY active DESC, name"))

@app.post("/users/<int:i>/toggle")
@admin_only
def user_toggle(i):
    if i != g.user["id"]:
        x("UPDATE users SET active=1-active WHERE id=?", (i,))
    return back("users")

@app.post("/password")
def password():
    f = request.form
    if not check_password_hash(g.user["pw_hash"], f["old"]) or len(f["new"]) < 8:
        flash("Old password wrong, or new password under 8 characters.", "err")
    else:
        x("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(f["new"]), g.user["id"]))
        flash("Password changed.", "ok")
    return back("users")

# ---------------------------------------------------------------- bookings
BK_SQL = """SELECT b.*, c.name client, c.phone, u.name instructor, k.reg, t.name tname FROM bookings b
 JOIN clients c ON c.id=b.client_id JOIN users u ON u.id=b.instructor_id JOIN cars k ON k.id=b.car_id
 JOIN lesson_types t ON t.id=b.type_id"""

@app.get("/")
def today():
    d = request.args.get("d") or date.today().isoformat()
    sql, a = BK_SQL + " WHERE substr(b.start_at,1,10)=?", [d]
    if not is_admin():
        sql += " AND b.instructor_id=?"; a.append(g.user["id"])
    reqs = q("SELECT r.*, c.name, c.phone FROM lesson_requests r JOIN clients c ON c.id=r.client_id WHERE r.status='open' ORDER BY r.id") if is_admin() else []
    return render_template("today.html", reqs=reqs, d=d, rows=q(sql + " ORDER BY b.start_at", a),
        clients=q("SELECT id,name FROM clients WHERE deleted=0 ORDER BY name"),
        instructors=q("SELECT id,name FROM users WHERE role='instructor' AND active=1 ORDER BY name"),
        cars=q("SELECT * FROM cars ORDER BY reg"), types=q("SELECT * FROM lesson_types"),
        wait=q("SELECT w.*, c.name, c.phone FROM waitlist w JOIN clients c ON c.id=w.client_id ORDER BY w.id"))

@app.post("/booking")
def booking_add():
    f = request.form
    t = q1("SELECT * FROM lesson_types WHERE id=?", (f["type_id"],))
    car = q1("SELECT * FROM cars WHERE id=?", (f["car_id"],))
    inst = int(f["instructor_id"]) if is_admin() else g.user["id"]
    try:
        start = datetime.strptime(f["date"] + " " + f["time"], FMT)
    except ValueError:
        flash("Enter a valid date and time.", "err"); return back()
    end = start + timedelta(minutes=t["minutes"])
    if car["status"] != "available":
        flash(f"{car['reg']} is {car['status']} and cannot be booked.", "err"); return back()
    if q1("SELECT 1 FROM leave WHERE user_id=? AND status='approved' AND ? BETWEEN date_from AND date_to",
          (inst, f["date"])):
        flash("That instructor is on approved leave that day.", "err"); return back()
    try:
        x("INSERT INTO bookings(client_id,instructor_id,car_id,type_id,start_at,end_at,price_p) VALUES(?,?,?,?,?,?,?)",
          (f["client_id"], inst, car["id"], t["id"], tw(start), tw(end), t["price_p"]))
        flash("Lesson booked.", "ok")
    except sqlite3.IntegrityError as e:
        flash(str(e), "err")
    return redirect(url_for("today", d=f["date"]))

def settle(b, act, now=None):
    """Return (new_status, charge_p). The money rules live here so they can be unit tested."""
    now = now or datetime.now()
    if act == "done":   return "completed", b["price_p"]
    if act == "noshow": return "no_show", b["price_p"] * LATE_FEE_PCT // 100
    hours = (datetime.strptime(b["start_at"], FMT) - now).total_seconds() / 3600
    return "cancelled", (b["price_p"] * LATE_FEE_PCT // 100 if hours < LATE_HOURS else 0)

@app.post("/booking/<int:i>/<act>")
def booking_act(i, act):
    b = q1("SELECT * FROM bookings WHERE id=?", (i,))
    if not b or act not in ("done", "cancel", "noshow") or b["status"] != "booked":
        abort(404)
    if not is_admin() and b["instructor_id"] != g.user["id"]:
        abort(403)
    status, charge = settle(b, act)
    x("UPDATE bookings SET status=?, charge_p=?, notes=COALESCE(NULLIF(?,''),notes) WHERE id=?",
      (status, charge, request.form.get("note", ""), i))
    if charge and act == "cancel":
        flash(f"Late cancellation: {gbp(charge)} fee applied.", "ok")
    if act == "cancel":
        w = q("SELECT c.name FROM waitlist w JOIN clients c ON c.id=w.client_id")
        if w:
            flash(f"Slot free at {b['start_at'][11:]}. Waiting list: " + ", ".join(r["name"] for r in w), "ok")
    return back()

@app.post("/wait")
def wait_add():
    x("INSERT INTO waitlist(client_id,preference,created_at) VALUES(?,?,?)",
      (request.form["client_id"], request.form.get("preference"), tw(datetime.now())))
    return back()

@app.post("/wait/<int:i>/delete")
def wait_del(i):
    x("DELETE FROM waitlist WHERE id=?", (i,)); return back()

# ---------------------------------------------------------------- clients
@app.get("/clients")
def clients():
    rows = q("""SELECT c.*, COALESCE((SELECT SUM(charge_p) FROM bookings WHERE client_id=c.id AND status!='booked'),0)
      - COALESCE((SELECT SUM(amount_p) FROM payments WHERE client_id=c.id),0) owed_p
      FROM clients c WHERE deleted=0 ORDER BY name""")
    return render_template("clients.html", rows=rows)

@app.post("/clients")
def client_add():
    f = request.form
    if not f.get("name", "").strip() or not f.get("consent"):
        flash("A name and recorded consent are required.", "err"); return back("clients")
    x("""INSERT INTO clients(name,phone,email,address,dob,licence_no,emergency,theory_passed,test_date,consent_at)
         VALUES(?,?,?,?,?,?,?,?,?,?)""",
      (f["name"].strip(), f.get("phone"), f.get("email"), f.get("address"), f.get("dob") or None, f.get("licence_no"),
       f.get("emergency"), 1 if f.get("theory") else 0, f.get("test_date") or None, tw(datetime.now())))
    return back("clients")

@app.get("/clients/<int:i>")
def client(i):
    c = q1("SELECT * FROM clients WHERE id=? AND deleted=0", (i,))
    if not c: abort(404)
    owed = q1("""SELECT COALESCE((SELECT SUM(charge_p) FROM bookings WHERE client_id=:i AND status!='booked'),0)
      - COALESCE((SELECT SUM(amount_p) FROM payments WHERE client_id=:i),0) v""", dict(i=i))["v"]
    return render_template("client.html", c=c, owed=owed, portal=q1("SELECT email FROM client_logins WHERE client_id=?", (i,)),
        skills={r["skill"] for r in q("SELECT skill FROM client_skills WHERE client_id=?", (i,))},
        notes=q("SELECT n.*, u.name uname FROM lesson_notes n LEFT JOIN users u ON u.id=n.user_id WHERE client_id=? ORDER BY n.id DESC", (i,)),
        lessons=q(BK_SQL + " WHERE b.client_id=? ORDER BY b.start_at DESC", (i,)),
        pays=q("SELECT * FROM payments WHERE client_id=? ORDER BY paid_at DESC", (i,)))

@app.post("/clients/<int:i>/<what>")
def client_do(i, what):
    f = request.form
    if what == "skill" and f.get("skill") in SKILLS:
        if q1("SELECT 1 FROM client_skills WHERE client_id=? AND skill=?", (i, f["skill"])):
            x("DELETE FROM client_skills WHERE client_id=? AND skill=?", (i, f["skill"]))
        else:
            x("INSERT INTO client_skills VALUES(?,?)", (i, f["skill"]))
    elif what == "note" and f.get("text", "").strip():
        x("INSERT INTO lesson_notes(client_id,user_id,at,text) VALUES(?,?,?,?)", (i, g.user["id"], tw(datetime.now()), f["text"].strip()))
    elif what == "theory":
        x("UPDATE clients SET theory_passed=1-theory_passed WHERE id=?", (i,))
    elif what == "passed":
        x("UPDATE clients SET passed=1-passed WHERE id=?", (i,))
    elif what == "testdate":
        x("UPDATE clients SET test_date=? WHERE id=?", (f.get("test_date") or None, i))
    elif what == "payment" and is_admin():
        p = pence(f.get("amount"))
        if not p or p <= 0:
            flash("Enter a payment amount.", "err")
        else:
            x("INSERT INTO payments(client_id,amount_p,method,paid_at,note) VALUES(?,?,?,?,?)",
              (i, p, f.get("method"), f.get("paid_at") or date.today().isoformat(), f.get("note")))
    elif what == "portal" and is_admin():
        em, pw = f.get("email", "").strip().lower(), f.get("password", "")
        if not em or len(pw) < 8:
            flash("Enter an email and a password of 8+ characters.", "err")
        elif q1("SELECT 1 FROM users WHERE email=?", (em,)):
            flash("That email belongs to a staff account.", "err")
        else:
            try:
                x("INSERT OR REPLACE INTO client_logins(client_id,email,pw_hash) VALUES(?,?,?)", (i, em, generate_password_hash(pw)))
                flash("Portal access saved. Give the client their email and temporary password.", "ok")
            except sqlite3.IntegrityError:
                flash("Another client already uses that email.", "err")
    elif what == "portal_off" and is_admin():
        x("DELETE FROM client_logins WHERE client_id=?", (i,))
    elif what == "erase" and is_admin():
        # UK GDPR erasure: remove personal data, keep anonymised bookings/payments for the accounts.
        x("""UPDATE clients SET name='Deleted client', phone=NULL,email=NULL,address=NULL,dob=NULL,licence_no=NULL,
             emergency=NULL,deleted=1 WHERE id=?""", (i,))
        for t in ("client_skills", "lesson_notes", "waitlist", "client_logins", "lesson_requests"):
            x(f"DELETE FROM {t} WHERE client_id=?", (i,))
        flash("Personal data erased.", "ok"); return redirect(url_for("clients"))
    else:
        abort(400)
    return back("clients")

@app.get("/clients/<int:i>/export.json")
@admin_only
def client_export(i):
    c = q1("SELECT * FROM clients WHERE id=?", (i,))
    if not c: abort(404)
    data = dict(client=dict(c), bookings=[dict(r) for r in q("SELECT * FROM bookings WHERE client_id=?", (i,))],
                payments=[dict(r) for r in q("SELECT * FROM payments WHERE client_id=?", (i,))],
                notes=[dict(r) for r in q("SELECT * FROM lesson_notes WHERE client_id=?", (i,))])
    return Response(json.dumps(data, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=client-{i}.json"})

# ---------------------------------------------------------------- fleet, checks, supplies
@app.get("/fleet")
def fleet():
    cars = []
    for c in q("SELECT * FROM cars ORDER BY reg"):
        s = q1("""SELECT COALESCE(SUM(minutes),0)/60.0 h, COALESCE(SUM(b.charge_p),0) rev FROM bookings b
                  JOIN lesson_types t ON t.id=b.type_id WHERE b.car_id=? AND b.status='completed'""", (c["id"],))
        cost = q1("SELECT COALESCE(SUM(amount_p),0) v FROM expenses WHERE car_id=?", (c["id"],))["v"]
        w = q1("SELECT MAX(washed_at) v FROM washes WHERE car_id=?", (c["id"],))["v"]
        wash_due = (not w) or (datetime.now() - datetime.strptime(w, FMT)).days >= 2
        cars.append(dict(c=c, alerts=car_alerts(c), cost=cost, rev=s["rev"], per_h=(cost / s["h"] if s["h"] else 0),
                         wash=w, wash_due=wash_due))
    return render_template("fleet.html", cars=cars, supplies=q("SELECT * FROM supplies ORDER BY name"),
        checks=q("SELECT k.*, c.reg, u.name uname FROM car_checks k JOIN cars c ON c.id=k.car_id LEFT JOIN users u ON u.id=k.user_id ORDER BY k.id DESC LIMIT 8"))

@app.post("/fleet/car")
@admin_only
def car_add():
    f = request.form
    try:
        x("INSERT INTO cars(reg,model,transmission,mileage,mot_due,insurance_due,tax_due,service_due) VALUES(?,?,?,?,?,?,?,?)",
          (f["reg"].strip().upper(), f.get("model"), f.get("transmission", "manual"), int(f.get("mileage") or 0),
           f.get("mot_due") or None, f.get("insurance_due") or None, f.get("tax_due") or None, f.get("service_due") or None))
    except sqlite3.IntegrityError:
        flash("That registration already exists.", "err")
    return back("fleet")

@app.post("/fleet/car/<int:i>")
@admin_only
def car_update(i):
    f = request.form
    if f.get("status") in ("available", "in service", "off-road"):
        x("UPDATE cars SET status=? WHERE id=?", (f["status"], i))
    for col in ("mot_due", "insurance_due", "tax_due", "service_due"):
        if col in f:
            x(f"UPDATE cars SET {col}=? WHERE id=?", (f[col] or None, i))
    if f.get("mileage"):
        x("UPDATE cars SET mileage=? WHERE id=?", (int(f["mileage"]), i))
    return back("fleet")

def sniff(head):
    if head[:3] == b"\xff\xd8\xff": return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "webp"

@app.post("/fleet/check")
def check_add():
    f = request.form
    ok = lambda k: 1 if f.get(k) else 0
    photo, up = None, request.files.get("photo")
    if up and up.filename:
        data = up.read(); ext = sniff(data[:16])
        if not ext:
            flash("Photo must be a JPEG, PNG or WebP image.", "err"); return back("fleet")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        photo = f"{secrets.token_hex(16)}.{ext}"
        with open(os.path.join(UPLOAD_DIR, photo), "wb") as fh:
            fh.write(data)
    x("INSERT INTO car_checks(car_id,user_id,checked_at,tyres_ok,lights_ok,fluids_ok,clean_ok,damage_notes,photo) VALUES(?,?,?,?,?,?,?,?,?)",
      (f["car_id"], g.user["id"], tw(datetime.now()), ok("tyres"), ok("lights"), ok("fluids"), ok("clean"), f.get("notes"), photo))
    return back("fleet")

@app.get("/photo/<name>")
def photo(name):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", name): abort(404)
    return send_from_directory(UPLOAD_DIR, name)

@app.post("/fleet/wash/<int:i>")
def wash(i):
    x("INSERT INTO washes(car_id,user_id,washed_at) VALUES(?,?,?)", (i, g.user["id"], tw(datetime.now()))); return back("fleet")

@app.post("/fleet/supply")
@admin_only
def supply_add():
    f = request.form
    x("INSERT INTO supplies(name,qty,reorder_level) VALUES(?,?,?)", (f["name"], int(f.get("qty") or 0), int(f.get("reorder") or 2)))
    return back("fleet")

@app.post("/fleet/supply/<int:i>/<int:d>")
def supply_adj(i, d):
    if d in (0, 2):  # 0 = minus one, 2 = plus one
        x("UPDATE supplies SET qty=MAX(0,qty+?) WHERE id=?", (d - 1, i))
    return back("fleet")

# ---------------------------------------------------------------- team: shifts, leave, pay
def month_pay(month, user_id=None):
    out = []
    users_ = q("SELECT * FROM users WHERE role='instructor' AND active=1" + (" AND id=?" if user_id else ""), (user_id,) if user_id else ())
    for u in users_:
        hrs = sum((datetime.strptime(s["end_at"], FMT) - datetime.strptime(s["start_at"], FMT)).total_seconds() / 3600
                  for s in q("SELECT * FROM shifts WHERE user_id=? AND substr(start_at,1,7)=? AND end_at<=?", (u["id"], month, tw(datetime.now()))))
        bk = q("SELECT status, (julianday(end_at)-julianday(start_at))*24 h FROM bookings WHERE instructor_id=? AND substr(start_at,1,7)=?", (u["id"], month))
        taught = sum(b["h"] for b in bk if b["status"] == "completed")
        out.append(dict(u=u, hrs=hrs, taught=taught, lessons=sum(1 for b in bk if b["status"] == "completed"),
                        cancelled=sum(1 for b in bk if b["status"] in ("cancelled", "no_show")),
                        util=(taught / (hrs * 0.8) if hrs else 0), pay_p=(sum(1 for b in bk if b["status"] == "completed") * u["per_lesson_p"] if u["pay_mode"] == "per_lesson"
                                else round(hrs * u["rate_p"]))))
    return out

@app.get("/team")
def team():
    month = date.today().strftime("%Y-%m")
    uid = None if is_admin() else g.user["id"]
    sh = "SELECT s.*, u.name uname, k.reg FROM shifts s JOIN users u ON u.id=s.user_id LEFT JOIN cars k ON k.id=s.car_id WHERE substr(s.start_at,1,10)>=?"
    lv = "SELECT l.*, u.name uname FROM leave l JOIN users u ON u.id=l.user_id"
    return render_template("team.html", pay=month_pay(month, uid), month=month,
        shifts=q(sh + ("" if uid is None else " AND s.user_id=?") + " ORDER BY s.start_at LIMIT 20", (date.today().isoformat(),) + (() if uid is None else (uid,))),
        leave=q(lv + ("" if uid is None else " WHERE l.user_id=?") + " ORDER BY l.id DESC LIMIT 20", () if uid is None else (uid,)),
        instructors=q("SELECT id,name FROM users WHERE role='instructor' AND active=1"), cars=q("SELECT id,reg FROM cars"))

@app.post("/team/shift")
@admin_only
def shift_add():
    f = request.form
    d0 = datetime.strptime(f["date"], "%Y-%m-%d")
    days = [d0 - timedelta(days=d0.weekday()) + timedelta(days=k) for k in range(5)] if f.get("week") else [d0]
    for d in days:
        x("INSERT INTO shifts(user_id,car_id,start_at,end_at) VALUES(?,?,?,?)",
          (f["user_id"], f.get("car_id") or None, f"{d:%Y-%m-%d} {f['start']}", f"{d:%Y-%m-%d} {f['end']}"))
    return back("team")

@app.post("/team/shift/<int:i>/delete")
@admin_only
def shift_del(i):
    x("DELETE FROM shifts WHERE id=?", (i,)); return back("team")

@app.post("/team/leave")
def leave_add():
    f = request.form
    uid = int(f["user_id"]) if is_admin() else g.user["id"]
    if f["date_to"] < f["date_from"]:
        flash("End date is before the start date.", "err")
    else:
        x("INSERT INTO leave(user_id,date_from,date_to,kind) VALUES(?,?,?,?)", (uid, f["date_from"], f["date_to"], f.get("kind")))
    return back("team")

@app.post("/team/leave/<int:i>/<s>")
@admin_only
def leave_set(i, s):
    if s in ("approved", "declined"):
        x("UPDATE leave SET status=? WHERE id=?", (s, i))
    return back("team")

# ---------------------------------------------------------------- money
@app.route("/money", methods=["GET", "POST"])
@admin_only
def money():
    if request.method == "POST":
        f = request.form
        a = pence(f.get("amount"))
        if not a or a <= 0 or f["category"] not in CATEGORIES:
            flash("Enter a category and amount.", "err")
        else:
            x("INSERT INTO expenses(category,car_id,amount_p,spent_at,note) VALUES(?,?,?,?,?)",
              (f["category"], f.get("car_id") or None, a, f.get("spent_at") or date.today().isoformat(), f.get("note")))
        return back("money")
    return render_template("money.html", cats=CATEGORIES, cars=q("SELECT id,reg FROM cars"),
        exp=q("SELECT e.*, k.reg FROM expenses e LEFT JOIN cars k ON k.id=e.car_id ORDER BY spent_at DESC, e.id DESC LIMIT 30"),
        pays=q("SELECT p.*, c.name FROM payments p JOIN clients c ON c.id=p.client_id ORDER BY paid_at DESC, p.id DESC LIMIT 30"))

@app.get("/money/export.csv")
@admin_only
def money_csv():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["type", "date", "category_or_method", "detail", "amount_gbp"])
    for p in q("SELECT * FROM payments ORDER BY paid_at"):
        w.writerow(["income", p["paid_at"], p["method"], p["note"] or "", f"{p['amount_p']/100:.2f}"])
    for e in q("SELECT * FROM expenses ORDER BY spent_at"):
        w.writerow(["expense", e["spent_at"], e["category"], e["note"] or "", f"{e['amount_p']/100:.2f}"])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=corolla-finance.csv"})

# ---------------------------------------------------------------- stats and suggestions
def bars(items, money=False):
    mx = max([v for _, v in items] + [1])
    return [dict(label=l, pct=round(max(v, 0) / mx * 100), text=gbp(v) if money else round(v, 1) if isinstance(v, float) else v) for l, v in items]

def profit_calc(rev_p, exp_p):
    """Money logic kept in one tested function: profit and margin %."""
    return rev_p - exp_p, (round((rev_p - exp_p) / rev_p * 100) if rev_p else None)

def suggestions():
    s = []
    for c in q("""SELECT c.name, MAX(b.start_at) last FROM clients c JOIN bookings b ON b.client_id=c.id AND b.status='completed'
                  WHERE c.deleted=0 AND c.passed=0 GROUP BY c.id"""):
        if (datetime.now() - datetime.strptime(c["last"], FMT)).days >= 14:
            s.append(f"{c['name']} has not had a lesson for 14+ days. Send a nudge.")
    for c in q("""SELECT c.name FROM clients c WHERE c.deleted=0 AND (SELECT COUNT(*) FROM bookings WHERE client_id=c.id AND status='completed')=4"""):
        s.append(f"{c['name']} is on their 4th lesson. Offer a 10-lesson bundle.")
    for c in q("""SELECT c.name FROM clients c WHERE c.deleted=0 AND (SELECT COUNT(*) FROM bookings WHERE client_id=c.id AND (status='no_show' OR (status='cancelled' AND charge_p>0)))>=2"""):
        s.append(f"{c['name']} keeps cancelling late. Ask for a deposit.")
    for c in q("SELECT * FROM cars"):
        for lbl, d, days in car_alerts(c):
            s.append(f"{c['reg']} {lbl} " + ("is overdue." if days < 0 else f"is due in {days} days ({d})."))
    rows = q("""SELECT c.reg, COALESCE((SELECT SUM(amount_p) FROM expenses WHERE car_id=c.id),0) cost,
        COALESCE((SELECT SUM(t.minutes) FROM bookings b JOIN lesson_types t ON t.id=b.type_id WHERE b.car_id=c.id AND b.status='completed'),0)/60.0 h FROM cars c""")
    rows = [r for r in rows if r["h"]]
    if len(rows) > 1:
        avg = sum(r["cost"] / r["h"] for r in rows) / len(rows)
        s += [f"{r['reg']} costs {gbp(r['cost']/r['h'])}/hour, above the fleet average {gbp(avg)}. Review, service or replace."
              for r in rows if r["cost"] / r["h"] > avg * 1.25]
    s += [f"Low stock: {r['name']} ({r['qty']} left)." for r in q("SELECT * FROM supplies WHERE qty<=reorder_level")]
    wd = {r["d"]: r["n"] for r in q("SELECT strftime('%w',start_at) d, COUNT(*) n FROM bookings WHERE status!='cancelled' GROUP BY d")}
    names = {"1": "Monday", "2": "Tuesday", "3": "Wednesday", "4": "Thursday", "5": "Friday"}
    if sum(wd.get(k, 0) for k in names) >= 10:
        s.append(f"{names[min(names, key=lambda k: wd.get(k, 0))]} is your quietest weekday. Offer a 15% off-peak discount.")
    m = date.today().strftime("%Y-%m")
    exp = q1("SELECT COALESCE(SUM(amount_p),0) v FROM expenses WHERE substr(spent_at,1,7)=?", (m,))["v"]
    avg_price = q1("SELECT AVG(price_p) v FROM bookings WHERE status='completed'")["v"]
    if exp and avg_price:
        need = -(-int(exp / 4.33) // int(avg_price))
        have = q1("SELECT COUNT(*) v FROM bookings WHERE status='completed' AND start_at>=?", (tw(datetime.now() - timedelta(days=7)),))["v"]
        if have < need:
            s.append(f"You need {need} lessons this week to break even. You are at {have}.")
    return s

@app.get("/stats")
@admin_only
def stats():
    today_ = date.today(); m = today_.strftime("%Y-%m")
    one = lambda sql, a=(): q1(sql, a)["v"]
    rev = one("SELECT COALESCE(SUM(amount_p),0) v FROM payments WHERE substr(paid_at,1,7)=?", (m,))
    exp = one("SELECT COALESCE(SUM(amount_p),0) v FROM expenses WHERE substr(spent_at,1,7)=?", (m,))
    profit, margin = profit_calc(rev, exp)
    months, y, mo = [], today_.year, today_.month
    for _ in range(6):
        months.insert(0, f"{y}-{mo:02d}"); mo -= 1
        if mo == 0: y, mo = y - 1, 12
    monthly = [(mm, one("SELECT COALESCE(SUM(amount_p),0) v FROM payments WHERE substr(paid_at,1,7)=?", (mm,)),
                one("SELECT COALESCE(SUM(amount_p),0) v FROM expenses WHERE substr(spent_at,1,7)=?", (mm,))) for mm in months]
    mx = max([a for _, a, _ in monthly] + [b for _, _, b in monthly] + [1])
    mon = today_ - timedelta(days=today_.weekday()); w0, w1 = mon.isoformat(), (mon + timedelta(days=7)).isoformat()
    util = lambda sql: [(r["n"], r["h"]) for r in q(sql, (w0, w1))]
    cars_u = util("""SELECT k.reg n, COALESCE(SUM((julianday(b.end_at)-julianday(b.start_at))*24),0) h FROM cars k LEFT JOIN bookings b
        ON b.car_id=k.id AND b.status IN('booked','completed') AND b.start_at>=? AND b.start_at<? GROUP BY k.id""")
    inst_u = util("""SELECT u.name n, COALESCE(SUM((julianday(b.end_at)-julianday(b.start_at))*24),0) h FROM users u LEFT JOIN bookings b
        ON b.instructor_id=u.id AND b.status IN('booked','completed') AND b.start_at>=? AND b.start_at<? WHERE u.role='instructor' GROUP BY u.id""")
    dn = "SELECT COUNT(*) v FROM clients c WHERE deleted=0 AND "
    has = "EXISTS(SELECT 1 FROM bookings WHERE client_id=c.id AND status='completed')"
    funnel = [("New", one(dn + "NOT " + has)), ("Active", one(dn + has + " AND passed=0")),
              ("Test-ready", one(dn + "theory_passed=1 AND test_date IS NOT NULL AND passed=0")), ("Passed", one(dn + "passed=1"))]
    wd = {r["d"]: r["n"] for r in q("SELECT strftime('%w',start_at) d, COUNT(*) n FROM bookings WHERE status!='cancelled' GROUP BY d")}
    total = one("SELECT COUNT(*) v FROM bookings"); bad = one("SELECT COUNT(*) v FROM bookings WHERE status IN('cancelled','no_show')")
    pcar = [(r["reg"], r["rev"] - r["cost"]) for r in q("""SELECT k.reg, COALESCE((SELECT SUM(charge_p) FROM bookings WHERE car_id=k.id AND status='completed'),0) rev,
        COALESCE((SELECT SUM(amount_p) FROM expenses WHERE car_id=k.id),0) cost FROM cars k""")]
    hm = {(r["d"], r["h"]): r["n"] for r in q("SELECT strftime('%w',start_at) d, strftime('%H',start_at) h, COUNT(*) n FROM bookings WHERE status!='cancelled' GROUP BY d,h")}
    hours, mxh = list(range(8, 20)), max(list(hm.values()) + [1])
    heat = [dict(day=n, cells=[dict(n=hm.get((k, f"{h:02d}"), 0), a=round(hm.get((k, f"{h:02d}"), 0) / mxh, 2)) for h in hours])
            for k, n in (("1","Mon"),("2","Tue"),("3","Wed"),("4","Thu"),("5","Fri"),("6","Sat"),("0","Sun"))]
    erows = q("SELECT category, SUM(amount_p) v FROM expenses GROUP BY category ORDER BY v DESC")
    etot, off, pie = sum(r["v"] for r in erows), 25.0, []
    pal = ["#d3202f", "#1f7a4d", "#b7791f", "#3b6ea5", "#7a4fa3", "#667078", "#c2185b", "#00897b", "#8d6e63", "#455a64"]
    for n, r in enumerate(erows):
        pct = r["v"] / etot * 100
        pie.append(dict(label=r["category"], text=gbp(r["v"]), color=pal[n % len(pal)], dash=f"{pct:.2f} {100-pct:.2f}", off=f"{off:.2f}")); off -= pct
    return render_template("stats.html", heat=heat, hours=hours, pie=pie, rev=rev, exp=exp, profit=profit, margin=margin,
        monthly=[dict(m=a, r=round(b / mx * 100), e=round(c / mx * 100), rt=gbp(b), et=gbp(c)) for a, b, c in monthly],
        cars_u=bars([(n, round(h / BOOKABLE_WEEK_HOURS * 100)) for n, h in cars_u]), inst_u=bars([(n, round(h / BOOKABLE_WEEK_HOURS * 100)) for n, h in inst_u]),
        busy=bars([(n, wd.get(k, 0)) for k, n in (("1","Mon"),("2","Tue"),("3","Wed"),("4","Thu"),("5","Fri"),("6","Sat"),("0","Sun"))]),
        cats=bars([(r["category"], r["v"]) for r in q("SELECT category, SUM(amount_p) v FROM expenses GROUP BY category ORDER BY v DESC")], money=True),
        pcar=bars(pcar, money=True), funnel=bars(funnel), cancel_rate=round(bad / total * 100) if total else 0, total=total,
        sug=suggestions())

@app.get("/backup")
@admin_only
def backup():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    dest = sqlite3.connect(tmp.name); db().backup(dest); dest.close()
    return send_file(tmp.name, as_attachment=True, download_name=f"corolla-{date.today()}.db")

# ---------------------------------------------------------------- email reminders (run daily from cron)
@app.cli.command("reminders")
def reminders():
    """Email tomorrow's lessons to clients. Set SMTP_HOST, SMTP_USER, SMTP_PASS, MAIL_FROM (e.g. Brevo free tier)."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    rows = q(BK_SQL + " WHERE substr(b.start_at,1,10)=? AND b.status='booked'", (tomorrow,))
    host = os.environ.get("SMTP_HOST")
    srv = None
    if host:
        srv = smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587))); srv.starttls()
        srv.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    for b in rows:
        c = q1("SELECT email FROM clients WHERE id=?", (b["client_id"],))
        if not c["email"]:
            continue
        m = EmailMessage(); m["To"] = c["email"]; m["From"] = os.environ.get("MAIL_FROM", "lessons@corollahouse.local")
        m["Subject"] = "Your driving lesson tomorrow"
        m.set_content(f"Hi {b['client']},\n\nReminder: your lesson with {b['instructor']} is tomorrow at {b['start_at'][11:]}.\n\nCorolla House")
        srv.send_message(m) if srv else print("[dry run]", c["email"], b["start_at"])
    if srv: srv.quit()

# ---------------------------------------------------------------- client portal
def balance(cid):
    return q1("""SELECT COALESCE((SELECT SUM(charge_p) FROM bookings WHERE client_id=:i AND status!='booked'),0)
      - COALESCE((SELECT SUM(amount_p) FROM payments WHERE client_id=:i),0) v""", dict(i=cid))["v"]

@app.get("/portal")
def portal():
    cid = g.client["client_id"]
    return render_template("portal.html", c=q1("SELECT * FROM clients WHERE id=?", (cid,)), owed=balance(cid),
        upcoming=q(BK_SQL + " WHERE b.client_id=? AND b.status='booked' AND b.start_at>=? ORDER BY b.start_at", (cid, tw(datetime.now()))),
        past=q(BK_SQL + " WHERE b.client_id=? AND b.status!='booked' ORDER BY b.start_at DESC LIMIT 10", (cid,)),
        skills={r["skill"] for r in q("SELECT skill FROM client_skills WHERE client_id=?", (cid,))},
        reqs=q("SELECT * FROM lesson_requests WHERE client_id=? AND status='open'", (cid,)),
        stripe=bool(os.environ.get("STRIPE_SECRET_KEY")), late_h=LATE_HOURS, pct=LATE_FEE_PCT)

@app.post("/portal/request")
def portal_request():
    w = request.form.get("wanted", "").strip()
    if w:
        x("INSERT INTO lesson_requests(client_id,wanted,created_at) VALUES(?,?,?)", (g.client["client_id"], w[:200], tw(datetime.now())))
        flash("Request sent. We will get back to you.", "ok")
    return back("portal")

@app.post("/portal/cancel/<int:i>")
def portal_cancel(i):
    b = q1("SELECT * FROM bookings WHERE id=? AND client_id=? AND status='booked'", (i, g.client["client_id"]))
    if not b: abort(404)
    status, charge = settle(b, "cancel")
    x("UPDATE bookings SET status=?, charge_p=? WHERE id=?", (status, charge, i))
    flash(f"Lesson cancelled. Late fee: {gbp(charge)}." if charge else "Lesson cancelled. No charge.", "ok")
    return back("portal")

@app.post("/portal/password")
def portal_password():
    f = request.form
    if not check_password_hash(g.client["pw_hash"], f["old"]) or len(f["new"]) < 8:
        flash("Old password wrong, or new password under 8 characters.", "err")
    else:
        x("UPDATE client_logins SET pw_hash=? WHERE client_id=?", (generate_password_hash(f["new"]), g.client["client_id"]))
        flash("Password changed.", "ok")
    return back("portal")

@app.post("/requests/<int:i>/done")
@admin_only
def request_done(i):
    x("UPDATE lesson_requests SET status='done' WHERE id=?", (i,)); return back()

# ---------------------------------------------------------------- optional online payments (Stripe Checkout)
def stripe_checkout(cid, amount_p, base):
    data = urllib.parse.urlencode({"mode": "payment", "success_url": base + "/portal?paid=1", "cancel_url": base + "/portal",
        "client_reference_id": str(cid), "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": str(amount_p),
        "line_items[0][price_data][product_data][name]": "Driving lessons - Corolla House"}).encode()
    req = urllib.request.Request("https://api.stripe.com/v1/checkout/sessions", data=data,
                                 headers={"Authorization": "Bearer " + os.environ["STRIPE_SECRET_KEY"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["url"]

def stripe_verify(payload, header, secret, now=None):
    items = [p.split("=", 1) for p in header.split(",") if "=" in p]
    t = next((v for k, v in items if k == "t"), None)
    sigs = [v for k, v in items if k == "v1"]
    if not t or not sigs or abs((now or time.time()) - int(t)) > 300:
        return False
    exp = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(exp, s) for s in sigs)

@app.post("/portal/pay")
def portal_pay():
    owed = balance(g.client["client_id"])
    if not os.environ.get("STRIPE_SECRET_KEY") or owed <= 0:
        flash("Online payment is not available.", "err"); return back("portal")
    try:
        return redirect(stripe_checkout(g.client["client_id"], owed, request.host_url.rstrip("/")))
    except Exception:
        flash("The payment page could not be opened. Please try again or pay your instructor.", "err"); return back("portal")

@app.post("/stripe/webhook")
def stripe_webhook():
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret or not stripe_verify(request.get_data(), request.headers.get("Stripe-Signature", ""), secret):
        abort(400)
    try:
        ev = json.loads(request.get_data())
    except ValueError:
        abort(400)
    s = (ev.get("data") or {}).get("object") or {}
    if ev.get("type") == "checkout.session.completed" and s.get("payment_status") == "paid" and not q1("SELECT 1 FROM payments WHERE ref=?", (s["id"],)):
        x("INSERT INTO payments(client_id,amount_p,method,paid_at,note,ref) VALUES(?,?,?,?,?,?)",
          (int(s["client_reference_id"]), s["amount_total"], "card (Stripe)", date.today().isoformat(), "Online payment", s["id"]))
    return "", 204

@app.get("/sw.js")
def sw():
    return send_from_directory(os.path.join(BASE, "static"), "sw.js", mimetype="application/javascript")

init_db()
if __name__ == "__main__":
    app.run(debug=False, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
