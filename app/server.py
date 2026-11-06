"""AfriEye server: connects the community health worker, the eye specialist and the family.

Flow:
  1. The CHW takes or uploads a photo. The AI scan runs on the phone and needs no internet.
  2. The CHW sees the next step. If the patient needs a specialist, the CHW sends a referral.
  3. The ophthalmologist reviews the photos and the AI result, then confirms an appointment.
  4. Only then does the SMS go to the patient and the family members they chose.

Research demo. Do not store real patient data until security, consent and ethics steps are complete.
"""
import base64, json, os, pathlib, re, secrets, sqlite3, time
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from .places import AREA, HOSPITAL, km
    from .sms import GRADES, compose, provider_from_env, segments, when_text, worst
except ImportError:
    from places import AREA, HOSPITAL, km
    from sms import GRADES, compose, provider_from_env, segments, when_text, worst

HERE = pathlib.Path(__file__).resolve().parent
try:
    TZ = ZoneInfo("Africa/Nairobi")
except Exception:  # Windows without the tzdata package
    TZ = timezone(timedelta(hours=3), "EAT")
MAX_PHOTO_BYTES = 3 * 1024 * 1024
LEVEL_TEXT = {
    "ok": ("No diabetic eye damage seen", "Keep your regular checks and diabetes care."),
    "watch": ("Early eye changes seen", "Keep your regular checks. Your eye clinic will tell you when to return."),
    "refer": ("Eye clinic visit needed", "Please attend your appointment so that a specialist can examine your eyes."),
    "urgent": ("Urgent eye care needed", "Please attend urgently. Do not wait for symptoms."),
}
AI_TEXT = {
    "urgent": "AI first result: possible severe DR. Urgent referral.",
    "refer": "AI first result: possible DR that needs an eye clinic.",
    "rescreen": "AI first result: no referable DR found. Check again in 12 months.",
}
STATUS_TEXT = {
    "no_referral": "No referral needed",
    "waiting_review": "Waiting for the eye specialist",
    "confirmed": "Appointment confirmed",
    "no_visit": "Specialist says no visit needed",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw TEXT, role TEXT NOT NULL DEFAULT 'member', code_hash TEXT, code_expires REAL, code_tries INTEGER DEFAULT 0, created REAL, area TEXT, staff_id TEXT, hospital_id TEXT, approved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, birth_year INTEGER, phone TEXT NOT NULL, village TEXT, sms_consent INTEGER NOT NULL DEFAULT 0, created_by INTEGER, created REAL, area_id TEXT);
CREATE TABLE IF NOT EXISTS links(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, user_id INTEGER NOT NULL, access TEXT NOT NULL, relation TEXT, receive_sms INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, added_by INTEGER, created REAL, UNIQUE(patient_id, user_id));
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, left_grade TEXT, right_grade TEXT, worst TEXT NOT NULL, level TEXT NOT NULL, note TEXT, clinician_id INTEGER NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, when_iso TEXT NOT NULL, location TEXT NOT NULL, clinician_id INTEGER NOT NULL, result_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS screenings(id INTEGER PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL, patient_id INTEGER NOT NULL, chw_id INTEGER NOT NULL, outcome TEXT NOT NULL, status TEXT NOT NULL, model TEXT, eyes TEXT NOT NULL, screened_at REAL, created REAL, clinician_id INTEGER, result_id INTEGER, appointment_id INTEGER, review_note TEXT, reviewed REAL, hospital_id TEXT, referred_to INTEGER, is_sample INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS patient_faces(patient_id INTEGER PRIMARY KEY, jpeg BLOB NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS consultations(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, hospital_id TEXT NOT NULL, preferred_date TEXT, reason TEXT, status TEXT NOT NULL, appointment_id INTEGER, clinician_id INTEGER, note TEXT, created REAL, reviewed REAL);
CREATE TABLE IF NOT EXISTS screening_photos(screening_id INTEGER NOT NULL, eye TEXT NOT NULL, jpeg BLOB NOT NULL, PRIMARY KEY(screening_id, eye));
CREATE TABLE IF NOT EXISTS sms(id INTEGER PRIMARY KEY, patient_id INTEGER, appointment_id INTEGER, to_phone TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, provider_id TEXT, error TEXT, parts INTEGER, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, user_id INTEGER, action TEXT, patient_id INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS login_fails(key TEXT, at REAL);
"""


def normalise_phone(raw):
    """Accepts Kenyan numbers such as 0712 345 678 or +254712345678."""
    p = re.sub(r"[\s\-()]", "", str(raw or ""))
    if re.fullmatch(r"0[17]\d{8}", p):
        p = "+254" + p[1:]
    elif re.fullmatch(r"254[17]\d{8}", p):
        p = "+" + p
    if not re.fullmatch(r"\+254[17]\d{8}", p):
        abort(400, "Enter a Kenyan mobile number, such as 0712 345 678.")
    return p


def clean(v, field, maxlen=120, required=True):
    v = str(v or "").strip()
    if required and not v:
        abort(400, f"Enter the {field}.")
    if len(v) > maxlen or any(ord(c) < 32 for c in v):
        abort(400, f"The {field} is not valid.")
    return v


def stamp(t):
    return datetime.fromtimestamp(t, TZ).strftime("%d %b %Y, %H:%M")


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=os.environ.get("AFRIEYE_SECRET"), DB=os.environ.get("AFRIEYE_DB", str(HERE / "afrieye_v2.db")),
        PUBLIC_URL=os.environ.get("AFRIEYE_PUBLIC_URL", "http://localhost:8200"), CLINIC_PHONE=os.environ.get("AFRIEYE_CLINIC_PHONE", "0700000000"),
        SMS_DETAIL=os.environ.get("AFRIEYE_SMS_DETAIL", "full"), AUTO_APPROVE=False, WEBHOOK_KEY=os.environ.get("AFRIEYE_WEBHOOK_KEY", ""), SMS=None,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.environ.get("AFRIEYE_HTTPS", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    app.config.update(config or {})
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Set AFRIEYE_SECRET to a long random value before starting the server.")
    app.config["SMS"] = app.config["SMS"] or provider_from_env()

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB"]); g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON"); g.db.executescript(SCHEMA)
        return g.db

    @app.teardown_appcontext
    def close(_):
        if "db" in g:
            g.db.close()

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def err(e):
        return jsonify(error=e.description), e.code

    @app.after_request
    def headers(r):
        r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                          "Content-Security-Policy": "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return r

    def audit(action, patient_id=None, detail=None):
        db().execute("INSERT INTO audit(at,user_id,action,patient_id,detail) VALUES(?,?,?,?,?)", (time.time(), session.get("uid"), action, patient_id, detail))

    def me():
        uid = session.get("uid")
        return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

    @app.before_request
    def csrf():
        open_paths = {"/api/auth/login", "/api/auth/activate", "/api/auth/forgot", "/api/auth/register"}
        if request.method not in ("GET", "HEAD") and request.path.startswith("/api/") and request.path not in open_paths and not request.path.startswith("/api/sms/delivery"):
            token = session.get("csrf")
            if not token or not secrets.compare_digest(request.headers.get("X-CSRF", ""), token):
                abort(403, "Your session has expired. Sign in again.")

    def login_required(fn):
        @wraps(fn)
        def inner(*a, **k):
            if not me():
                abort(401, "Sign in to continue.")
            return fn(*a, **k)
        return inner

    def role_only(role, message):
        def wrap(fn):
            @wraps(fn)
            def inner(*a, **k):
                u = me()
                if not u:
                    abort(401, "Sign in to continue.")
                if u["role"] != role:
                    abort(403, message)
                if not u["approved"]:
                    abort(403, "Your account is waiting for verification.")
                return fn(*a, **k)
            return inner
        return wrap

    clinician_only = role_only("clinician", "Only eye clinic staff can do this.")
    chw_only = role_only("chw", "Only community health workers can do this.")

    def link_for(pid):
        return db().execute("SELECT * FROM links WHERE patient_id=? AND user_id=? AND active=1", (pid, session.get("uid"))).fetchone()

    def body():
        return request.get_json(force=True, silent=True) or {}

    def issue_code(user_id):
        code = f"{secrets.randbelow(1000000):06d}"
        db().execute("UPDATE users SET code_hash=?, code_expires=?, code_tries=0 WHERE id=?", (generate_password_hash(code), time.time() + 48 * 3600, user_id))
        return code

    def send_sms(to, text, kind, patient_id=None, appointment_id=None):
        """Always records the message, so a failure can be seen and retried."""
        r = app.config["SMS"].send(to, text); now = time.time()
        cur = db().execute("INSERT INTO sms(patient_id,appointment_id,to_phone,body,kind,status,provider_id,error,parts,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (patient_id, appointment_id, to, text, kind, r["status"], r["provider_id"], r["error"], segments(text), now, now))
        return {"id": cur.lastrowid, "to": to, "status": r["status"], "ok": r["ok"], "body": text, "parts": segments(text), "kind": kind}

    def sms_targets(p):
        """The patient's phone, plus active relatives the patient chose to include."""
        out = [p["phone"]]
        for r in db().execute("SELECT u.phone FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 AND l.receive_sms=1", (p["id"],)):
            if r["phone"] not in out:
                out.append(r["phone"])
        return out

    def notify(p, kind, appt, grade):
        if not p["sms_consent"]:
            return []
        text = compose(kind, p["name"], appt["when_iso"], appt["location"], grade, app.config["PUBLIC_URL"], app.config["CLINIC_PHONE"], app.config["SMS_DETAIL"])
        return [send_sms(t, text, kind, p["id"], appt["id"]) for t in sms_targets(p)]

    def ensure_user(phone, name):
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if u:
            return u, False
        cur = db().execute("INSERT INTO users(phone,name,created) VALUES(?,?,?)", (phone, name, time.time()))
        return db().execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone(), True

    def invite_new_accounts(p):
        """After a confirmed appointment, gives a sign-in code to the patient and chosen relatives who have no account yet."""
        if not p["sms_consent"]:
            return []
        who = p["name"].split()[0]; sent = []
        rows = db().execute("SELECT u.id,u.phone,l.access FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 AND (l.access='owner' OR l.receive_sms=1) AND u.pw IS NULL", (p["id"],)).fetchall()
        for r in rows:
            code = issue_code(r["id"])
            intro = "Your eye care record is ready" if r["access"] == "owner" else f"{who} added you to see their eye care record"
            sent.append(send_sms(r["phone"], f"AfriEye: {intro}. Your code: {code}. Create your account at {app.config['PUBLIC_URL']}", "invite", p["id"]))
        return sent

    # ---- Accounts
    @app.post("/api/auth/login")
    def login():
        b = body(); phone = normalise_phone(b.get("phone")); key = phone + "|" + (request.remote_addr or "")
        db().execute("DELETE FROM login_fails WHERE at<?", (time.time() - 900,))
        if db().execute("SELECT COUNT(*) c FROM login_fails WHERE key=?", (key,)).fetchone()["c"] >= 5:
            db().commit(); abort(429, "Too many attempts. Wait 15 minutes and try again.")
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if not u or not u["pw"] or not check_password_hash(u["pw"], str(b.get("password", ""))):
            db().execute("INSERT INTO login_fails VALUES(?,?)", (key, time.time())); db().commit()
            abort(401, "That phone number or password is not right.")
        db().execute("DELETE FROM login_fails WHERE key=?", (key,))
        session.clear(); session.permanent = True; session["uid"] = u["id"]; session["csrf"] = secrets.token_urlsafe(24)
        audit("login"); db().commit(); return me_json()

    @app.post("/api/auth/activate")
    def activate():
        """Used for a first sign-in and for a forgotten password."""
        b = body(); phone = normalise_phone(b.get("phone")); pw = str(b.get("password", ""))
        if len(pw) < 8:
            abort(400, "Choose a password with at least 8 characters.")
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        bad = "That code is not right or has expired. Ask for a new one."
        if not u or not u["code_hash"] or time.time() > (u["code_expires"] or 0) or u["code_tries"] >= 5:
            abort(400, bad)
        if not check_password_hash(u["code_hash"], str(b.get("code", "")).strip()):
            db().execute("UPDATE users SET code_tries=code_tries+1 WHERE id=?", (u["id"],)); db().commit(); abort(400, bad)
        db().execute("UPDATE users SET pw=?, code_hash=NULL, code_expires=NULL, code_tries=0 WHERE id=?", (generate_password_hash(pw), u["id"]))
        session.clear(); session.permanent = True; session["uid"] = u["id"]; session["csrf"] = secrets.token_urlsafe(24)
        audit("activate"); db().commit(); return me_json()

    @app.get("/api/places")
    def places():
        return jsonify(areas=[dict(id=a["id"], name=a["name"], county=a["county"], lat=a["lat"], lng=a["lng"]) for a in AREA.values()], hospitals=list(HOSPITAL.values()))

    @app.post("/api/auth/register")
    def register():
        """Creates an account, then proves the phone is theirs with an SMS code. The password is set in the next step."""
        b = body(); role = {"chw": "chw", "clinician": "clinician", "patient": "member"}.get(b.get("role"))
        if not role:
            abort(400, "Choose whether you are a CHW, a clinician or a patient.")
        name = clean(b.get("name"), "full name"); phone = normalise_phone(b.get("phone")); area = b.get("area"); hospital = b.get("hospital")
        if db().execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone():
            abort(409, "This number already has an account. Sign in, or use an SMS code.")
        if role != "clinician" and area not in AREA:
            abort(400, "Choose your area.")
        if role == "clinician" and hospital not in HOSPITAL:
            abort(400, "Choose your hospital.")
        staff = clean(b.get("staff_id"), "CHW ID number" if role == "chw" else "licence number", 40) if role != "member" else None
        approved = 1 if role == "member" or app.config["AUTO_APPROVE"] else 0
        uid = db().execute("INSERT INTO users(phone,name,role,area,staff_id,hospital_id,approved,created) VALUES(?,?,?,?,?,?,?,?)", (phone, name, role, area if role != "clinician" else None, staff, hospital if role == "clinician" else None, approved, time.time())).lastrowid
        if role == "member":
            by = birth_year(b.get("birth_year"))
            pid = db().execute("INSERT INTO patients(name,birth_year,phone,village,area_id,sms_consent,created_by,created) VALUES(?,?,?,?,?,?,?,?)", (name, by, phone, AREA[area]["name"], area, 1 if b.get("sms_consent") else 0, uid, time.time())).lastrowid
            db().execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,?,?,?,?)", (pid, uid, "owner", "Self", uid, time.time()))
        code = issue_code(uid); send_sms(phone, f"AfriEye: Your sign-in code is {code}. It expires in 48 hours. Do not share it.", "code"); audit("register", None, role); db().commit()
        out = dict(ok=True, phone=phone)
        if app.config["SMS"].name == "test":  # no real SMS is sent in test mode, so the code is shown on screen
            out["test_code"] = code
        return jsonify(out), 201

    @app.post("/api/auth/forgot")
    def forgot():
        phone = normalise_phone(body().get("phone"))
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if u and time.time() > (u["code_expires"] or 0) - 47.9 * 3600:  # at most one code every 6 minutes
            code = issue_code(u["id"]); send_sms(phone, f"AfriEye: Your sign-in code is {code}. It expires in 48 hours. Do not share it.", "code"); db().commit()
            if app.config["SMS"].name == "test":
                return jsonify(ok=True, message="Test mode: no SMS was sent, so your code is shown here.", test_code=code)
        return jsonify(ok=True, message="If that number has an account, we sent a code by SMS.")

    @app.post("/api/auth/logout")
    def logout():
        session.clear(); return jsonify(ok=True)

    def me_json():
        u = me()
        rows = db().execute("SELECT p.id,p.name,l.access,l.relation FROM links l JOIN patients p ON p.id=l.patient_id WHERE l.user_id=? AND l.active=1 ORDER BY l.access='owner' DESC,p.name", (u["id"],)).fetchall()
        a, h = AREA.get(u["area"]), HOSPITAL.get(u["hospital_id"])
        return jsonify(signed_in=True, id=u["id"], name=u["name"], role=u["role"], phone=u["phone"], csrf=session.get("csrf"), patients=[dict(r) for r in rows], sms_mode=app.config["SMS"].name, approved=bool(u["approved"]), staff_id=u["staff_id"], area=a["name"] if a else None, area_id=u["area"], county=a["county"] if a else (h["county"] if h else None), hospital=h["name"] if h else None, hospital_id=u["hospital_id"])

    @app.get("/api/me")
    def api_me():
        if not me():
            return jsonify(signed_in=False)
        return me_json()

    # ---- Community health worker
    def birth_year(by):
        if by in (None, ""):
            return None
        try:
            by = int(by); assert 1900 <= by <= datetime.now(TZ).year
        except (ValueError, TypeError, AssertionError):
            abort(400, "Enter a valid birth year.")
        return by

    def saved_patient(b, chw_id):
        info = b.get("patient") or {}
        name = clean(info.get("name"), "patient's name"); phone = normalise_phone(info.get("phone")); area = info.get("area")
        if area not in AREA:
            abort(400, "Choose the patient's area.")
        village = AREA[area]["name"]; by = birth_year(info.get("birth_year"))
        consent = 1 if info.get("sms_consent") else 0
        row = db().execute("SELECT * FROM patients WHERE phone=? AND name=?", (phone, name)).fetchone()
        if row:
            db().execute("UPDATE patients SET sms_consent=?, village=?, area_id=?, birth_year=COALESCE(?,birth_year) WHERE id=?", (consent, village, area, by, row["id"]))
            return row["id"]
        u, _ = ensure_user(phone, name)
        if u["role"] != "member":
            abort(400, "Use the patient's own phone number.")
        pid = db().execute("INSERT INTO patients(name,birth_year,phone,village,area_id,sms_consent,created_by,created) VALUES(?,?,?,?,?,?,?,?)", (name, by, phone, village, area, consent, chw_id, time.time())).lastrowid
        db().execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,?,?,?,?)", (pid, u["id"], "owner", "Self", chw_id, time.time()))
        return pid

    def link_relative(pid, b, added_by):
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        phone = normalise_phone(b.get("phone")); name = clean(b.get("name"), "relative's name"); relation = clean(b.get("relation"), "relationship", 40)
        if phone == p["phone"]:
            abort(400, "That number already belongs to the patient.")
        u, is_new = ensure_user(phone, name)
        if u["role"] != "member":
            abort(400, "Use a family member's own phone number.")
        ex = db().execute("SELECT * FROM links WHERE patient_id=? AND user_id=?", (pid, u["id"])).fetchone()
        recv = 1 if b.get("receive_sms") else 0
        if ex and ex["active"]:
            return u, is_new, False
        if ex:
            db().execute("UPDATE links SET active=1,relation=?,receive_sms=?,access='view',added_by=? WHERE id=?", (relation, recv, added_by, ex["id"]))
        else:
            db().execute("INSERT INTO links(patient_id,user_id,access,relation,receive_sms,added_by,created) VALUES(?,?,?,?,?,?,?)", (pid, u["id"], "view", relation, recv, added_by, time.time()))
        return u, is_new, True

    def decode_photo(data):
        m = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", str(data or ""))
        if not m:
            abort(400, "A photo is missing or is not a JPEG.")
        raw = base64.b64decode(m.group(1))
        if len(raw) > MAX_PHOTO_BYTES or raw[:3] != b"\xff\xd8\xff":
            abort(400, "A photo is too large or is not a JPEG.")
        return raw

    def screening_json(s, with_patient=True):
        eyes = json.loads(s["eyes"]); out = dict(id=s["id"], client_ref=s["client_ref"], outcome=s["outcome"], ai_text=AI_TEXT[s["outcome"]], status=s["status"], status_text=STATUS_TEXT[s["status"]], model=s["model"], eyes=eyes, screened=stamp(s["screened_at"]), review_note=s["review_note"], reviewed=stamp(s["reviewed"]) if s["reviewed"] else None, waiting_hours=round((time.time() - s["created"]) / 3600, 1))
        if s["appointment_id"]:
            a = db().execute("SELECT * FROM appointments WHERE id=?", (s["appointment_id"],)).fetchone()
            out["appointment"] = dict(when_text=when_text(a["when_iso"]), location=a["location"])
        if s["result_id"]:
            r = db().execute("SELECT * FROM results WHERE id=?", (s["result_id"],)).fetchone()
            out["specialist_result"] = LEVEL_TEXT[r["level"]][0]
        if with_patient:
            p = db().execute("SELECT * FROM patients WHERE id=?", (s["patient_id"],)).fetchone(); c = db().execute("SELECT name,phone FROM users WHERE id=?", (s["chw_id"],)).fetchone()
            has_face = db().execute("SELECT 1 FROM patient_faces WHERE patient_id=?", (p["id"],)).fetchone() is not None
            out["patient"] = dict(id=p["id"], name=p["name"], birth_year=p["birth_year"], village=p["village"], sms_consent=bool(p["sms_consent"]), has_face=has_face)
            out["chw"] = dict(name=c["name"], phone=c["phone"])
            h = HOSPITAL.get(s["hospital_id"]); d = db().execute("SELECT name FROM users WHERE id=?", (s["referred_to"],)).fetchone() if s["referred_to"] else None
            out["hospital"] = h["name"] if h else None; out["referred_to"] = d["name"] if d else None
        return out

    @app.post("/api/chw/screenings")
    @chw_only
    def chw_save():
        """Saves one screening. Safe to repeat: a queued upload sent twice is only stored once."""
        b = body(); ref = clean(b.get("client_ref"), "screening reference", 64)
        old = db().execute("SELECT * FROM screenings WHERE client_ref=?", (ref,)).fetchone()
        if old:
            if old["chw_id"] != session["uid"]:
                abort(409, "That screening reference is already in use.")
            return jsonify(screening_json(old))
        if not b.get("screening_consent"):
            abort(400, "Confirm that the patient agreed to this screening.")
        eyes = b.get("eyes") or []
        if not eyes or len(eyes) > 2 or len({e.get("eye") for e in eyes}) != len(eyes) or any(e.get("eye") not in ("left", "right") for e in eyes):
            abort(400, "Scan at least one eye.")
        rank = ["rescreen", "refer", "urgent"]; summary = []
        for e in eyes:
            if e.get("level") not in rank:
                abort(400, "Finish every retake before saving this screening.")
            try:
                summary.append(dict(eye=e["eye"], level=e["level"], message=clean(e.get("message"), "result", 200, False), p_referable=round(float(e["p_referable"]), 4), p_urgent=round(float(e["p_urgent"]), 4), p_quality=round(float(e["p_quality"]), 4), retakes=int(e.get("retakes", 0))))
            except (KeyError, TypeError, ValueError):
                abort(400, "The AI scores are not valid.")
        photos = {e["eye"]: decode_photo(e.get("photo")) for e in eyes}
        outcome = max((e["level"] for e in eyes), key=rank.index)
        hospital, referred = None, None
        if outcome != "rescreen":
            hospital, referred = b.get("hospital"), b.get("clinician")
            if hospital not in HOSPITAL:
                abort(400, "Choose a hospital for this referral.")
            if not db().execute("SELECT 1 FROM users WHERE id=? AND role='clinician' AND approved=1 AND hospital_id=?", (referred, hospital)).fetchone():
                abort(400, "Choose an available clinician at that hospital.")
        face = decode_photo(b["face"]) if b.get("face") else None
        if face and not b.get("face_consent"):
            abort(400, "Confirm that the patient agreed to store their face photo.")
        pid = saved_patient(b, session["uid"])
        for rel in b.get("family") or []:
            link_relative(pid, rel, session["uid"])
        now = time.time()
        try:
            at = float(b.get("screened_at") or now)
        except (TypeError, ValueError):
            at = now
        sid = db().execute("INSERT INTO screenings(client_ref,patient_id,chw_id,outcome,status,model,eyes,screened_at,created,hospital_id,referred_to) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ref, pid, session["uid"], outcome, "no_referral" if outcome == "rescreen" else "waiting_review", clean(b.get("model"), "model name", 60, False), json.dumps(summary), min(at, now), now, hospital, referred)).lastrowid
        if face:
            db().execute("INSERT OR REPLACE INTO patient_faces VALUES(?,?,?)", (pid, face, now))
        for eye, raw in photos.items():
            db().execute("INSERT INTO screening_photos VALUES(?,?,?)", (sid, eye, raw))
        audit("screening", pid, outcome); db().commit()
        return jsonify(screening_json(db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone())), 201

    def period_starts():
        now = datetime.now(TZ)
        return dict(today=now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), month=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp(), year=now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())

    def age_group(by):
        if not by:
            return "Age not given"
        age = datetime.now(TZ).year - by
        return "Under 50" if age < 50 else "50 to 59" if age < 60 else "60 to 69" if age < 70 else "70 to 79" if age < 80 else "80 and over"

    def counted(pairs, order=None, limit=6):
        """Most common first, folding the smallest groups into Other, so that a pie never has too many slices."""
        c = {}
        for k in pairs:
            c[k] = c.get(k, 0) + 1
        if order:
            return [dict(label=k, count=c[k]) for k in order if k in c]
        items = sorted(c.items(), key=lambda x: (-x[1], x[0]))
        if len(items) > limit:
            items = items[:limit - 1] + [("Other", sum(v for _, v in items[limit - 1:]))]
        return [dict(label=k, count=v) for k, v in items]

    AGE_ORDER = ["Under 50", "50 to 59", "60 to 69", "70 to 79", "80 and over", "Age not given"]

    @app.get("/api/chw/summary")
    @chw_only
    def chw_summary():
        rows = db().execute("SELECT s.screened_at,s.outcome,s.is_sample,p.birth_year,p.village FROM screenings s JOIN patients p ON p.id=s.patient_id WHERE s.chw_id=?", (session["uid"],)).fetchall()
        out = {}
        for name, start in period_starts().items():
            part = [r for r in rows if r["screened_at"] >= start]
            out[name] = dict(count=len(part), ages=counted((age_group(r["birth_year"]) for r in part), AGE_ORDER), areas=counted(r["village"] or "Area not given" for r in part),
                             outcomes=dict(urgent=sum(r["outcome"] == "urgent" for r in part), refer=sum(r["outcome"] == "refer" for r in part), rescreen=sum(r["outcome"] == "rescreen" for r in part)))
        return jsonify(periods=out, sample_count=sum(r["is_sample"] for r in rows))

    @app.get("/api/chw/directory")
    @chw_only
    def chw_directory():
        """Hospitals and available clinicians. The phone keeps a copy, so a referral can be prepared offline."""
        doctors = db().execute("SELECT id,name,hospital_id FROM users WHERE role='clinician' AND approved=1 ORDER BY name").fetchall()
        return jsonify(hospitals=[dict(h, clinicians=[dict(id=d["id"], name=d["name"]) for d in doctors if d["hospital_id"] == h["id"]]) for h in HOSPITAL.values()])

    @app.get("/api/chw/screenings")
    @chw_only
    def chw_list():
        rows = db().execute("SELECT * FROM screenings WHERE chw_id=? ORDER BY created DESC,id DESC LIMIT 100", (session["uid"],)).fetchall()
        return jsonify([screening_json(s) for s in rows])

    @app.get("/api/screenings/<int:sid>/photo/<eye>")
    @login_required
    def photo(sid, eye):
        s = db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone(); u = me()
        if not s:
            abort(404, "Photo not found.")
        if not ((u["role"] == "clinician" and u["approved"] and s["hospital_id"] == u["hospital_id"]) or (u["role"] == "chw" and s["chw_id"] == u["id"])):
            abort(403, "You do not have access to this photo.")
        r = db().execute("SELECT jpeg FROM screening_photos WHERE screening_id=? AND eye=?", (sid, eye)).fetchone()
        if not r:
            abort(404, "Photo not found.")
        audit("view_photo", s["patient_id"], eye); db().commit()
        return Response(r["jpeg"], mimetype="image/jpeg")

    # ---- Eye specialist
    @app.get("/api/clinic/referrals")
    @clinician_only
    def referrals():
        h = me()["hospital_id"]
        waiting = db().execute("SELECT * FROM screenings WHERE status='waiting_review' AND hospital_id=? ORDER BY outcome='urgent' DESC, created ASC", (h,)).fetchall()
        done = db().execute("SELECT * FROM screenings WHERE status IN ('confirmed','no_visit') AND hospital_id=? ORDER BY reviewed DESC LIMIT 50", (h,)).fetchall()
        cons = db().execute("SELECT c.*,p.name,p.birth_year,p.village,p.phone FROM consultations c JOIN patients p ON p.id=c.patient_id WHERE c.hospital_id=? AND c.status='requested' ORDER BY c.created", (h,)).fetchall()
        return jsonify(waiting=[screening_json(s) for s in waiting], reviewed=[screening_json(s) for s in done], consultations=[dict(id=c["id"], patient_id=c["patient_id"], name=c["name"], birth_year=c["birth_year"], village=c["village"], preferred_date=c["preferred_date"], reason=c["reason"], requested=stamp(c["created"])) for c in cons])

    @app.get("/api/clinic/summary")
    @clinician_only
    def clinic_summary():
        h = me()["hospital_id"]; st = period_starts()
        rows = db().execute("SELECT s.status,s.outcome,s.reviewed,s.created,s.is_sample,s.result_id,p.village FROM screenings s JOIN patients p ON p.id=s.patient_id WHERE s.hospital_id=?", (h,)).fetchall()
        month = [r for r in rows if r["created"] >= st["month"]]
        levels = {"ok": "No DR", "watch": "Early changes", "refer": "Clinic visit", "urgent": "Urgent"}
        decided = db().execute("SELECT r.level FROM results r JOIN screenings s ON s.result_id=r.id WHERE s.hospital_id=? AND s.reviewed>=?", (h, st["month"])).fetchall()
        cons = db().execute("SELECT COUNT(*) c FROM consultations WHERE hospital_id=? AND status='requested'", (h,)).fetchone()["c"]
        return jsonify(hospital=HOSPITAL[h]["name"], county=HOSPITAL[h]["county"], waiting=sum(r["status"] == "waiting_review" for r in rows), urgent=sum(r["status"] == "waiting_review" and r["outcome"] == "urgent" for r in rows),
                       confirmed_month=sum(r["status"] == "confirmed" and (r["reviewed"] or 0) >= st["month"] for r in rows), referrals_month=len(month), consultations=cons,
                       results=counted((levels[r["level"]] for r in decided), ["No DR", "Early changes", "Clinic visit", "Urgent"]), areas=counted(r["village"] or "Area not given" for r in month), sample_count=sum(r["is_sample"] for r in rows))

    def own_consultation(cid, status="requested"):
        c = db().execute("SELECT * FROM consultations WHERE id=? AND hospital_id=?", (cid, me()["hospital_id"])).fetchone()
        if not c:
            abort(404, "Consultation request not found.")
        if c["status"] != status:
            abort(409, "This request has already been reviewed.")
        return c

    @app.post("/api/clinic/consultations/<int:cid>/confirm")
    @clinician_only
    def consultation_confirm(cid):
        c = own_consultation(cid); b = body(); when = appt_when(b); p = db().execute("SELECT * FROM patients WHERE id=?", (c["patient_id"],)).fetchone(); loc = HOSPITAL[c["hospital_id"]]["name"]
        aid = db().execute("INSERT INTO appointments(patient_id,when_iso,location,clinician_id,created) VALUES(?,?,?,?,?)", (p["id"], when, loc, session["uid"], time.time())).lastrowid
        db().execute("UPDATE consultations SET status='confirmed',appointment_id=?,clinician_id=?,reviewed=? WHERE id=?", (aid, session["uid"], time.time(), cid)); audit("consultation_confirm", p["id"], str(cid)); db().commit()
        sent = []
        if p["sms_consent"]:
            who = "".join(w[0].upper() + "." for w in p["name"].split()[:3]); text = f"AfriEye: Consultation for {who} confirmed: {when_text(when)}, {loc}. Help: {app.config['CLINIC_PHONE']}"
            sent = [send_sms(t, text, "booked", p["id"], aid) for t in sms_targets(p)]; db().commit()
        return jsonify(ok=True, sms=sent, consent=bool(p["sms_consent"]))

    @app.post("/api/clinic/consultations/<int:cid>/decline")
    @clinician_only
    def consultation_decline(cid):
        c = own_consultation(cid); note = clean(body().get("note"), "reason", 300)
        db().execute("UPDATE consultations SET status='declined',clinician_id=?,note=?,reviewed=? WHERE id=?", (session["uid"], note, time.time(), cid)); audit("consultation_decline", c["patient_id"], str(cid)); db().commit()
        return jsonify(ok=True)

    def waiting_referral(sid):
        s = db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()
        if not s:
            abort(404, "Referral not found.")
        if s["hospital_id"] != me()["hospital_id"]:
            abort(403, "This referral was sent to another hospital.")
        if s["status"] != "waiting_review":
            abort(409, "This referral has already been reviewed.")
        return s, db().execute("SELECT * FROM patients WHERE id=?", (s["patient_id"],)).fetchone()

    def grades(b):
        l, r = b.get("left") or None, b.get("right") or None
        if not l and not r:
            abort(400, "Choose your result for at least one eye.")
        for g_ in (l, r):
            if g_ and g_ not in GRADES:
                abort(400, "Choose a result from the list.")
        return l, r

    def save_result(pid, b):
        l, r = grades(b); w = worst(l, r)
        cur = db().execute("INSERT INTO results(patient_id,left_grade,right_grade,worst,level,note,clinician_id,created) VALUES(?,?,?,?,?,?,?,?)",
                           (pid, l, r, w, GRADES[w][0], clean(b.get("note"), "note", 500, False), session["uid"], time.time()))
        return cur.lastrowid, w

    def appt_when(b):
        try:
            dt = datetime.fromisoformat(f'{b.get("date")}T{b.get("time")}').replace(tzinfo=TZ)
        except (ValueError, TypeError):
            abort(400, "Choose an appointment date and time.")
        if dt < datetime.now(TZ):
            abort(400, "Choose a date and time in the future.")
        return dt.strftime("%Y-%m-%dT%H:%M")

    @app.post("/api/clinic/referrals/<int:sid>/preview")
    @clinician_only
    def preview(sid):
        s, p = waiting_referral(sid); b = body(); when = appt_when(b); loc = clean(b.get("location"), "clinic location", 60); l, r = grades(b)
        text = compose("booked", p["name"], when, loc, worst(l, r), app.config["PUBLIC_URL"], app.config["CLINIC_PHONE"], app.config["SMS_DETAIL"])
        return jsonify(body=text, parts=segments(text), to=sms_targets(p) if p["sms_consent"] else [], consent=bool(p["sms_consent"]))

    @app.post("/api/clinic/referrals/<int:sid>/confirm")
    @clinician_only
    def confirm(sid):
        """The specialist's one click: saves the result and appointment, then sends the SMS straight away."""
        s, p = waiting_referral(sid); b = body(); when = appt_when(b); loc = clean(b.get("location"), "clinic location", 60)
        rid, w = save_result(p["id"], b)
        aid = db().execute("INSERT INTO appointments(patient_id,when_iso,location,clinician_id,result_id,created) VALUES(?,?,?,?,?,?)", (p["id"], when, loc, session["uid"], rid, time.time())).lastrowid
        db().execute("UPDATE screenings SET status='confirmed',clinician_id=?,result_id=?,appointment_id=?,review_note=?,reviewed=? WHERE id=?", (session["uid"], rid, aid, clean(b.get("note"), "note", 500, False), time.time(), sid))
        audit("confirm_referral", p["id"], str(sid)); db().commit()  # the booking is safe even if the SMS then fails
        a = db().execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
        sent = notify(p, "booked", a, w); invites = invite_new_accounts(p); db().commit()
        return jsonify(screening=screening_json(db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()), sms=sent, invites=[dict(to=i["to"], status=i["status"], ok=i["ok"]) for i in invites], consent=bool(p["sms_consent"]))

    @app.post("/api/clinic/referrals/<int:sid>/no-visit")
    @clinician_only
    def no_visit(sid):
        """The specialist disagrees with the referral. The CHW sees the note and tells the patient."""
        s, p = waiting_referral(sid); b = body(); note = clean(b.get("note"), "reason for the health worker", 500)
        rid, _ = save_result(p["id"], b)
        db().execute("UPDATE screenings SET status='no_visit',clinician_id=?,result_id=?,review_note=?,reviewed=? WHERE id=?", (session["uid"], rid, note, time.time(), sid))
        audit("no_visit", p["id"], str(sid)); db().commit()
        return jsonify(screening=screening_json(db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()))

    @app.get("/api/clinic/sms")
    @clinician_only
    def sms_log():
        # Sign-in codes are shown only in test mode, so that the demo can be used with no phone network.
        test = app.config["SMS"].name == "test"
        rows = db().execute("SELECT s.id,s.to_phone,s.body,s.kind,s.status,s.error,s.parts,s.created,p.name FROM sms s LEFT JOIN patients p ON p.id=s.patient_id ORDER BY s.id DESC LIMIT 50").fetchall()
        return jsonify([dict(r, when=stamp(r["created"])) for r in rows if test or r["kind"] not in ("invite", "code")])

    @app.post("/api/clinic/sms/<int:sid>/retry")
    @clinician_only
    def retry(sid):
        s = db().execute("SELECT * FROM sms WHERE id=? AND kind NOT IN ('invite','code')", (sid,)).fetchone()
        if not s:
            abort(404, "Message not found.")
        if s["status"] != "failed":
            abort(409, "Only a failed message can be sent again.")
        r = app.config["SMS"].send(s["to_phone"], s["body"])
        db().execute("UPDATE sms SET status=?,provider_id=?,error=?,updated=? WHERE id=?", (r["status"], r["provider_id"], r["error"], time.time(), sid)); audit("sms_retry", s["patient_id"], str(sid)); db().commit()
        return jsonify(ok=r["ok"], status=r["status"])

    @app.post("/api/sms/delivery/<key>")
    def delivery(key):
        """Delivery reports from Africa's Talking. Set this URL, with its secret key, in their dashboard."""
        if not app.config["WEBHOOK_KEY"] or not secrets.compare_digest(key, app.config["WEBHOOK_KEY"]):
            abort(403, "Not allowed.")
        status = {"Success": "delivered", "Delivered": "delivered", "Sent": "sent to network", "Submitted": "sent to network", "Buffered": "sent to network", "Failed": "failed", "Rejected": "failed"}.get(request.form.get("status", ""))
        if status:
            db().execute("UPDATE sms SET status=?,error=?,updated=? WHERE provider_id=?", (status, request.form.get("failureReason"), time.time(), request.form.get("id"))); db().commit()
        return "", 200

    # ---- Patient and family
    def result_json(r):
        lab = LEVEL_TEXT[r["level"]]; c = db().execute("SELECT name FROM users WHERE id=?", (r["clinician_id"],)).fetchone()
        return dict(id=r["id"], date=datetime.fromtimestamp(r["created"], TZ).strftime("%d %b %Y"), left=r["left_grade"], right=r["right_grade"], worst=r["worst"], level=r["level"], label=lab[0], advice=lab[1], note=r["note"], clinician=c["name"] if c else "")

    def appt_json(a):
        d = db().execute("SELECT name FROM users WHERE id=?", (a["clinician_id"],)).fetchone()
        return dict(id=a["id"], when=a["when_iso"], when_text=when_text(a["when_iso"]), location=a["location"], doctor=d["name"] if d else "", upcoming=a["when_iso"] >= datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"))

    def patient_view(pid, include_family):
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        results = [result_json(r) for r in db().execute("SELECT * FROM results WHERE patient_id=? ORDER BY created DESC,id DESC", (pid,))]
        appts = [appt_json(a) for a in db().execute("SELECT * FROM appointments WHERE patient_id=? ORDER BY when_iso DESC", (pid,))]
        msgs = [dict(body=m["body"], status=m["status"], when=stamp(m["created"])) for m in db().execute("SELECT * FROM sms WHERE patient_id=? AND to_phone=? AND kind NOT IN ('invite','code') ORDER BY created DESC,id DESC LIMIT 10", (pid, p["phone"]))]
        scans = [dict(date=stamp(s["screened_at"]), text=AI_TEXT[s["outcome"]], status=STATUS_TEXT[s["status"]]) for s in db().execute("SELECT * FROM screenings WHERE patient_id=? ORDER BY screened_at DESC", (pid,))]
        up = sorted((a for a in appts if a["upcoming"]), key=lambda a: a["when"])
        last = db().execute("SELECT chw_id,hospital_id FROM screenings WHERE patient_id=? ORDER BY screened_at DESC LIMIT 1", (pid,)).fetchone()
        chw = db().execute("SELECT name,phone FROM users WHERE id=?", (last["chw_id"],)).fetchone() if last else None
        cons = [dict(id=c["id"], hospital=HOSPITAL[c["hospital_id"]]["name"], preferred_date=c["preferred_date"], reason=c["reason"], status=c["status"], note=c["note"], requested=stamp(c["created"])) for c in db().execute("SELECT * FROM consultations WHERE patient_id=? ORDER BY created DESC LIMIT 10", (pid,))]
        has_face = db().execute("SELECT 1 FROM patient_faces WHERE patient_id=?", (pid,)).fetchone() is not None
        out = dict(patient=dict(id=p["id"], name=p["name"], birth_year=p["birth_year"], sms_consent=bool(p["sms_consent"]), phone=p["phone"], village=p["village"], area_id=p["area_id"], county=AREA[p["area_id"]]["county"] if p["area_id"] in AREA else None, has_face=has_face), chw=dict(name=chw["name"], phone=chw["phone"]) if chw else None, doctor=results[0]["clinician"] if results else (up[0]["doctor"] if up else None), condition=(GRADES[results[0]["worst"]][1] if results else None), consultations=cons, status=results[0] if results else None, next_appointment=up[0] if up else None, appointments=appts, results=results, messages=msgs, screenings=scans)
        if include_family:
            out["family"] = [dict(r) for r in db().execute("SELECT l.id,u.name,u.phone,l.relation,l.access,l.receive_sms,u.pw IS NOT NULL joined FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 ORDER BY l.access='owner' DESC,u.name", (pid,))]
        return out

    def may_view_face(pid):
        u = me()
        if link_for(pid):
            return True
        if u["role"] == "chw":
            return db().execute("SELECT 1 FROM screenings WHERE patient_id=? AND chw_id=?", (pid, u["id"])).fetchone() is not None
        if u["role"] == "clinician" and u["approved"]:
            return db().execute("SELECT 1 FROM screenings WHERE patient_id=? AND hospital_id=? UNION SELECT 1 FROM consultations WHERE patient_id=? AND hospital_id=?", (pid, u["hospital_id"], pid, u["hospital_id"])).fetchone() is not None
        return False

    @app.get("/api/patients/<int:pid>/face")
    @login_required
    def face_get(pid):
        if not may_view_face(pid):
            abort(403, "You do not have access to this photo.")
        r = db().execute("SELECT jpeg FROM patient_faces WHERE patient_id=?", (pid,)).fetchone()
        if not r:
            abort(404, "No photo yet.")
        return Response(r["jpeg"], mimetype="image/jpeg")

    @app.post("/api/patients/<int:pid>/face")
    @login_required
    def face_set(pid):
        owner_required(pid); raw = decode_photo(body().get("photo"))
        db().execute("INSERT OR REPLACE INTO patient_faces VALUES(?,?,?)", (pid, raw, time.time())); audit("face_upload", pid); db().commit(); return jsonify(ok=True)

    @app.delete("/api/patients/<int:pid>/face")
    @login_required
    def face_del(pid):
        owner_required(pid); db().execute("DELETE FROM patient_faces WHERE patient_id=?", (pid,)); audit("face_remove", pid); db().commit(); return jsonify(ok=True)

    @app.post("/api/patients/<int:pid>/location")
    @login_required
    def location(pid):
        owner_required(pid); area = body().get("area")
        if area not in AREA:
            abort(400, "Choose your area.")
        db().execute("UPDATE patients SET village=?,area_id=? WHERE id=?", (AREA[area]["name"], area, pid)); audit("location", pid, area); db().commit(); return jsonify(ok=True)

    @app.post("/api/patients/<int:pid>/consultations")
    @login_required
    def consult_request(pid):
        owner_required(pid); b = body(); h = b.get("hospital")
        if h not in HOSPITAL:
            abort(400, "Choose a hospital.")
        d = str(b.get("date") or "")
        try:
            if datetime.strptime(d, "%Y-%m-%d").date() < datetime.now(TZ).date():
                abort(400, "Choose today or a later date.")
        except ValueError:
            abort(400, "Choose the date you would prefer.")
        reason = clean(b.get("reason"), "reason for the consultation", 300)
        if db().execute("SELECT 1 FROM consultations WHERE patient_id=? AND status='requested'", (pid,)).fetchone():
            abort(409, "You already have a request waiting. The clinic will reply by SMS.")
        db().execute("INSERT INTO consultations(patient_id,hospital_id,preferred_date,reason,status,created) VALUES(?,?,?,?,?,?)", (pid, h, d, reason, "requested", time.time())); audit("consultation_request", pid, h); db().commit()
        return jsonify(ok=True), 201

    @app.get("/api/patients/<int:pid>")
    @login_required
    def patient_summary(pid):
        l = link_for(pid)
        if not l:
            abort(403, "You do not have access to this person's record.")
        out = patient_view(pid, l["access"] == "owner"); out["access"] = l["access"]; out["relation"] = l["relation"]
        audit("view_record", pid); db().commit(); return jsonify(out)

    def owner_required(pid):
        l = link_for(pid)
        if not l or l["access"] != "owner":
            abort(403, "Only the patient or the person managing the record can change this.")

    @app.post("/api/patients/<int:pid>/family")
    @login_required
    def family_add(pid):
        owner_required(pid); b = body(); u, is_new, added = link_relative(pid, b, session["uid"])
        if not added:
            abort(409, "This person already has access.")
        who = db().execute("SELECT name FROM patients WHERE id=?", (pid,)).fetchone()["name"].split()[0]
        if is_new or not u["pw"]:
            s = send_sms(u["phone"], f"AfriEye: {who} added you to see their eye care record. Your code: {issue_code(u['id'])}. Create your account at {app.config['PUBLIC_URL']}", "invite", pid)
        else:
            s = send_sms(u["phone"], f"AfriEye: {who} added you to see their eye care record. Log in at {app.config['PUBLIC_URL']}", "invite", pid)
        audit("add_relative", pid, u["phone"]); db().commit(); return jsonify(ok=True, sms=dict(to=s["to"], status=s["status"], ok=s["ok"])), 201

    @app.delete("/api/patients/<int:pid>/family/<int:link>")
    @login_required
    def family_remove(pid, link):
        owner_required(pid)
        n = db().execute("UPDATE links SET active=0,receive_sms=0 WHERE id=? AND patient_id=? AND access='view'", (link, pid)).rowcount
        if not n:
            abort(404, "That person was not found.")
        audit("remove_relative", pid, str(link)); db().commit(); return jsonify(ok=True)

    @app.post("/api/patients/<int:pid>/family/<int:link>/sms")
    @login_required
    def family_sms(pid, link):
        owner_required(pid); v = 1 if body().get("receive_sms") else 0
        if not db().execute("UPDATE links SET receive_sms=? WHERE id=? AND patient_id=? AND active=1 AND access='view'", (v, link, pid)).rowcount:
            abort(404, "That person was not found.")
        audit("relative_sms", pid, f"{link}={v}"); db().commit(); return jsonify(ok=True)

    @app.post("/api/patients/<int:pid>/sms-consent")
    @login_required
    def consent(pid):
        owner_required(pid); v = 1 if body().get("consent") else 0
        db().execute("UPDATE patients SET sms_consent=? WHERE id=?", (v, pid)); audit("sms_consent", pid, str(v)); db().commit(); return jsonify(ok=True, consent=bool(v))

    # ---- Website
    @app.get("/")
    def index():
        return send_from_directory(HERE / "static", "index.html")

    @app.get("/sw.js")
    def service_worker():
        return send_from_directory(HERE / "static", "sw.js", mimetype="text/javascript")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(HERE / "static", name)

    return app
