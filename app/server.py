"""AfriEye server: connects the community health worker, the eye specialist and the family.

Flow:
  1. The CHW takes or uploads a photo. The AI scan runs on the phone and needs no internet.
  2. The CHW sees the next step. If the patient needs a specialist, the CHW sends a referral.
  3. The ophthalmologist reviews the photos and the AI result, then confirms an appointment.
  4. Only then does the SMS go to the patient and the family members they chose.

Research demo. Do not store real patient data until security, consent and ethics steps are complete.
"""
import base64, json, os, pathlib, re, secrets, sqlite3, time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo
import mimetypes

# Windows sometimes maps .js to text/plain, which stops browsers running the scripts.
for _ext in (".js", ".mjs"):
    mimetypes.add_type("text/javascript", _ext)

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from .mailer import EMAIL_RE, email_from_env
    from .places import AREA, HOSPITAL, km
    from .sms import GRADES, GRADES_SW, compose, provider_from_env, segments, when_text, worst
    from .tips import TIPS, tip_for_week
except ImportError:
    from mailer import EMAIL_RE, email_from_env
    from places import AREA, HOSPITAL, km
    from sms import GRADES, GRADES_SW, compose, provider_from_env, segments, when_text, worst
    from tips import TIPS, tip_for_week

HERE = pathlib.Path(__file__).resolve().parent
try:
    TZ = ZoneInfo("Africa/Nairobi")
except Exception:  # Windows without the tzdata package
    TZ = timezone(timedelta(hours=3), "EAT")
MAX_PHOTO_BYTES = 3 * 1024 * 1024
COUNTIES = sorted({a["county"] for a in AREA.values()})
TREATMENTS = {"laser": "Laser treatment", "injection": "Eye injection (anti-VEGF)", "surgery": "Eye surgery (vitrectomy)", "cataract": "Cataract surgery", "glasses": "Glasses", "monitor": "Watch and check again", "other": "Other"}
VISION = ["6/6", "6/9", "6/12", "6/18", "6/24", "6/36", "6/60", "3/60", "CF", "HM", "PL", "NPL"]
RECALL_DAYS = {"ok": 365, "watch": 180, "rescreen": 365}   # yearly, or after 6 months for early changes
# The CHW training quiz. Answers are checked here, so they can't be read from the phone.
QUIZ = [1, 2, 0, 2, 1]
LANGS = ["en", "sw", "ki", "luy", "luo", "kln", "kam", "mas", "guz", "mer", "tuv", "so", "nyf"]
CONDITIONS = ["High blood pressure", "Kidney disease", "Heart disease", "High cholesterol", "Cataract", "Glaucoma", "Previous eye surgery or laser", "Pregnancy", "HIV", "Smoking"]
DIABETES_TYPES = ["Type 1", "Type 2", "Not sure"]
# How the patient's eye care is paid for. SHA is Kenya's Social Health Authority (it replaced NHIF in 2024).
COVER = {"sha": "SHA (Social Health Authority)", "ngo": "NGO or charity support", "private": "Private insurance", "none": "No cover", "unsure": "Not sure"}
COVER_SW = {"sha": "SHA (Mamlaka ya Afya ya Jamii)", "ngo": "Msaada wa shirika au NGO", "private": "Bima ya kibinafsi", "none": "Hana bima", "unsure": "Hajui"}
LEVEL_TEXT = {
    "ok": ("No diabetic eye damage seen", "Keep your regular checks and diabetes care."),
    "watch": ("Early eye changes seen", "Keep your regular checks. Your eye clinic will tell you when to return."),
    "refer": ("Eye clinic visit needed", "Please attend your appointment so that a specialist can examine your eyes."),
    "urgent": ("Urgent eye care needed", "Please attend urgently. Do not wait for symptoms."),
}
AI_TEXT_SHORT = {"urgent": "Possible severe DR", "refer": "Possible DR", "rescreen": "No referable DR"}
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
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, birth_year INTEGER, phone TEXT NOT NULL, village TEXT, sms_consent INTEGER NOT NULL DEFAULT 0, created_by INTEGER, created REAL, area_id TEXT, cover TEXT, cover_ref TEXT, cover_help TEXT, cover_chw INTEGER, cover_updated REAL);
CREATE TABLE IF NOT EXISTS links(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, user_id INTEGER NOT NULL, access TEXT NOT NULL, relation TEXT, receive_sms INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, added_by INTEGER, created REAL, UNIQUE(patient_id, user_id));
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, left_grade TEXT, right_grade TEXT, worst TEXT NOT NULL, level TEXT NOT NULL, note TEXT, clinician_id INTEGER NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, when_iso TEXT NOT NULL, location TEXT NOT NULL, clinician_id INTEGER NOT NULL, result_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS screenings(id INTEGER PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL, patient_id INTEGER NOT NULL, chw_id INTEGER NOT NULL, outcome TEXT NOT NULL, status TEXT NOT NULL, model TEXT, eyes TEXT NOT NULL, screened_at REAL, created REAL, clinician_id INTEGER, result_id INTEGER, appointment_id INTEGER, review_note TEXT, reviewed REAL, hospital_id TEXT, referred_to INTEGER, is_sample INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS patient_faces(patient_id INTEGER PRIMARY KEY, jpeg BLOB NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS consultations(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, hospital_id TEXT NOT NULL, preferred_date TEXT, reason TEXT, status TEXT NOT NULL, appointment_id INTEGER, clinician_id INTEGER, note TEXT, created REAL, reviewed REAL);
CREATE TABLE IF NOT EXISTS screening_photos(screening_id INTEGER NOT NULL, eye TEXT NOT NULL, jpeg BLOB NOT NULL, PRIMARY KEY(screening_id, eye));
CREATE TABLE IF NOT EXISTS sms(id INTEGER PRIMARY KEY, patient_id INTEGER, appointment_id INTEGER, to_phone TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, provider_id TEXT, error TEXT, parts INTEGER, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS emails(id INTEGER PRIMARY KEY, patient_id INTEGER, to_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created REAL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, link TEXT, created REAL, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, author_id INTEGER NOT NULL, target_id INTEGER NOT NULL, target_role TEXT NOT NULL, stars INTEGER NOT NULL, comment TEXT, created REAL, UNIQUE(patient_id, target_id));
CREATE TABLE IF NOT EXISTS visit_requests(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, area_id TEXT, note TEXT, status TEXT NOT NULL, source TEXT, chw_id INTEGER, created REAL, updated REAL, kind TEXT, people INTEGER, place TEXT, preferred TEXT);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, appointment_id INTEGER NOT NULL, user_id INTEGER NOT NULL, remind_at REAL NOT NULL, channels TEXT NOT NULL, sent REAL, created REAL);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, created_by INTEGER NOT NULL, author_role TEXT, title TEXT, pdf BLOB NOT NULL, created REAL, status TEXT, to_user INTEGER, released REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, user_id INTEGER, action TEXT, patient_id INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS login_fails(key TEXT, at REAL);
CREATE TABLE IF NOT EXISTS vitals(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, by_user INTEGER, by_role TEXT, glucose REAL, glucose_kind TEXT, hba1c REAL, sys INTEGER, dia INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS treatments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, clinician_id INTEGER NOT NULL, treatment TEXT NOT NULL, eye TEXT, va_right TEXT, va_left TEXT, note TEXT, created REAL);
CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, chw_id INTEGER, kind TEXT NOT NULL, appointment_id INTEGER, status TEXT NOT NULL, note TEXT, created REAL, done REAL);
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


def upgrade(con):
    """Adds columns that older databases don't have yet, so existing data keeps working."""
    want = {"users": [("email", "TEXT"), ("lang", "TEXT")], "patients": [("conditions", "TEXT")], "screenings": [("alerted", "REAL"), ("escalated", "REAL")], "reports": [("status", "TEXT"), ("to_user", "INTEGER"), ("released", "REAL")], "visit_requests": [("kind", "TEXT"), ("people", "INTEGER"), ("place", "TEXT"), ("preferred", "TEXT")]}
    want["patients"] += [("cover", "TEXT"), ("cover_ref", "TEXT"), ("cover_help", "TEXT"), ("cover_chw", "INTEGER"), ("cover_updated", "REAL"), ("tips", "INTEGER"), ("tip_sent", "REAL"), ("recall_notified", "REAL")]
    want["users"] += [("county", "TEXT"), ("trained", "REAL"), ("train_score", "INTEGER"), ("rejected", "TEXT"), ("reviewed_by", "INTEGER"), ("reviewed_at", "REAL")]
    want["appointments"] = [("attended", "TEXT"), ("attended_at", "REAL"), ("attended_by", "INTEGER")]
    for table, cols in want.items():
        have = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
        for name, kind in cols:
            if have and name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    con.commit()


def stamp(t):
    return datetime.fromtimestamp(t, TZ).strftime("%d %b %Y, %H:%M")


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=os.environ.get("AFRIEYE_SECRET"), DB=os.environ.get("AFRIEYE_DB", str(HERE / "afrieye_v2.db")),
        PUBLIC_URL=os.environ.get("AFRIEYE_PUBLIC_URL", "http://localhost:8200"), CLINIC_PHONE=os.environ.get("AFRIEYE_CLINIC_PHONE", "0700000000"),
        SMS_DETAIL=os.environ.get("AFRIEYE_SMS_DETAIL", "full"), AUTO_APPROVE=False, USSD_KEY=os.environ.get("AFRIEYE_USSD_KEY", ""), WEBHOOK_KEY=os.environ.get("AFRIEYE_WEBHOOK_KEY", ""), SMS=None, EMAIL=None,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.environ.get("AFRIEYE_HTTPS", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    app.config.update(config or {})
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Set AFRIEYE_SECRET to a long random value before starting the server.")
    app.config["SMS"] = app.config["SMS"] or provider_from_env()
    app.config["EMAIL"] = app.config["EMAIL"] or email_from_env()

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB"]); g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON"); g.db.executescript(SCHEMA)
            upgrade(g.db)
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
        if request.method not in ("GET", "HEAD") and request.path.startswith("/api/") and request.path not in open_paths and not request.path.startswith("/api/sms/delivery") and not request.path.startswith("/api/ussd/"):
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

    def demo_locked():
        """On a public demo, the shared demo accounts can't have their password, email or data changed by visitors."""
        u = me(); pre = app.config.get("LOCKED_PREFIXES") or ()
        if u and any(u["phone"].startswith(x) for x in pre):
            abort(403, "This is a shared demo account, so this setting is switched off. Register your own account to try it.")

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
    county_only = role_only("county", "Only county health officers can see this.")

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

    def clean_email(v):
        v = str(v or "").strip().lower()
        if not v:
            return None
        if len(v) > 120 or not EMAIL_RE.fullmatch(v):
            abort(400, "Enter a valid email address, such as name@example.com.")
        return v

    def send_email(to, subject, text, kind, patient_id=None):
        """Records every email, like the SMS, so a failure can be seen."""
        r = app.config["EMAIL"].send(to, subject, text)
        cur = db().execute("INSERT INTO emails(patient_id,to_email,subject,body,kind,status,error,created) VALUES(?,?,?,?,?,?,?,?)", (patient_id, to, subject, text, kind, r["status"], r["error"], time.time()))
        return {"id": cur.lastrowid, "to": to, "status": r["status"], "ok": r["ok"], "kind": kind}

    def email_targets(p):
        """The patient's email, plus chosen family members who gave one."""
        out = []
        for r in db().execute("SELECT u.email FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 AND (l.access='owner' OR l.receive_sms=1) AND u.email IS NOT NULL ORDER BY l.access='owner' DESC", (p["id"],)):
            if r["email"] not in out:
                out.append(r["email"])
        return out

    def email_family(p, subject, text, kind, sw=None):
        if not p["sms_consent"]:
            return []
        out = []
        for t in email_targets(p):
            u = db().execute("SELECT lang FROM users WHERE email=?", (t,)).fetchone()
            out.append(send_email(t, *(sw if sw and u and u["lang"] == "sw" else (subject, text)), kind, p["id"]))
        return out

    def email_clinicians(user_ids, subject, text, kind, patient_id):
        sent = []
        for r in db().execute(f"SELECT email FROM users WHERE role='clinician' AND approved=1 AND email IS NOT NULL AND id IN ({','.join('?' * len(user_ids))})", list(user_ids)) if user_ids else []:
            sent.append(send_email(r["email"], subject, text, kind, patient_id))
        return sent

    def appointment_email(p, when, loc, doctor, result_label, advice, lang="en"):
        url = app.config["PUBLIC_URL"]; who = p["name"].split()[0]
        if lang == "sw":
            lines = [f"Mpendwa {who} na familia,", "", "Miadi ya macho imethibitishwa.", "", f"Lini: {when_text(when, 'sw')}", f"Wapi: {loc}", f"Daktari wa macho: {doctor}"]
            if result_label:
                lines += ["", f"Matokeo ya macho: {result_label}."]
            lines += ["", f"Ona rekodi kamili kwa kuingia {url}", f"Maswali? Piga {app.config['CLINIC_PHONE']}.", "", "Ujumbe huu si utambuzi wa ugonjwa."]
            return f"AfriEye: miadi ya macho {when_text(when, 'sw')}", "\n".join(lines)
        lines = [f"Dear {who} and family,", "", "An eye appointment has been confirmed.", "", f"When: {when_text(when)}", f"Where: {loc}", f"Eye doctor: {doctor}"]
        if result_label:
            lines += ["", f"Eye result: {result_label}.", advice]
        lines += ["", f"See the full record by signing in at {url}", f"Questions? Call {app.config['CLINIC_PHONE']}.", "", "This message is not a diagnosis."]
        return f"AfriEye: eye appointment on {when_text(when)}", "\n".join(lines)

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
        out = []
        for t in sms_targets(p):
            u = db().execute("SELECT lang FROM users WHERE phone=?", (t,)).fetchone(); lang = "sw" if u and u["lang"] == "sw" else "en"
            text = compose(kind, p["name"], appt["when_iso"], appt["location"], grade, app.config["PUBLIC_URL"], app.config["CLINIC_PHONE"], app.config["SMS_DETAIL"], lang)
            out.append(send_sms(t, text, kind, p["id"], appt["id"]))
        return out

    def ensure_user(phone, name, email=None):
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if email and db().execute("SELECT 1 FROM users WHERE email=? AND phone<>?", (email, phone)).fetchone():
            abort(409, f"{email} already belongs to another account.")
        if u:
            if email and u["role"] == "member" and u["email"] != email:
                db().execute("UPDATE users SET email=? WHERE id=?", (email, u["id"]))
            return db().execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone(), False
        cur = db().execute("INSERT INTO users(phone,name,email,created) VALUES(?,?,?,?)", (phone, name, email, time.time()))
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
        b = body(); role = {"chw": "chw", "clinician": "clinician", "patient": "member", "county": "county"}.get(b.get("role"))
        if not role:
            abort(400, "Choose whether you are a CHW, a clinician or a patient.")
        county = b.get("county") if role == "county" else None
        if role == "county" and county not in COUNTIES:
            abort(400, "Choose your county.")
        name = clean(b.get("name"), "full name"); phone = normalise_phone(b.get("phone")); area = b.get("area"); hospital = b.get("hospital")
        if db().execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone():
            abort(409, "This number already has an account. Sign in, or use an SMS code.")
        if role == "county":
            area = next(a_["id"] for a_ in AREA.values() if a_["county"] == county)
        if role != "clinician" and area not in AREA:
            abort(400, "Choose your area.")
        if role == "clinician" and hospital not in HOSPITAL:
            abort(400, "Choose your hospital.")
        staff = clean(b.get("staff_id"), "CHW ID number" if role == "chw" else "staff number" if role == "county" else "licence number", 40) if role != "member" else None
        # Clinicians are always checked by their county's health office, and county officers by the system administrator,
        # so nobody can pose as an eye specialist. CHWs are approved automatically only in the demo.
        approved = 1 if role == "member" or (role == "chw" and app.config["AUTO_APPROVE"]) else 0
        email = clean_email(b.get("email"))
        if role == "clinician" and not email:
            abort(400, "Enter your email, so that you're told about new referrals.")
        if email and db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            abort(409, "Another account already uses this email.")
        uid = db().execute("INSERT INTO users(phone,name,role,area,staff_id,hospital_id,approved,email,created) VALUES(?,?,?,?,?,?,?,?,?)", (phone, name, role, area if role != "clinician" else None, staff, hospital if role == "clinician" else None, approved, email, time.time())).lastrowid
        if role == "county":
            db().execute("UPDATE users SET county=?, area=NULL WHERE id=?", (county, uid))
        if role == "member":
            by = birth_year(b.get("birth_year"))
            pid = db().execute("INSERT INTO patients(name,birth_year,phone,village,area_id,sms_consent,created_by,created) VALUES(?,?,?,?,?,?,?,?)", (name, by, phone, AREA[area]["name"], area, 1 if b.get("sms_consent") else 0, uid, time.time())).lastrowid
            db().execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,?,?,?,?)", (pid, uid, "owner", "Self", uid, time.time()))
        if role == "clinician":
            hc = HOSPITAL[hospital]["county"]
            for o in db().execute("SELECT id,phone,email FROM users WHERE role='county' AND approved=1 AND county=?", (hc,)).fetchall():
                notify_user(o["id"], "Clinician waiting for verification", f"{name} registered as an eye clinician at {HOSPITAL[hospital]['name']}, licence {staff}. Please check and approve or reject.")
                send_sms(o["phone"], f"AfriEye: {name} registered as a clinician at {HOSPITAL[hospital]['name']}. Open AfriEye to verify the licence.", "verify_request")
                if o["email"]:
                    send_email(o["email"], "AfriEye: a clinician is waiting for verification", f"{name} registered as an eye clinician at {HOSPITAL[hospital]['name']}.\nLicence number: {staff}\nPhone: {phone}\nEmail: {email}\n\nCheck the licence on the KMPDC or Clinical Officers Council register, then approve or reject at {app.config['PUBLIC_URL']}", "verify_request")
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
                return jsonify(ok=True, message="Your code is shown below.", test_code=code)
        return jsonify(ok=True, message="If that number has an account, we sent a code by SMS.")

    @app.post("/api/me/email")
    @login_required
    def my_email():
        demo_locked()
        e = clean_email(body().get("email"))
        if e and db().execute("SELECT 1 FROM users WHERE email=? AND id<>?", (e, session["uid"])).fetchone():
            abort(409, "Another account already uses this email.")
        db().execute("UPDATE users SET email=? WHERE id=?", (e, session["uid"])); audit("email", None, "set" if e else "removed"); db().commit()
        return jsonify(ok=True, email=e)

    @app.post("/api/auth/logout")
    def logout():
        session.clear(); return jsonify(ok=True)

    def me_json():
        u = me()
        rows = db().execute("SELECT p.id,p.name,l.access,l.relation FROM links l JOIN patients p ON p.id=l.patient_id WHERE l.user_id=? AND l.active=1 ORDER BY l.access='owner' DESC,p.name", (u["id"],)).fetchall()
        a, h = AREA.get(u["area"]), HOSPITAL.get(u["hospital_id"])
        return jsonify(signed_in=True, id=u["id"], name=u["name"], role=u["role"], phone=u["phone"], email=u["email"], lang=u["lang"] or "en", csrf=session.get("csrf"), patients=[dict(r) for r in rows], sms_mode=app.config["SMS"].name, approved=bool(u["approved"]), staff_id=u["staff_id"], area=a["name"] if a else None, area_id=u["area"], county=u["county"] or (a["county"] if a else (h["county"] if h else None)), rejected=u["rejected"], trained=stamp(u["trained"]) if u["trained"] else None, hospital=h["name"] if h else None, hospital_id=u["hospital_id"])

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

    def health_history(h):
        """Other conditions the CHW records, so the clinician sees them with the photos."""
        if not h:
            abort(400, "Record the patient's health history before saving.")
        items = [x for x in (h.get("items") or []) if x in CONDITIONS]
        dt = h.get("diabetes_type")
        if dt not in DIABETES_TYPES:
            abort(400, "Choose the type of diabetes, or Not sure.")
        years = h.get("years")
        if years in (None, ""):
            years = None
        else:
            try:
                years = int(years); assert 0 <= years <= 80
            except (TypeError, ValueError, AssertionError):
                abort(400, "Enter years with diabetes as a number.")
        return json.dumps(dict(items=items, diabetes_type=dt, years=years, insulin=bool(h.get("insulin")), notes=clean(h.get("notes"), "health notes", 300, False)))

    def cover_json(p):
        """The patient's health cover, and whether a CHW is helping them apply for SHA."""
        if not p["cover"] and not p["cover_help"]:
            return None
        helper = db().execute("SELECT name,phone FROM users WHERE id=?", (p["cover_chw"],)).fetchone() if p["cover_chw"] else None
        return dict(cover=p["cover"], label=COVER.get(p["cover"], ""), ref=p["cover_ref"], help=p["cover_help"], helper=dict(helper) if helper else None)

    def set_cover(pid, cover, ref=None, want_help=False, chw_id=None):
        """Saves the cover. With no cover and a request for help, the area's CHWs are asked to help them apply for SHA."""
        if cover not in COVER:
            abort(400, "Choose how the patient's eye care is paid for.")
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone(); helpv, helper = p["cover_help"], p["cover_chw"]
        if cover in ("none", "unsure") and want_help:
            if chw_id:  # the CHW recording it is helping them
                helpv, helper = "helping", chw_id
            elif helpv not in ("requested", "helping"):
                helpv = "requested"
                for c in db().execute("SELECT id,phone FROM users WHERE role='chw' AND approved=1 AND area=?", (p["area_id"],)).fetchall():
                    notify_user(c["id"], "Help with SHA requested", f"{p['name']} in {p['village'] or 'your area'} has no health cover and asked for help to apply for SHA.")
                    send_sms(c["phone"], f"AfriEye: {p['name']} in {p['village'] or 'your area'} asked for help to apply for SHA. Open AfriEye to help.", "sha_help", pid)
        elif cover in ("none", "unsure"):
            helpv, helper = (None, None) if helpv != "registered" else (helpv, helper)
        elif helpv in ("requested", "helping"):
            helpv = "registered" if cover == "sha" else None
        db().execute("UPDATE patients SET cover=?,cover_ref=?,cover_help=?,cover_chw=?,cover_updated=? WHERE id=?", (cover, clean(ref, "cover number", 40, False) or None, helpv, helper, time.time(), pid))

    def saved_patient(b, chw_id):
        info = b.get("patient") or {}
        name = clean(info.get("name"), "patient's name"); phone = normalise_phone(info.get("phone")); area = info.get("area")
        if area not in AREA:
            abort(400, "Choose the patient's area.")
        village = AREA[area]["name"]; by = birth_year(info.get("birth_year"))
        consent = 1 if info.get("sms_consent") else 0; email = clean_email(info.get("email")); hist = health_history(b.get("history"))
        hv = b.get("history") or {}; cov = hv.get("cover")
        row = db().execute("SELECT * FROM patients WHERE phone=? AND name=?", (phone, name)).fetchone()
        if row:
            if email:
                ensure_user(phone, name, email)
            if hist:
                db().execute("UPDATE patients SET conditions=? WHERE id=?", (hist, row["id"]))
            db().execute("UPDATE patients SET sms_consent=?, village=?, area_id=?, birth_year=COALESCE(?,birth_year) WHERE id=?", (consent, village, area, by, row["id"]))
            if cov:
                set_cover(row["id"], cov, hv.get("cover_ref"), bool(hv.get("cover_help")), chw_id)
            add_vitals(row["id"], hv, me())
            return row["id"]
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if u and u["role"] != "member":
            abort(400, "Use the patient's own phone number.")
        u, _ = ensure_user(phone, name, email)
        pid = db().execute("INSERT INTO patients(name,birth_year,phone,village,area_id,sms_consent,created_by,created,conditions) VALUES(?,?,?,?,?,?,?,?,?)", (name, by, phone, village, area, consent, chw_id, time.time(), hist)).lastrowid
        db().execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,?,?,?,?)", (pid, u["id"], "owner", "Self", chw_id, time.time()))
        if cov:
            set_cover(pid, cov, hv.get("cover_ref"), bool(hv.get("cover_help")), chw_id)
        add_vitals(pid, hv, me())
        return pid

    def link_relative(pid, b, added_by):
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        phone = normalise_phone(b.get("phone")); name = clean(b.get("name"), "relative's name"); relation = clean(b.get("relation"), "relationship", 40)
        if phone == p["phone"]:
            abort(400, "That number already belongs to the patient.")
        old = db().execute("SELECT role FROM users WHERE phone=?", (phone,)).fetchone()
        if old and old["role"] != "member":
            abort(400, "Use a family member's own phone number.")
        u, is_new = ensure_user(phone, name, clean_email(b.get("email")))
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
            out["patient"] = dict(id=p["id"], name=p["name"], birth_year=p["birth_year"], village=p["village"], sms_consent=bool(p["sms_consent"]), has_face=has_face, conditions=json.loads(p["conditions"]) if p["conditions"] else None, cover=cover_json(p), vitals=vitals_json(p["id"], 3), treatments=treatments_json(p["id"]))
            if s["status"] == "waiting_review":
                days = (time.time() - s["created"]) / 86400
                out["deadline"] = dict(day=int(days) + 1, days_left=max(0, REFERRAL_DAYS - int(days)), due=stamp(s["created"] + REFERRAL_DAYS * 86400), alerted=bool(s["alerted"]), escalated=bool(s["escalated"]))
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
        if referred:
            notify_user(referred, "New referral" if outcome != "urgent" else "URGENT referral", f"From {me()['name']}. Please review within {REFERRAL_DAYS} days.")
            db().commit()
        if referred:  # the screening is saved first, so a failed email can't lose it
            pt = db().execute("SELECT name,village FROM patients WHERE id=?", (pid,)).fetchone(); chw = me()
            label = "URGENT referral" if outcome == "urgent" else "New referral"
            email_clinicians([referred], f"AfriEye: {label} waiting for your review", "\n".join([
                f"{label} from {chw['name']}, community health worker ({chw['phone']}).", "", f"Patient: {pt['name']}, {pt['village'] or 'area not given'}", f"AI first result: {AI_TEXT[outcome]}", "",
                f"Please review the photos and confirm the appointment at {app.config['PUBLIC_URL']}", "", "The AI result is not a diagnosis."]), "referral", pid)
            db().commit()
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
        return jsonify(periods=out, sample_count=sum(r["is_sample"] for r in rows), ratings=ratings_for(session["uid"]))

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
                       ratings=ratings_for(session["uid"]), overdue=sum(r["status"] == "waiting_review" and time.time() - r["created"] >= ALERT_DAYS * 86400 for r in rows), results=counted((levels[r["level"]] for r in decided), ["No DR", "Early changes", "Clinic visit", "Urgent"]), areas=counted(r["village"] or "Area not given" for r in month), sample_count=sum(r["is_sample"] for r in rows))

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
        subj, etext = appointment_email(p, when, loc, me()["name"], None, None); mails = email_family(p, subj, etext, "booked", appointment_email(p, when, loc, me()["name"], None, None, "sw"))
        notify_patient_people(p["id"], "Consultation confirmed", f"{when_text(when)}, {loc}, with {me()['name']}."); db().commit()
        return jsonify(ok=True, sms=sent, emails=mails, consent=bool(p["sms_consent"]))

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
        lab = LEVEL_TEXT[GRADES[w][0]]; doc = me()["name"]
        notify_patient_people(p["id"], "Appointment confirmed", f"{when_text(when)}, {loc}, with {doc}.")
        subj, text = appointment_email(p, when, loc, doc, lab[0], lab[1]); mails = email_family(p, subj, text, "booked", appointment_email(p, when, loc, doc, GRADES_SW[w], None, "sw"))
        mails += email_clinicians([session["uid"]], f"AfriEye: you confirmed {p['name']}'s appointment", f"Confirmed: {p['name']}, {when_text(when)}, {loc}.\nYour grade: {lab[0]}.\nPatient and chosen family were notified where they agreed to messages.", "confirm_copy", p["id"])
        db().commit()
        return jsonify(screening=screening_json(db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()), sms=sent, emails=mails, invites=[dict(to=i["to"], status=i["status"], ok=i["ok"]) for i in invites], consent=bool(p["sms_consent"]))

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

    @app.get("/api/clinic/emails")
    @clinician_only
    def email_log():
        rows = db().execute("SELECT e.id,e.to_email,e.subject,e.kind,e.status,e.error,e.created,p.name FROM emails e LEFT JOIN patients p ON p.id=e.patient_id ORDER BY e.id DESC LIMIT 50").fetchall()
        return jsonify(mode=app.config["EMAIL"].name, rows=[dict(r, when=stamp(r["created"])) for r in rows])

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
        return dict(id=a["id"], when=a["when_iso"], when_text=when_text(a["when_iso"]), location=a["location"], doctor=d["name"] if d else "", upcoming=a["when_iso"] >= datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"), attended=a["attended"])

    def patient_view(pid, include_family):
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        results = [result_json(r) for r in db().execute("SELECT * FROM results WHERE patient_id=? ORDER BY created DESC,id DESC", (pid,))]
        appts = [appt_json(a) for a in db().execute("SELECT * FROM appointments WHERE patient_id=? ORDER BY when_iso DESC", (pid,))]
        mails = [dict(subject=m["subject"], to=m["to_email"], status=m["status"], when=stamp(m["created"])) for m in db().execute("SELECT * FROM emails WHERE patient_id=? AND kind='booked' ORDER BY id DESC LIMIT 10", (pid,))]
        msgs = [dict(body=m["body"], status=m["status"], when=stamp(m["created"])) for m in db().execute("SELECT * FROM sms WHERE patient_id=? AND to_phone=? AND kind NOT IN ('invite','code') ORDER BY created DESC,id DESC LIMIT 10", (pid, p["phone"]))]
        scans = [dict(date=stamp(s["screened_at"]), text=AI_TEXT[s["outcome"]], status=STATUS_TEXT[s["status"]]) for s in db().execute("SELECT * FROM screenings WHERE patient_id=? ORDER BY screened_at DESC", (pid,))]
        up = sorted((a for a in appts if a["upcoming"]), key=lambda a: a["when"])
        last = db().execute("SELECT chw_id,hospital_id FROM screenings WHERE patient_id=? ORDER BY screened_at DESC LIMIT 1", (pid,)).fetchone()
        chw = db().execute("SELECT name,phone FROM users WHERE id=?", (last["chw_id"],)).fetchone() if last else None
        cons = [dict(id=c["id"], hospital=HOSPITAL[c["hospital_id"]]["name"], preferred_date=c["preferred_date"], reason=c["reason"], status=c["status"], note=c["note"], requested=stamp(c["created"])) for c in db().execute("SELECT * FROM consultations WHERE patient_id=? ORDER BY created DESC LIMIT 10", (pid,))]
        has_face = db().execute("SELECT 1 FROM patient_faces WHERE patient_id=?", (pid,)).fetchone() is not None
        uid = session.get("uid"); srv = served_by(pid); mine = {r["target_id"]: r for r in db().execute("SELECT * FROM reviews WHERE patient_id=?", (pid,))}
        people = [dict(id=i, name=n["name"], role=srv[i], stars=mine[i]["stars"] if i in mine else None, comment=mine[i]["comment"] if i in mine else "") for i in srv for n in [db().execute("SELECT name FROM users WHERE id=?", (i,)).fetchone()] if n]
        reports = [dict(id=r["id"], title=r["title"], by=r["by"], when=stamp(r["created"])) for r in db().execute("SELECT r.id,r.title,r.created,u.name by_name,u.name || CASE r.author_role WHEN 'chw' THEN ', health worker' ELSE ', eye specialist' END by FROM reports r JOIN users u ON u.id=r.created_by WHERE r.patient_id=? AND COALESCE(r.status,'sent')<>'to_clinician' ORDER BY r.id DESC", (pid,))]
        rem = {}
        for r in db().execute("SELECT * FROM reminders WHERE user_id=? AND sent IS NULL", (uid,)):
            rem.setdefault(r["appointment_id"], []).append(dict(id=r["id"], at=stamp(r["remind_at"]), channels=json.loads(r["channels"])))
        visit = db().execute("SELECT v.status,u.name,u.phone FROM visit_requests v LEFT JOIN users u ON u.id=v.chw_id WHERE v.patient_id=? AND COALESCE(v.kind,'home')='home' AND v.status IN ('open','accepted') ORDER BY v.id DESC LIMIT 1", (pid,)).fetchone()
        vvisit = db().execute("SELECT v.status,v.people,v.place,v.preferred,u.name,u.phone FROM visit_requests v LEFT JOIN users u ON u.id=v.chw_id WHERE v.patient_id=? AND v.kind='village' AND v.status IN ('open','accepted') ORDER BY v.id DESC LIMIT 1", (pid,)).fetchone()
        out = dict(people=people, reports=reports, reminders=rem, visit=dict(visit) if visit else None, village_visit=dict(vvisit) if vvisit else None, emails=mails, cover=cover_json(p), patient=dict(conditions=json.loads(p["conditions"]) if p["conditions"] else None, id=p["id"], name=p["name"], birth_year=p["birth_year"], sms_consent=bool(p["sms_consent"]), phone=p["phone"], village=p["village"], area_id=p["area_id"], county=AREA[p["area_id"]]["county"] if p["area_id"] in AREA else None, has_face=has_face), chw=dict(name=chw["name"], phone=chw["phone"]) if chw else None, doctor=results[0]["clinician"] if results else (up[0]["doctor"] if up else None), condition=(GRADES[results[0]["worst"]][1] if results else None), consultations=cons, status=results[0] if results else None, next_appointment=up[0] if up else None, appointments=appts, results=results, messages=msgs, screenings=scans)
        wk = int(time.time() // (7 * 86400)); out.update(vitals=vitals_json(pid), treatments=treatments_json(pid), recall=recall_info(pid), tip=tip_for_week(wk), tips_on=bool(p["tips"]))
        if include_family:
            out["family"] = [dict(r) for r in db().execute("SELECT l.id,u.name,u.phone,u.email,l.relation,l.access,l.receive_sms,u.pw IS NOT NULL joined FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 ORDER BY l.access='owner' DESC,u.name", (pid,))]
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
        pt = db().execute("SELECT name,village FROM patients WHERE id=?", (pid,)).fetchone(); ids = [r["id"] for r in db().execute("SELECT id FROM users WHERE role='clinician' AND approved=1 AND hospital_id=?", (h,))]
        for i in ids:
            notify_user(i, "New consultation request", f"{pt['name']} asked for a consultation.")
        email_clinicians(ids, "AfriEye: new consultation request", f"{pt['name']} ({pt['village'] or 'area not given'}) asked for a consultation at {HOSPITAL[h]['name']}.\nPreferred date: {d}\nReason: {reason}\n\nReply at {app.config['PUBLIC_URL']}", "consult_request", pid); db().commit()
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

    # ======== Notifications, deadlines and reminders ========
    REFERRAL_DAYS, ALERT_DAYS = 7, 3

    def notify_user(uid, title, text, link=None):
        db().execute("INSERT INTO notifications(user_id,title,body,link,created) VALUES(?,?,?,?,?)", (uid, title, text, link, time.time()))

    def notify_patient_people(pid, title, text):
        for r in db().execute("SELECT user_id FROM links WHERE patient_id=? AND active=1", (pid,)):
            notify_user(r["user_id"], title, text)

    def user_lang(uid):
        r = db().execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
        return (r["lang"] if r and r["lang"] else "en")

    def run_due_jobs(now=None):
        """Sends deadline alerts and reminders that are due. Called every few minutes, and safe to call often."""
        now = now or time.time(); done = dict(alerts=0, escalations=0, reminders=0)
        waiting = db().execute("SELECT * FROM screenings WHERE status='waiting_review' AND referred_to IS NOT NULL").fetchall()
        for s in waiting:
            age_days = (now - s["created"]) / 86400
            doc = db().execute("SELECT * FROM users WHERE id=?", (s["referred_to"],)).fetchone(); p = db().execute("SELECT * FROM patients WHERE id=?", (s["patient_id"],)).fetchone()
            if not doc or not p:
                continue
            due = stamp(s["created"] + REFERRAL_DAYS * 86400)
            if age_days >= ALERT_DAYS and not s["alerted"]:
                msg = f"The referral for {p['name']} has waited {int(age_days)} days. Please review it before {due}, or it will be reported to the hospital."
                notify_user(doc["id"], "Referral waiting for your review", msg)
                send_sms(doc["phone"], f"AfriEye: {msg}"[:300], "deadline_alert", p["id"])
                if doc["email"]:
                    send_email(doc["email"], f"AfriEye: referral for {p['name']} waiting {int(age_days)} days", f"{msg}\n\nReview it at {app.config['PUBLIC_URL']}", "deadline_alert", p["id"])
                db().execute("UPDATE screenings SET alerted=? WHERE id=?", (now, s["id"])); done["alerts"] += 1
            if age_days >= REFERRAL_DAYS and not s["escalated"]:
                h = HOSPITAL.get(s["hospital_id"])
                if h:
                    send_email(h["email"], f"AfriEye: no action on a referral for {REFERRAL_DAYS} days", "\n".join([
                        f"Dear {h['name']} eye clinic,", "", f"{doc['name']} has not taken any action for a period of {REFERRAL_DAYS} days on a referral sent through AfriEye.", "",
                        f"Patient: {p['name']}, {p['village'] or 'area not given'}", f"Referred on: {stamp(s['created'])}", f"AI first result: {AI_TEXT[s['outcome']]}", "",
                        f"Please arrange for another clinician to review it at {app.config['PUBLIC_URL']}"]), "escalation", p["id"])
                notify_user(doc["id"], "Referral reported to your hospital", f"The referral for {p['name']} passed {REFERRAL_DAYS} days without action, so your hospital was emailed.")
                db().execute("UPDATE screenings SET escalated=? WHERE id=?", (now, s["id"])); done["escalations"] += 1
        for r in db().execute("SELECT r.*,a.when_iso,a.location,a.patient_id FROM reminders r JOIN appointments a ON a.id=r.appointment_id WHERE r.sent IS NULL AND r.remind_at<=?", (now,)).fetchall():
            u = db().execute("SELECT * FROM users WHERE id=?", (r["user_id"],)).fetchone(); p = db().execute("SELECT name FROM patients WHERE id=?", (r["patient_id"],)).fetchone()
            if u and p:
                lang = u["lang"] or "en"; ch = json.loads(r["channels"]); when = when_text(r["when_iso"], lang)
                text = (f"AfriEye: Kumbusho. Miadi ya macho ya {p['name']}: {when}, {r['location']}." if lang == "sw" else f"AfriEye reminder: eye appointment for {p['name']}: {when}, {r['location']}.")
                if "app" in ch:
                    notify_user(u["id"], "Kumbusho la miadi" if lang == "sw" else "Appointment reminder", text)
                if "sms" in ch:
                    send_sms(u["phone"], text, "reminder", r["patient_id"], r["appointment_id"])
                if "email" in ch and u["email"]:
                    send_email(u["email"], "AfriEye: appointment reminder", text, "reminder", r["patient_id"])
            db().execute("UPDATE reminders SET sent=? WHERE id=?", (now, r["id"])); done["reminders"] += 1
        done.update(recalls=0, tips=0)
        for p in db().execute("SELECT * FROM patients").fetchall():
            r = recall_info(p["id"])
            if r and r["due"] <= now and (p["recall_notified"] or 0) < r["due"]:
                chw = last_chw(p["id"])
                if chw:
                    notify_user(chw, "Eye check due", f"{p['name']} is due for an eye check ({'6-month' if r['reason'] == 'early' else 'yearly'}). Please arrange a screening.")
                if p["sms_consent"]:
                    send_sms(p["phone"], f"AfriEye: {p['name'].split()[0]}, it's time for your {'6-month' if r['reason'] == 'early' else 'yearly'} eye check. Your health worker will contact you, or dial the AfriEye USSD code.", "recall", p["id"])
                notify_patient_people(p["id"], "Eye check due", "It's time for your next eye check. Your health worker will contact you.")
                db().execute("UPDATE patients SET recall_notified=? WHERE id=?", (now, p["id"])); done["recalls"] += 1
            if p["tips"] and p["sms_consent"] and now - (p["tip_sent"] or 0) >= 7 * 86400 - 3600:
                u = db().execute("SELECT u.lang FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.access='owner'", (p["id"],)).fetchone()
                tip = tip_for_week(int(now // (7 * 86400)))
                send_sms(p["phone"], "AfriEye: " + (tip["sw"] if u and u["lang"] == "sw" else tip["en"]), "tip", p["id"])
                db().execute("UPDATE patients SET tip_sent=? WHERE id=?", (now, p["id"])); done["tips"] += 1
        db().commit()
        return done
    app.run_due_jobs = run_due_jobs
    app.last_jobs = 0.0

    @app.before_request
    def jobs_now_and_then():
        if time.time() - app.last_jobs > 60:  # also covered by the background loop in run.py
            app.last_jobs = time.time()
            try:
                run_due_jobs()
            except Exception as e:  # a failed alert must never break a page
                app.logger.warning("Due jobs failed: %s", e)

    @app.get("/api/notifications")
    @login_required
    def notifications():
        rows = db().execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (session["uid"],)).fetchall()
        return jsonify(unread=sum(1 for r in rows if not r["read"]), items=[dict(id=r["id"], title=r["title"], body=r["body"], when=stamp(r["created"]), read=bool(r["read"])) for r in rows])

    @app.post("/api/notifications/read")
    @login_required
    def notifications_read():
        db().execute("UPDATE notifications SET read=1 WHERE user_id=?", (session["uid"],)); db().commit(); return jsonify(ok=True)

    def can_see_patient(pid):
        return bool(link_for(pid)) or may_view_face(pid)

    @app.post("/api/appointments/<int:aid>/reminders")
    @login_required
    def reminder_add(aid):
        a = db().execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
        if not a or not link_for(a["patient_id"]):
            abort(404, "Appointment not found.")
        b = body(); ch = [c for c in (b.get("channels") or []) if c in ("app", "sms", "email")]
        if not ch:
            abort(400, "Choose how you want to be reminded.")
        if "email" in ch and not me()["email"]:
            abort(400, "Add your email in Settings first, or choose another way.")
        appt = datetime.fromisoformat(a["when_iso"]).replace(tzinfo=TZ); kind = b.get("when")
        if kind == "day_before":
            at = appt - timedelta(days=1)
        elif kind == "morning":
            at = appt.replace(hour=7, minute=0)
        elif kind == "two_hours":
            at = appt - timedelta(hours=2)
        else:
            try:
                at = datetime.fromisoformat(str(b.get("at"))).replace(tzinfo=TZ)
            except ValueError:
                abort(400, "Choose when to be reminded.")
        if at.timestamp() < time.time() - 60:
            abort(400, "That reminder time has already passed. Choose a later one.")
        if at > appt:
            abort(400, "Choose a time before the appointment.")
        db().execute("INSERT INTO reminders(appointment_id,user_id,remind_at,channels,created) VALUES(?,?,?,?,?)", (aid, session["uid"], at.timestamp(), json.dumps(ch), time.time())); db().commit()
        return jsonify(ok=True, at=stamp(at.timestamp())), 201

    @app.delete("/api/reminders/<int:rid>")
    @login_required
    def reminder_del(rid):
        if not db().execute("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, session["uid"])).rowcount:
            abort(404, "Reminder not found.")
        db().commit(); return jsonify(ok=True)

    # ======== Reviews ========
    def served_by(pid):
        """The CHWs and clinicians who looked after this patient."""
        ids = {}
        for r in db().execute("SELECT chw_id u,'chw' role FROM screenings WHERE patient_id=? UNION SELECT clinician_id,'clinician' FROM screenings WHERE patient_id=? AND clinician_id IS NOT NULL UNION SELECT clinician_id,'clinician' FROM appointments WHERE patient_id=?", (pid, pid, pid)):
            ids[r["u"]] = r["role"]
        return ids

    def ratings_for(uid):
        r = db().execute("SELECT AVG(stars) a, COUNT(*) n FROM reviews WHERE target_id=?", (uid,)).fetchone()
        recent = [dict(stars=x["stars"], comment=x["comment"], when=stamp(x["created"])) for x in db().execute("SELECT * FROM reviews WHERE target_id=? AND comment<>'' ORDER BY id DESC LIMIT 3", (uid,))]
        return dict(average=round(r["a"], 1) if r["a"] else None, count=r["n"], recent=recent)

    @app.post("/api/patients/<int:pid>/reviews")
    @login_required
    def review(pid):
        owner_required(pid); b = body(); target = b.get("target_id")
        if served_by(pid).get(target) is None:
            abort(400, "You can only review a health worker or clinician who looked after you.")
        try:
            stars = int(b.get("stars")); assert 1 <= stars <= 5
        except (TypeError, ValueError, AssertionError):
            abort(400, "Choose from 1 to 5 stars.")
        comment = clean(b.get("comment"), "comment", 400, False)
        db().execute("INSERT INTO reviews(patient_id,author_id,target_id,target_role,stars,comment,created) VALUES(?,?,?,?,?,?,?) ON CONFLICT(patient_id,target_id) DO UPDATE SET stars=excluded.stars,comment=excluded.comment,created=excluded.created",
                     (pid, session["uid"], target, served_by(pid)[target], stars, comment, time.time()))
        notify_user(target, "New review", f"A patient gave you {stars} star{'s' if stars > 1 else ''}.")
        audit("review", pid, str(target)); db().commit(); return jsonify(ok=True)

    @app.get("/api/me/ratings")
    @login_required
    def my_ratings():
        return jsonify(ratings_for(session["uid"]))

    # ======== Home visits ========
    def request_visit(pid, source, note="", kind="home", people=None, place=None, preferred=None):
        """A home visit for the patient, or a village visit so the CHW can check a group of people with diabetes."""
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if db().execute("SELECT 1 FROM visit_requests WHERE patient_id=? AND COALESCE(kind,'home')=? AND status IN ('open','accepted')", (pid, kind)).fetchone():
            abort(409, "A village visit is already requested." if kind == "village" else "A health worker visit is already requested.")
        place = place or p["village"] or (AREA[p["area_id"]]["name"] if p["area_id"] in AREA else "")
        db().execute("INSERT INTO visit_requests(patient_id,area_id,note,status,source,created,kind,people,place,preferred) VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, p["area_id"], note, "open", source, time.time(), kind, people, place, preferred))
        for c in db().execute("SELECT id,phone FROM users WHERE role='chw' AND approved=1 AND area=?", (p["area_id"],)).fetchall():
            if kind == "village":
                notify_user(c["id"], "Village visit requested", f"{p['name']} asked you to visit {place} to check about {people or 'several'} people.")
                send_sms(c["phone"], f"AfriEye: {p['name']} asked for a village eye check in {place}, for about {people or 'several'} people{', around ' + preferred if preferred else ''}. Open AfriEye to accept.", "visit_request", pid)
            else:
                notify_user(c["id"], "Home visit requested", f"{p['name']} in {p['village'] or 'your area'} asked for a visit.")
                send_sms(c["phone"], f"AfriEye: {p['name']} in {p['village'] or 'your area'} asked for a home eye check. Open AfriEye to accept.", "visit_request", pid)

    @app.post("/api/patients/<int:pid>/visit")
    @login_required
    def visit_ask(pid):
        owner_required(pid); b = body(); kind = "village" if b.get("kind") == "village" else "home"
        people = place = preferred = None
        if kind == "village":
            try:
                people = int(b.get("people") or 0)
            except (TypeError, ValueError):
                people = 0
            if not 2 <= people <= 200:
                abort(400, "Enter how many people need an eye check, from 2 to 200.")
            place = clean(b.get("place"), "place", 80, False) or None
            preferred = clean(b.get("date"), "date", 10, False) or None
        request_visit(pid, "app", clean(b.get("note"), "note", 200, False), kind, people, place, preferred); db().commit(); return jsonify(ok=True), 201

    # ======== Health cover (SHA, NGO, insurance) ========
    @app.post("/api/patients/<int:pid>/cover")
    @login_required
    def cover_save(pid):
        u = me(); b = body()
        if u["role"] == "chw":
            if not may_view_face(pid):
                abort(403, "You can only update patients you've looked after.")
            set_cover(pid, b.get("cover"), b.get("ref"), bool(b.get("help")), u["id"])
        else:
            owner_required(pid); set_cover(pid, b.get("cover"), b.get("ref"), bool(b.get("help")))
        audit("cover", pid, str(b.get("cover"))); db().commit(); return jsonify(ok=True)

    @app.get("/api/chw/cover-help")
    @chw_only
    def cover_help_list():
        u = me()
        rows = db().execute("SELECT * FROM patients WHERE (cover_help='requested' AND area_id=?) OR (cover_help='helping' AND cover_chw=?) ORDER BY cover_updated", (u["area"], u["id"])).fetchall()
        return jsonify([dict(id=r["id"], name=r["name"], phone=r["phone"], village=r["village"], birth_year=r["birth_year"], status=r["cover_help"], cover=COVER.get(r["cover"], ""), when=stamp(r["cover_updated"] or time.time())) for r in rows])

    @app.post("/api/chw/cover-help/<int:pid>/<action>")
    @chw_only
    def cover_help_act(pid, action):
        u = me(); p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        if action == "start" and p["cover_help"] == "requested" and p["area_id"] == u["area"]:
            db().execute("UPDATE patients SET cover_help='helping',cover_chw=?,cover_updated=? WHERE id=?", (u["id"], time.time(), pid))
            send_sms(p["phone"], f"AfriEye: {u['name']}, community health worker, will help you apply for SHA. Keep your ID card ready. Phone: {u['phone']}", "sha_help", pid)
            notify_patient_people(pid, "Help with SHA", f"{u['name']} will help you apply for SHA. Phone: {u['phone']}")
        elif action == "done" and p["cover_help"] == "helping" and p["cover_chw"] == u["id"]:
            ref = clean(body().get("ref"), "SHA number", 40, False) or p["cover_ref"]
            db().execute("UPDATE patients SET cover='sha',cover_ref=?,cover_help='registered',cover_updated=? WHERE id=?", (ref, time.time(), pid))
            send_sms(p["phone"], "AfriEye: you are now registered with SHA. Show your ID card at the hospital for your eye care.", "sha_help", pid)
            notify_patient_people(pid, "Registered with SHA", f"{u['name']} helped you register with SHA.")
        else:
            abort(409, "This request can't be changed now.")
        audit("cover_help_" + action, pid, ""); db().commit(); return jsonify(ok=True)

    @app.get("/api/chw/visits")
    @chw_only
    def visits():
        u = me()
        rows = db().execute("SELECT v.*,p.name,p.phone,p.village,p.birth_year FROM visit_requests v JOIN patients p ON p.id=v.patient_id WHERE (v.status='open' AND v.area_id=?) OR (v.status='accepted' AND v.chw_id=?) ORDER BY v.created", (u["area"], u["id"])).fetchall()
        return jsonify([dict(id=r["id"], name=r["name"], phone=r["phone"], village=r["village"], birth_year=r["birth_year"], note=r["note"], status=r["status"], source=r["source"], when=stamp(r["created"]), kind=r["kind"] or "home", people=r["people"], place=r["place"], preferred=r["preferred"]) for r in rows])

    @app.post("/api/chw/visits/<int:vid>/<action>")
    @chw_only
    def visit_act(vid, action):
        u = me(); v = db().execute("SELECT * FROM visit_requests WHERE id=?", (vid,)).fetchone()
        if not v:
            abort(404, "Visit request not found.")
        if action == "accept" and v["status"] == "open" and v["area_id"] == u["area"]:
            db().execute("UPDATE visit_requests SET status='accepted',chw_id=?,updated=? WHERE id=?", (u["id"], time.time(), vid))
            p = db().execute("SELECT * FROM patients WHERE id=?", (v["patient_id"],)).fetchone()
            if v["kind"] == "village":
                send_sms(p["phone"], f"AfriEye: {u['name']}, community health worker, will come to {v['place']} for the village eye check. Phone: {u['phone']}. Please tell the people who need a check.", "visit_accepted", p["id"])
                notify_patient_people(p["id"], "Village visit accepted", f"{u['name']} will come to {v['place']}. Phone: {u['phone']}")
            else:
                send_sms(p["phone"], f"AfriEye: {u['name']}, community health worker, will visit you for an eye check. Phone: {u['phone']}", "visit_accepted", p["id"])
                notify_patient_people(p["id"], "Health worker visit accepted", f"{u['name']} will visit you. Phone: {u['phone']}")
        elif action == "done" and v["status"] == "accepted" and v["chw_id"] == u["id"]:
            db().execute("UPDATE visit_requests SET status='done',updated=? WHERE id=?", (time.time(), vid))
        else:
            abort(409, "This visit request can't be changed now.")
        db().commit(); return jsonify(ok=True)

    # ======== PDF reports ========
    def build_report(pid, author):
        from io import BytesIO
        from reportlab.lib import colors as rc
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Image as RImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        v = patient_view(pid, False); P = v["patient"]; navy = rc.HexColor("#0B1F3F")
        H = ParagraphStyle("h", fontName="Times-Bold", fontSize=18, leading=22, textColor=navy, spaceAfter=4)
        H2 = ParagraphStyle("h2", fontName="Times-Bold", fontSize=13, leading=16, textColor=navy, spaceBefore=10, spaceAfter=4)
        B = ParagraphStyle("b", fontName="Times-Roman", fontSize=11, leading=14)
        S = ParagraphStyle("s", parent=B, fontSize=9, textColor=rc.HexColor("#4C545E"))
        e = lambda t: str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;")
        def grid(rows, widths):
            t = Table(rows, colWidths=widths)
            t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Times-Roman", 10), ("FONT", (0, 0), (0, -1), "Times-Bold", 10), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), .4, rc.HexColor("#CDD4DE")), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
            return t
        logo = HERE / "static" / "logo.png"
        top = [RImage(str(logo), 26 * mm, 31.8 * mm)] if logo.exists() else []
        flow = top + [Spacer(1, 4), Paragraph("AfriEye eye care report", H), Paragraph("<i>Kuja Utuone</i>", S), Paragraph(f"Prepared {e(stamp(time.time()))} by {e(author['name'])}, {'community health worker' if author['role'] == 'chw' else 'eye specialist'}.", S), Spacer(1, 6)]
        flow += [Paragraph("Patient", H2), grid([[k, Paragraph(e(x), B)] for k, x in [("Name", P["name"]), ("Age", f"{datetime.now(TZ).year - P['birth_year']} years" if P["birth_year"] else "Not given"), ("Phone", P["phone"]), ("Area", f"{P['village'] or ''}{', ' + P['county'] if P.get('county') else ''}"), ("Community health worker", f"{v['chw']['name']}, {v['chw']['phone']}" if v["chw"] else "Not assigned"), ("Eye doctor", v["doctor"] or "Not assigned")]], [45 * mm, 125 * mm])]
        c = P.get("conditions") or {}
        hist = [("Diabetes", ", ".join(x for x in [c.get("diabetes_type"), f"{c['years']} years" if c.get("years") else None, "on insulin" if c.get("insulin") else None] if x) or "Not recorded"), ("Other conditions", ", ".join(c.get("items") or []) or "None recorded")]
        if c.get("notes"):
            hist.append(("Notes", c["notes"]))
        flow += [Paragraph("Health history", H2), grid([[k, Paragraph(e(x), B)] for k, x in hist], [45 * mm, 125 * mm])]
        if v["status"]:
            st = v["status"]
            flow += [Paragraph("Specialist result", H2), grid([[k, Paragraph(e(x), B)] for k, x in [("Date", st["date"]), ("Result", st["label"]), ("Right eye", st["right"] or "Not graded"), ("Left eye", st["left"] or "Not graded"), ("Advice", st["advice"]), ("Note", st["note"] or ""), ("Specialist", st["clinician"])]], [45 * mm, 125 * mm])]
        last = db().execute("SELECT * FROM screenings WHERE patient_id=? ORDER BY screened_at DESC LIMIT 1", (pid,)).fetchone()
        if last:
            flow += [Paragraph("Latest eye screening", H2), Paragraph(e(f"{stamp(last['screened_at'])}. {AI_TEXT[last['outcome']]} Scanned by {last['model'] or 'the AfriEye model'}."), B)]
            pics = []
            for ph in db().execute("SELECT eye,jpeg FROM screening_photos WHERE screening_id=? ORDER BY eye DESC", (last["id"],)):
                try:
                    from PIL import Image as PImage
                    PImage.open(BytesIO(ph["jpeg"])).verify()
                except Exception:
                    continue  # an unreadable photo is left out, rather than stopping the report
                pics.append([RImage(BytesIO(ph["jpeg"]), 55 * mm, 55 * mm), Paragraph(("Right eye" if ph["eye"] == "right" else "Left eye"), S)])
            if pics:
                flow += [Spacer(1, 6), Table([[x[0] for x in pics], [x[1] for x in pics]])]
        if v["appointments"]:
            flow += [Paragraph("Appointments", H2), grid([["When", "Where", "Doctor"]] + [[a["when_text"], Paragraph(e(a["location"]), B), Paragraph(e(a["doctor"]), B)] for a in v["appointments"][:6]], [45 * mm, 80 * mm, 45 * mm])]
        flow += [Spacer(1, 12), Paragraph("The AI gives a first result. It isn't a diagnosis. An eye specialist makes the decision.", S)]
        buf = BytesIO(); SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm, title=f"AfriEye report, {P['name']}", author="AfriEye").build(flow)
        return buf.getvalue()

    def may_report(pid):
        u = me()
        if u["role"] == "chw":
            return db().execute("SELECT 1 FROM screenings WHERE patient_id=? AND chw_id=?", (pid, u["id"])).fetchone() is not None
        return u["role"] == "clinician" and u["approved"] and may_view_face(pid)

    @app.get("/api/patients/<int:pid>/reports/preview")
    @login_required
    def report_preview(pid):
        """The same PDF the patient would get, shown to the CHW or clinician first. Nothing is saved or sent."""
        if not may_report(pid):
            abort(403, "You can only make reports for patients you've looked after.")
        audit("preview_report", pid, ""); db().commit()
        return Response(build_report(pid, me()), mimetype="application/pdf", headers={"Content-Disposition": 'inline; filename="afrieye-report-preview.pdf"', "Cache-Control": "no-store"})

    @app.post("/api/patients/<int:pid>/reports")
    @login_required
    def report_make(pid):
        if not may_report(pid):
            abort(403, "You can only make reports for patients you've looked after.")
        u = me(); name = db().execute("SELECT name FROM patients WHERE id=?", (pid,)).fetchone()["name"]
        title = f"Eye care report, {datetime.now(TZ).strftime('%d %b %Y')}"
        if u["role"] == "chw":
            # A health worker's report goes to the patient's eye specialist first. The specialist checks it and sends it on.
            doc = db().execute("SELECT u.id,u.name FROM screenings s JOIN users u ON u.id=COALESCE(s.clinician_id,s.referred_to) WHERE s.patient_id=? ORDER BY s.id DESC LIMIT 1", (pid,)).fetchone()
            if not doc:
                abort(409, "This patient has no eye specialist yet. Send a referral first, and the report will go to that specialist.")
            rid = db().execute("INSERT INTO reports(patient_id,created_by,author_role,title,pdf,created,status,to_user) VALUES(?,?,?,?,?,?,?,?)", (pid, u["id"], "chw", title, build_report(pid, u), time.time(), "to_clinician", doc["id"])).lastrowid
            notify_user(doc["id"], "Report from a health worker", f"{u['name']} sent a report for {name}. Check it, then send it to the patient.")
            audit("report_to_clinician", pid, str(rid)); db().commit(); return jsonify(ok=True, id=rid, to=doc["name"]), 201
        rid = db().execute("INSERT INTO reports(patient_id,created_by,author_role,title,pdf,created,status) VALUES(?,?,?,?,?,?,?)", (pid, u["id"], u["role"], title, build_report(pid, u), time.time(), "sent")).lastrowid
        notify_patient_people(pid, "New eye care report", f"{u['name']} sent a report for {name}. Open it from your dashboard.")
        audit("report", pid, str(rid)); db().commit(); return jsonify(ok=True, id=rid), 201

    @app.get("/api/clinic/chw-reports")
    @clinician_only
    def chw_reports():
        rows = db().execute("SELECT r.id,r.title,r.created,r.status,r.released,p.id pid,p.name pname,u.name chw FROM reports r JOIN patients p ON p.id=r.patient_id JOIN users u ON u.id=r.created_by WHERE r.to_user=? ORDER BY (r.status='to_clinician') DESC, r.id DESC LIMIT 200", (session["uid"],)).fetchall()
        return jsonify(rows=[dict(id=r["id"], title=r["title"], patient=r["pname"], patient_id=r["pid"], chw=r["chw"], when=stamp(r["created"]), waiting=r["status"] == "to_clinician", released=stamp(r["released"]) if r["released"] else None) for r in rows])

    @app.post("/api/reports/<int:rid>/release")
    @clinician_only
    def report_release(rid):
        r = db().execute("SELECT * FROM reports WHERE id=? AND to_user=?", (rid, session["uid"])).fetchone()
        if not r:
            abort(404, "Report not found.")
        if r["status"] != "to_clinician":
            abort(409, "This report was already sent to the patient.")
        u = me(); name = db().execute("SELECT name FROM patients WHERE id=?", (r["patient_id"],)).fetchone()["name"]
        db().execute("UPDATE reports SET status='released',released=? WHERE id=?", (time.time(), rid))
        notify_patient_people(r["patient_id"], "New eye care report", f"{u['name']} checked and sent a report for {name}. Open it from your dashboard.")
        notify_user(r["created_by"], "Report sent to the patient", f"{u['name']} checked your report for {name} and sent it to the patient.")
        audit("report_release", r["patient_id"], str(rid)); db().commit(); return jsonify(ok=True)

    def report_row(rid):
        r = db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        if not r:
            abort(404, "Report not found.")
        if r["status"] == "to_clinician":
            ok = session.get("uid") in (r["created_by"], r["to_user"])  # not the patient yet: the specialist checks it first
        else:
            ok = link_for(r["patient_id"]) or r["created_by"] == session.get("uid") or (me()["role"] == "clinician" and may_view_face(r["patient_id"]))
        if not ok:
            abort(403, "You do not have access to this report.")
        return r

    @app.get("/api/reports/<int:rid>/pdf")
    @login_required
    def report_pdf(rid):
        r = report_row(rid); audit("view_report", r["patient_id"], str(rid)); db().commit()
        return Response(r["pdf"], mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="afrieye-report-{rid}.pdf"'})

    @app.post("/api/reports/<int:rid>/email")
    @login_required
    def report_email(rid):
        r = report_row(rid)
        if not link_for(r["patient_id"]):
            abort(403, "Only the patient or their family can share this report.")
        to = clean_email(body().get("email"))
        if not to:
            abort(400, "Enter the email address to send it to.")
        res = app.config["EMAIL"].send(to, f"AfriEye: {r['title']}", f"Attached is an AfriEye eye care report, shared by {me()['name']}.\n\nThe AI gives a first result. It isn't a diagnosis.", attachment=(f"afrieye-report-{rid}.pdf", r["pdf"], "application/pdf"))
        db().execute("INSERT INTO emails(patient_id,to_email,subject,body,kind,status,error,created) VALUES(?,?,?,?,?,?,?,?)", (r["patient_id"], to, f"AfriEye: {r['title']}", "(report attached)", "report", res["status"], res["error"], time.time()))
        audit("share_report", r["patient_id"], to); db().commit()
        if not res["ok"]:
            abort(400, f"The email could not be sent: {res['error']}")
        return jsonify(ok=True, status=res["status"])

    # ======== Settings ========
    @app.post("/api/me/password")
    @login_required
    def change_password():
        demo_locked()
        b = body(); u = me()
        if not check_password_hash(u["pw"] or "", str(b.get("current", ""))):
            abort(400, "Your current password is not right.")
        if len(str(b.get("new", ""))) < 8:
            abort(400, "Choose a new password with at least 8 characters.")
        db().execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(str(b["new"])), u["id"])); audit("password"); db().commit(); return jsonify(ok=True)

    @app.post("/api/me/lang")
    @login_required
    def set_lang():
        lang = body().get("lang")
        if lang not in LANGS:
            abort(400, "Choose a language from the list.")
        db().execute("UPDATE users SET lang=? WHERE id=?", (lang, session["uid"])); db().commit(); return jsonify(ok=True)

    @app.get("/api/me/export")
    @login_required
    def export_mine():
        """A copy of this person's own data, for their records."""
        u = me(); out = dict(exported=stamp(time.time()), account=dict(name=u["name"], phone=u["phone"], email=u["email"], role=u["role"], language=u["lang"]))
        if u["role"] == "member":
            out["records"] = [patient_view(l["patient_id"], True) for l in db().execute("SELECT patient_id FROM links WHERE user_id=? AND active=1", (u["id"],))]
        elif u["role"] == "chw":
            out["screenings"] = [screening_json(s) for s in db().execute("SELECT * FROM screenings WHERE chw_id=? ORDER BY created DESC LIMIT 500", (u["id"],))]
        else:
            out["reviewed"] = [screening_json(s) for s in db().execute("SELECT * FROM screenings WHERE clinician_id=? ORDER BY reviewed DESC LIMIT 500", (u["id"],))]
        audit("export"); db().commit()
        return Response(json.dumps(out, indent=1, default=str), mimetype="application/json", headers={"Content-Disposition": 'attachment; filename="afrieye-my-data.json"'})

    @app.post("/api/me/delete")
    @login_required
    def delete_mine():
        demo_locked()
        u = me()
        if not check_password_hash(u["pw"] or "", str(body().get("password", ""))):
            abort(400, "Your password is not right.")
        if u["role"] != "member":
            abort(400, "Staff accounts hold patient records, so ask your administrator to close yours.")
        for l in db().execute("SELECT patient_id FROM links WHERE user_id=? AND access='owner'", (u["id"],)).fetchall():
            pid = l["patient_id"]
            for sid in [r["id"] for r in db().execute("SELECT id FROM screenings WHERE patient_id=?", (pid,))]:
                db().execute("DELETE FROM screening_photos WHERE screening_id=?", (sid,))
            for tbl in ("screenings", "results", "patient_faces", "consultations", "reports", "reviews", "visit_requests", "links", "vitals", "treatments", "followups"):
                db().execute(f"DELETE FROM {tbl} WHERE patient_id=?", (pid,))
            db().execute("DELETE FROM reminders WHERE appointment_id IN (SELECT id FROM appointments WHERE patient_id=?)", (pid,))
            db().execute("DELETE FROM appointments WHERE patient_id=?", (pid,))
            db().execute("UPDATE sms SET patient_id=NULL WHERE patient_id=?", (pid,)); db().execute("UPDATE emails SET patient_id=NULL WHERE patient_id=?", (pid,))
            db().execute("DELETE FROM patients WHERE id=?", (pid,))
        db().execute("DELETE FROM links WHERE user_id=?", (u["id"],)); db().execute("DELETE FROM notifications WHERE user_id=?", (u["id"],)); db().execute("DELETE FROM reminders WHERE user_id=?", (u["id"],))
        db().execute("DELETE FROM users WHERE id=?", (u["id"],)); audit("delete_account"); db().commit(); session.clear()
        return jsonify(ok=True)

    # ======== Follow-up after the referral: attendance, treatment, recall, readings ========
    def last_chw(pid):
        r = db().execute("SELECT chw_id FROM screenings WHERE patient_id=? ORDER BY screened_at DESC LIMIT 1", (pid,)).fetchone()
        return r["chw_id"] if r else None

    def recall_info(pid):
        """When the patient's next eye check is due, from the latest screening or specialist result."""
        s = db().execute("SELECT outcome,status,screened_at FROM screenings WHERE patient_id=? ORDER BY screened_at DESC LIMIT 1", (pid,)).fetchone()
        r = db().execute("SELECT level,created FROM results WHERE patient_id=? ORDER BY created DESC LIMIT 1", (pid,)).fetchone()
        if not s and not r:
            return None
        base_s = s["screened_at"] if s else 0
        if r and r["created"] >= base_s:
            if r["level"] not in RECALL_DAYS:
                return None  # in the eye clinic's care
            days, base, why = RECALL_DAYS[r["level"]], r["created"], "early" if r["level"] == "watch" else "yearly"
        else:
            if s["outcome"] != "rescreen":
                return None  # waiting for, or in, the eye clinic
            days, base, why = RECALL_DAYS["rescreen"], base_s, "yearly"
        due = base + days * 86400
        return dict(due=due, due_text=datetime.fromtimestamp(due, TZ).strftime("%d %b %Y"), overdue=due < time.time(), months=round(days / 30), reason=why)

    def vitals_json(pid, n=12):
        return [dict(id=v["id"], glucose=v["glucose"], glucose_kind=v["glucose_kind"], hba1c=v["hba1c"], sys=v["sys"], dia=v["dia"], by=v["by_role"], when=stamp(v["created"]), date=datetime.fromtimestamp(v["created"], TZ).strftime("%d %b"))
                for v in db().execute("SELECT * FROM vitals WHERE patient_id=? ORDER BY created DESC,id DESC LIMIT ?", (pid, n))]

    def treatments_json(pid):
        out = []
        for t in db().execute("SELECT t.*,u.name doc FROM treatments t LEFT JOIN users u ON u.id=t.clinician_id WHERE t.patient_id=? ORDER BY t.created DESC", (pid,)):
            out.append(dict(id=t["id"], treatment=t["treatment"], label=TREATMENTS.get(t["treatment"], t["treatment"]), eye=t["eye"], va_right=t["va_right"], va_left=t["va_left"], note=t["note"], doctor=t["doc"], when=datetime.fromtimestamp(t["created"], TZ).strftime("%d %b %Y")))
        return out

    def add_vitals(pid, b, u):
        def num(k, lo, hi, label, cast=float):
            v = b.get(k)
            if v in (None, ""):
                return None
            try:
                v = cast(v); assert lo <= v <= hi
            except (TypeError, ValueError, AssertionError):
                abort(400, f"Check the {label}. It should be between {lo} and {hi}.")
            return v
        g = num("glucose", 1, 35, "blood sugar (mmol/L)"); a1c = num("hba1c", 3, 20, "HbA1c (%)"); sy = num("sys", 60, 260, "top blood pressure number", int); di = num("dia", 30, 160, "bottom blood pressure number", int)
        if (sy is None) != (di is None):
            abort(400, "Enter both blood pressure numbers, such as 130 over 80.")
        if g is None and a1c is None and sy is None:
            return None
        kind = b.get("glucose_kind") if b.get("glucose_kind") in ("fasting", "random") else "random"
        return db().execute("INSERT INTO vitals(patient_id,by_user,by_role,glucose,glucose_kind,hba1c,sys,dia,created) VALUES(?,?,?,?,?,?,?,?,?)", (pid, u["id"], u["role"], g, kind if g is not None else None, a1c, sy, di, time.time())).lastrowid

    @app.post("/api/patients/<int:pid>/vitals")
    @login_required
    def vitals_add(pid):
        u = me()
        if u["role"] in ("chw", "clinician"):
            if not may_view_face(pid):
                abort(403, "You can only add readings for patients you've looked after.")
        else:
            owner_required(pid)
        if not add_vitals(pid, body(), u):
            abort(400, "Enter at least one reading.")
        audit("vitals", pid, ""); db().commit(); return jsonify(ok=True, vitals=vitals_json(pid)), 201

    @app.get("/api/clinic/attendance")
    @clinician_only
    def attendance_list():
        u = me(); now = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"); since = (datetime.now(TZ) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M")
        rows = db().execute("SELECT a.*,p.name,p.phone,p.village,p.birth_year FROM appointments a JOIN patients p ON p.id=a.patient_id WHERE (a.clinician_id=? OR a.location=?) AND a.when_iso<? AND a.when_iso>=? ORDER BY a.attended IS NOT NULL, a.when_iso DESC LIMIT 100",
                            (u["id"], HOSPITAL[u["hospital_id"]]["name"], now, since)).fetchall()
        return jsonify([dict(id=a["id"], patient_id=a["patient_id"], name=a["name"], phone=a["phone"], village=a["village"], birth_year=a["birth_year"], when_text=when_text(a["when_iso"]), location=a["location"], attended=a["attended"]) for a in rows])

    @app.post("/api/clinic/appointments/<int:aid>/attendance")
    @clinician_only
    def attendance_mark(aid):
        u = me(); a = db().execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone(); st = body().get("status")
        if not a or not (a["clinician_id"] == u["id"] or a["location"] == HOSPITAL[u["hospital_id"]]["name"]):
            abort(404, "Appointment not found.")
        if st not in ("attended", "missed"):
            abort(400, "Choose attended or missed.")
        if a["when_iso"] > datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"):
            abort(409, "This appointment hasn't happened yet.")
        db().execute("UPDATE appointments SET attended=?,attended_at=?,attended_by=? WHERE id=?", (st, time.time(), u["id"], aid))
        p = db().execute("SELECT * FROM patients WHERE id=?", (a["patient_id"],)).fetchone()
        if st == "missed" and not db().execute("SELECT 1 FROM followups WHERE appointment_id=? AND kind='missed'", (aid,)).fetchone():
            chw = last_chw(p["id"])
            db().execute("INSERT INTO followups(patient_id,chw_id,kind,appointment_id,status,created) VALUES(?,?,?,?,?,?)", (p["id"], chw, "missed", aid, "open", time.time()))
            if chw:
                c = db().execute("SELECT * FROM users WHERE id=?", (chw,)).fetchone()
                notify_user(chw, "Missed eye appointment", f"{p['name']} missed the eye appointment on {when_text(a['when_iso'])} at {a['location']}. Please follow up.")
                send_sms(c["phone"], f"AfriEye: {p['name']} missed the eye appointment at {a['location']}. Please visit or call them and help them book again.", "missed_followup", p["id"])
            notify_patient_people(p["id"], "Missed eye appointment", f"The eye appointment on {when_text(a['when_iso'])} was missed. Your health worker will contact you to book again.")
        audit("attendance_" + st, p["id"], str(aid)); db().commit(); return jsonify(ok=True)

    @app.post("/api/clinic/patients/<int:pid>/treatments")
    @clinician_only
    def treatment_add(pid):
        if not may_view_face(pid):
            abort(403, "You can only record treatment for your hospital's patients.")
        b = body(); t = b.get("treatment"); eye = b.get("eye") if b.get("eye") in ("right", "left", "both") else None
        if t not in TREATMENTS:
            abort(400, "Choose the treatment given.")
        vr, vl = b.get("va_right") or None, b.get("va_left") or None
        if any(v and v not in VISION for v in (vr, vl)):
            abort(400, "Choose the vision from the list.")
        db().execute("INSERT INTO treatments(patient_id,clinician_id,treatment,eye,va_right,va_left,note,created) VALUES(?,?,?,?,?,?,?,?)", (pid, session["uid"], t, eye, vr, vl, clean(b.get("note"), "note", 300, False), time.time()))
        notify_patient_people(pid, "Treatment recorded", f"{me()['name']} recorded your eye treatment: {TREATMENTS[t]}.")
        audit("treatment", pid, t); db().commit(); return jsonify(ok=True, treatments=treatments_json(pid)), 201

    @app.get("/api/chw/followups")
    @chw_only
    def chw_followups():
        u = me()
        missed = [dict(id=f["id"], patient_id=f["patient_id"], name=f["name"], phone=f["phone"], village=f["village"], when=stamp(f["created"]), appt=when_text(f["when_iso"]) if f["when_iso"] else "", location=f["location"] or "")
                  for f in db().execute("SELECT f.*,p.name,p.phone,p.village,a.when_iso,a.location FROM followups f JOIN patients p ON p.id=f.patient_id LEFT JOIN appointments a ON a.id=f.appointment_id WHERE f.chw_id=? AND f.status='open' AND f.kind='missed' ORDER BY f.created", (u["id"],))]
        due = []
        for p in db().execute("SELECT DISTINCT p.* FROM patients p JOIN screenings s ON s.patient_id=p.id WHERE s.chw_id=?", (u["id"],)).fetchall():
            r = recall_info(p["id"])
            if r and r["due"] <= time.time() + 30 * 86400:
                due.append(dict(patient_id=p["id"], name=p["name"], phone=p["phone"], village=p["village"], birth_year=p["birth_year"], area=p["area_id"], email=None, due=r["due_text"], overdue=r["overdue"], reason=r["reason"], months=r["months"], _d=r["due"]))
        due.sort(key=lambda x: x.pop("_d"))
        return jsonify(missed=missed, recall=due)

    @app.post("/api/chw/followups/<int:fid>/done")
    @chw_only
    def followup_done(fid):
        f = db().execute("SELECT * FROM followups WHERE id=? AND chw_id=? AND status='open'", (fid, session["uid"])).fetchone()
        if not f:
            abort(404, "Follow-up not found.")
        db().execute("UPDATE followups SET status='done',note=?,done=? WHERE id=?", (clean(body().get("note"), "note", 200, False), time.time(), fid))
        audit("followup_done", f["patient_id"], str(fid)); db().commit(); return jsonify(ok=True)

    @app.post("/api/patients/<int:pid>/tips")
    @login_required
    def tips_toggle(pid):
        owner_required(pid); on = bool(body().get("on"))
        db().execute("UPDATE patients SET tips=? WHERE id=?", (1 if on else 0, pid)); db().commit(); return jsonify(ok=True, on=on)

    # ======== How well the AI agrees with the eye specialists ========
    def model_check(hospitals=None, areas=None):
        """Compares the AI's first result with the specialist's grade, for every referral a specialist reviewed.
        Screenings the AI didn't refer aren't reviewed, so this measures how often referrals were right, not missed cases."""
        q = "SELECT s.outcome,s.is_sample,r.worst FROM screenings s JOIN results r ON r.id=s.result_id JOIN patients p ON p.id=s.patient_id WHERE s.result_id IS NOT NULL"; args = []
        if hospitals is not None:
            q += f" AND s.hospital_id IN ({','.join('?' * len(hospitals))})" if hospitals else " AND 0"; args += hospitals
        if areas is not None:
            q += f" AND p.area_id IN ({','.join('?' * len(areas))})" if areas else " AND 0"; args += areas
        rows = db().execute(q, args).fetchall(); real = [r for r in rows if not r["is_sample"]]
        def stats(rs):
            rs = [r for r in rs if r["worst"] != "ungradable"]
            ref = [r for r in rs if r["outcome"] in ("refer", "urgent")]
            agree = sum(r["worst"] in ("moderate", "severe", "proliferative") for r in ref)
            urg = [r for r in rs if r["outcome"] == "urgent"]; sev = [r for r in rs if r["worst"] in ("severe", "proliferative")]
            grid = {o: {g: sum(r["outcome"] == o and r["worst"] == g for r in rs) for g in ("none", "mild", "moderate", "severe", "proliferative")} for o in ("rescreen", "refer", "urgent")}
            return dict(n=len(rs), referred=len(ref), confirmed=agree, ppv=round(agree / len(ref), 3) if ref else None,
                        urgent=len(urg), urgent_confirmed=sum(r["worst"] in ("severe", "proliferative") for r in urg), severe=len(sev), severe_flagged_urgent=sum(r["outcome"] == "urgent" for r in sev), grid=grid)
        return dict(all=stats(rows), real=stats(real), sample=len(rows) - len(real))

    @app.get("/api/clinic/model-check")
    @clinician_only
    def clinic_model_check():
        return jsonify(model_check(hospitals=[me()["hospital_id"]]))

    # ======== County health dashboard: totals only, no names ========
    @app.get("/api/county/summary")
    @county_only
    def county_summary():
        u = me(); county = u["county"]; areas = [a for a in AREA.values() if a["county"] == county]; ids = [a["id"] for a in areas]
        hosp = [h for h in HOSPITAL.values() if h["county"] == county]
        period = request.args.get("period", "year"); start = period_starts().get(period, 0) if period != "all" else 0
        ph = ",".join("?" * len(ids)) or "''"
        scr = db().execute(f"SELECT s.*,p.area_id FROM screenings s JOIN patients p ON p.id=s.patient_id WHERE p.area_id IN ({ph}) AND s.screened_at>=?", ids + [start]).fetchall()
        res = db().execute(f"SELECT r.level,r.created,p.area_id FROM results r JOIN patients p ON p.id=r.patient_id WHERE p.area_id IN ({ph}) AND r.created>=?", ids + [start]).fetchall()
        appts = db().execute(f"SELECT a.attended FROM appointments a JOIN patients p ON p.id=a.patient_id WHERE p.area_id IN ({ph}) AND a.when_iso<? AND a.created>=?", ids + [datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"), start]).fetchall()
        treat = db().execute(f"SELECT t.treatment FROM treatments t JOIN patients p ON p.id=t.patient_id WHERE p.area_id IN ({ph}) AND t.created>=?", ids + [start]).fetchall()
        pats = db().execute(f"SELECT * FROM patients WHERE area_id IN ({ph})", ids).fetchall()
        reviewed = [s for s in scr if s["reviewed"]]; waits = sorted((s["reviewed"] - s["created"]) / 86400 for s in reviewed)
        by_area = []
        for a in areas:
            sa = [s for s in scr if s["area_id"] == a["id"]]; ra = [r for r in res if r["area_id"] == a["id"]]
            by_area.append(dict(id=a["id"], name=a["name"], lat=a["lat"], lng=a["lng"], screenings=len(sa), people=len({s["patient_id"] for s in sa}), ai_referred=sum(s["outcome"] != "rescreen" for s in sa),
                                urgent=sum(s["outcome"] == "urgent" for s in sa), dr_confirmed=sum(r["level"] in ("refer", "urgent") for r in ra), sight_threatening=sum(r["level"] == "urgent" for r in ra)))
        att = dict(attended=sum(a["attended"] == "attended" for a in appts), missed=sum(a["attended"] == "missed" for a in appts), unknown=sum(not a["attended"] for a in appts))
        known = att["attended"] + att["missed"]
        recall_due = sum(1 for p in pats for r in [recall_info(p["id"])] if r and r["overdue"])
        cover = counted((COVER.get(p["cover"], "Not recorded") if p["cover"] else "Not recorded" for p in pats), list(COVER.values()) + ["Not recorded"])
        waiting = {h["id"]: db().execute("SELECT COUNT(*) c FROM screenings WHERE status='waiting_review' AND hospital_id=?", (h["id"],)).fetchone()["c"] for h in hosp}
        late = {h["id"]: db().execute("SELECT COUNT(*) c FROM screenings WHERE status='waiting_review' AND hospital_id=? AND created<?", (h["id"], time.time() - REFERRAL_DAYS * 86400)).fetchone()["c"] for h in hosp}
        chws = db().execute(f"SELECT COUNT(*) c FROM users WHERE role='chw' AND approved=1 AND area IN ({ph})", ids).fetchone()["c"]
        trained = db().execute(f"SELECT COUNT(*) c FROM users WHERE role='chw' AND approved=1 AND trained IS NOT NULL AND area IN ({ph})", ids).fetchone()["c"]
        return jsonify(county=county, period=period, areas=by_area, hospitals=[dict(name=h["name"], lat=h["lat"], lng=h["lng"], waiting=waiting[h["id"]], overdue=late[h["id"]]) for h in hosp],
                       totals=dict(screenings=len(scr), people=len({s["patient_id"] for s in scr}), ai_referred=sum(s["outcome"] != "rescreen" for s in scr), urgent=sum(s["outcome"] == "urgent" for s in scr),
                                   dr_confirmed=sum(r["level"] in ("refer", "urgent") for r in res), sight_threatening=sum(r["level"] == "urgent" for r in res), sample=sum(s["is_sample"] for s in scr)),
                       wait=dict(reviewed=len(waits), median_days=round(waits[len(waits) // 2], 1) if waits else None, within_7=round(sum(w <= REFERRAL_DAYS for w in waits) / len(waits), 3) if waits else None),
                       attendance=dict(att, rate=round(att["attended"] / known, 3) if known else None), treatments=counted(TREATMENTS.get(t["treatment"], "Other") for t in treat),
                       recall_overdue=recall_due, cover=cover, chws=chws, chws_trained=trained, outcomes=counted((AI_TEXT_SHORT[s["outcome"]] for s in scr), list(AI_TEXT_SHORT.values())),
                       model=model_check(areas=ids))

    # ======== Verifying new clinicians: the county health office checks each licence ========
    def staff_json(r):
        h = HOSPITAL.get(r["hospital_id"]); rev = db().execute("SELECT name FROM users WHERE id=?", (r["reviewed_by"],)).fetchone() if r["reviewed_by"] else None
        return dict(id=r["id"], name=r["name"], phone=r["phone"], email=r["email"], hospital=h["name"] if h else "", licence=r["staff_id"], registered=stamp(r["created"] or time.time()), joined=bool(r["pw"]),
                    status="approved" if r["approved"] else "rejected" if r["rejected"] else "waiting", reason=r["rejected"], reviewed_by=rev["name"] if rev else None, reviewed=stamp(r["reviewed_at"]) if r["reviewed_at"] else None)

    def county_clinician(uid):
        r = db().execute("SELECT * FROM users WHERE id=? AND role='clinician'", (uid,)).fetchone()
        if not r or HOSPITAL.get(r["hospital_id"], {}).get("county") != me()["county"]:
            abort(404, "Clinician not found in your county.")
        return r

    @app.get("/api/county/staff")
    @county_only
    def county_staff():
        hs = [h["id"] for h in HOSPITAL.values() if h["county"] == me()["county"]]
        ph = ",".join("?" * len(hs)) or "''"
        rows = db().execute(f"SELECT * FROM users WHERE role='clinician' AND hospital_id IN ({ph}) ORDER BY approved, rejected IS NOT NULL, created DESC", hs).fetchall()
        return jsonify([staff_json(r) for r in rows])

    @app.post("/api/county/staff/<int:uid>/<action>")
    @county_only
    def county_staff_act(uid, action):
        r = county_clinician(uid); u = me()
        if action == "approve":
            db().execute("UPDATE users SET approved=1,rejected=NULL,reviewed_by=?,reviewed_at=? WHERE id=?", (u["id"], time.time(), uid))
            send_sms(r["phone"], f"AfriEye: your clinician account at {HOSPITAL[r['hospital_id']]['name']} was verified by the {u['county']} County health office. You can now see referrals.", "verified")
            notify_user(uid, "Account verified", f"The {u['county']} County health office verified your account. You can now see referrals.")
        elif action == "reject":
            reason = clean(body().get("reason"), "reason", 200)
            db().execute("UPDATE users SET approved=0,rejected=?,reviewed_by=?,reviewed_at=? WHERE id=?", (reason, u["id"], time.time(), uid))
            send_sms(r["phone"], f"AfriEye: your clinician account was not approved by the {u['county']} County health office. Reason: {reason}", "verify_rejected")
        else:
            abort(404, "Unknown action.")
        audit("staff_" + action, None, str(uid)); db().commit(); return jsonify(ok=True)

    # ======== CHW training ========
    @app.post("/api/chw/training")
    @chw_only
    def training_submit():
        ans = body().get("answers") or []
        score = sum(1 for i, a in enumerate(QUIZ) if i < len(ans) and ans[i] == a); passed = score >= len(QUIZ) - 1
        if passed:
            db().execute("UPDATE users SET trained=?, train_score=? WHERE id=?", (time.time(), score, session["uid"])); audit("training_passed", None, str(score)); db().commit()
        return jsonify(score=score, total=len(QUIZ), passed=passed, wrong=[i for i, a in enumerate(QUIZ) if i >= len(ans) or ans[i] != a])

    @app.get("/api/chw/certificate")
    @chw_only
    def training_certificate():
        u = me()
        if not u["trained"]:
            abort(404, "Pass the training quiz first.")
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.pdfgen import canvas as rcanvas
        buf = BytesIO(); W, H = landscape(A4); c = rcanvas.Canvas(buf, pagesize=(W, H))
        stripes = [("#000000", 10), ("#FFFFFF", 3), ("#BB0000", 8), ("#FFFFFF", 3), ("#006600", 10)]; y = H
        for col, h_ in stripes:
            y -= h_; c.setFillColor(col); c.rect(0, y, W, h_, stroke=0, fill=1)
        logo = pathlib.Path(__file__).resolve().parent / "static" / "logo.png"
        if logo.exists():
            c.drawImage(str(logo), W / 2 - 45, H - 165, 90, 110, mask="auto")
        c.setFillColor("#1C1C1C"); c.setFont("Times-Bold", 30); c.drawCentredString(W / 2, H - 205, "Certificate of training")
        c.setFont("Times-Roman", 15); c.drawCentredString(W / 2, H - 240, "This certifies that")
        c.setFont("Times-Bold", 26); c.drawCentredString(W / 2, H - 278, u["name"])
        c.setFont("Times-Roman", 15); c.drawCentredString(W / 2, H - 310, f"community health worker {u['staff_id'] or ''}, completed AfriEye training in diabetic eye screening")
        c.drawCentredString(W / 2, H - 332, f"with a quiz score of {u['train_score']} out of {len(QUIZ)}, on {datetime.fromtimestamp(u['trained'], TZ).strftime('%d %B %Y')}.")
        c.setFont("Times-Italic", 13); c.setFillColor("#A3121B"); c.drawCentredString(W / 2, 70, "Kuja Utuone")
        c.setFillColor("#555555"); c.setFont("Times-Roman", 10); c.drawCentredString(W / 2, 50, "AfriEye prototype by Team empOUR, Samsung Innovation Campus. This training does not replace a recognised health qualification.")
        c.showPage(); c.save(); audit("certificate"); db().commit()
        return Response(buf.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": 'inline; filename="afrieye-training-certificate.pdf"'})

    # ======== USSD: works on any phone, with no internet ========
    def ussd_reply(text, end=False):
        return Response(("END " if end else "CON ") + text, mimetype="text/plain")

    U_TXT = {
        "en": dict(menu="AfriEye eye care\n1. Book an eye doctor\n2. Ask a CHW to visit\n3. My result and appointment\n4. Remind me of my appointment\n5. Kiswahili", hosp="Choose a hospital:", when="When would you like to go?\n1. This week\n2. Next week",
                   booked="Request sent to {h}. The clinic will SMS you a date.", visit="Ask a community health worker to visit:\n1. Me at home\n2. My village, to check a group\n0. Cancel", people="How many people need an eye check? Enter a number:", vsent="Request sent. A health worker in {a} will SMS you about the village visit.", visited="Request sent. A health worker in {a} will SMS you.", cancel="Nothing was sent.",
                   none="No specialist result yet.", noappt="No appointment booked.", remind="Remind me:\n1. The day before\n2. On the morning", reminded="Done. We will SMS you.", lang="Language set to Kiswahili.", invalid="Please choose a number from the menu.",
                   welcome="Welcome to AfriEye eye care.\n1. Register", name="Enter your full name:", area="Choose your area:", consent="Agree to get eye care messages by SMS?\n1. Yes\n2. No", registered="You are registered. Dial again to book an eye doctor."),
        "sw": dict(menu="AfriEye huduma ya macho\n1. Panga miadi na daktari wa macho\n2. Omba CHW atembelee\n3. Matokeo na miadi yangu\n4. Nikumbushe miadi\n5. English", hosp="Chagua hospitali:", when="Ungependa kwenda lini?\n1. Wiki hii\n2. Wiki ijayo",
                   booked="Ombi limetumwa kwa {h}. Kliniki itakutumia SMS ya tarehe.", visit="Omba mhudumu wa afya ya jamii atembelee:\n1. Mimi nyumbani\n2. Kijiji changu, kupima kikundi\n0. Ghairi", people="Ni watu wangapi wanahitaji kupimwa macho? Andika nambari:", vsent="Ombi limetumwa. Mhudumu wa afya wa {a} atakutumia SMS kuhusu ziara ya kijiji.", visited="Ombi limetumwa. Mhudumu wa afya wa {a} atakutumia SMS.", cancel="Hakuna kilichotumwa.",
                   none="Bado hakuna matokeo ya daktari.", noappt="Hakuna miadi.", remind="Nikumbushe:\n1. Siku moja kabla\n2. Asubuhi ya siku hiyo", reminded="Sawa. Tutakutumia SMS.", lang="Lugha sasa ni English.", invalid="Tafadhali chagua nambari kwenye menyu.",
                   welcome="Karibu AfriEye huduma ya macho.\n1. Jisajili", name="Andika jina lako kamili:", area="Chagua eneo lako:", consent="Ukubali kupokea ujumbe wa SMS kuhusu macho?\n1. Ndiyo\n2. Hapana", registered="Umesajiliwa. Piga tena kupanga miadi."),
    }

    @app.post("/api/ussd/<key>")
    def ussd(key):
        """Africa's Talking USSD callback. Each answer arrives joined with *, for example 1*2."""
        if not app.config["USSD_KEY"] or not secrets.compare_digest(key, app.config["USSD_KEY"]):
            abort(403, "Not allowed.")
        f = request.form; parts = [x for x in str(f.get("text", "")).split("*") if x != ""] if f.get("text") else []
        try:
            phone = normalise_phone(f.get("phoneNumber"))
        except Exception:
            return ussd_reply("AfriEye: this service needs a Kenyan mobile number.", True)
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        link = db().execute("SELECT l.patient_id FROM links l WHERE l.user_id=? AND l.access='owner' AND l.active=1", (u["id"],)).fetchone() if u else None
        areas = list(AREA.values())
        if not link:  # register a new patient
            T = U_TXT["en"]
            if not parts:
                return ussd_reply(T["welcome"])
            if parts[0] != "1":
                return ussd_reply(T["invalid"], True)
            if len(parts) == 1:
                return ussd_reply(T["name"])
            if len(parts) == 2:
                return ussd_reply(T["area"] + "\n" + "\n".join(f"{i + 1}. {a['name']}" for i, a in enumerate(areas)))
            if len(parts) == 3:
                return ussd_reply(T["consent"])
            try:
                area = areas[int(parts[2]) - 1]; name = clean(parts[1], "name", 80)
            except (ValueError, IndexError):
                return ussd_reply(T["invalid"], True)
            if u and u["role"] != "member":
                return ussd_reply(T["invalid"], True)
            u2, _ = ensure_user(phone, name); db().execute("UPDATE users SET approved=1 WHERE id=?", (u2["id"],))
            pid = db().execute("INSERT INTO patients(name,phone,village,area_id,sms_consent,created_by,created) VALUES(?,?,?,?,?,?,?)", (name, phone, area["name"], area["id"], 1 if parts[3] == "1" else 0, u2["id"], time.time())).lastrowid
            db().execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,?,?,?,?)", (pid, u2["id"], "owner", "Self", u2["id"], time.time())); db().commit()
            return ussd_reply(T["registered"], True)
        pid = link["patient_id"]; p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone(); lang = u["lang"] if u["lang"] in ("en", "sw") else "en"; T = U_TXT[lang]
        if not parts:
            return ussd_reply(T["menu"])
        choice = parts[0]
        if choice == "1":
            near = sorted(HOSPITAL.values(), key=lambda h: km(AREA[p["area_id"]], h) if p["area_id"] in AREA else 0)[:3]
            if len(parts) == 1:
                return ussd_reply(T["hosp"] + "\n" + "\n".join(f"{i + 1}. {h['name']}" for i, h in enumerate(near)))
            if len(parts) == 2:
                return ussd_reply(T["when"])
            try:
                h = near[int(parts[1]) - 1]; days = {"1": 2, "2": 8}[parts[2]]
            except (ValueError, IndexError, KeyError):
                return ussd_reply(T["invalid"], True)
            if db().execute("SELECT 1 FROM consultations WHERE patient_id=? AND status='requested'", (pid,)).fetchone():
                return ussd_reply(T["booked"].format(h=h["name"]), True)
            d = (datetime.now(TZ) + timedelta(days=days)).strftime("%Y-%m-%d")
            db().execute("INSERT INTO consultations(patient_id,hospital_id,preferred_date,reason,status,created) VALUES(?,?,?,?,?,?)", (pid, h["id"], d, "Booked by USSD", "requested", time.time()))
            ids = [r["id"] for r in db().execute("SELECT id FROM users WHERE role='clinician' AND approved=1 AND hospital_id=?", (h["id"],))]
            for i in ids:
                notify_user(i, "New consultation request", f"{p['name']} booked by USSD for around {d}.")
            email_clinicians(ids, "AfriEye: new consultation request (USSD)", f"{p['name']} ({p['village'] or 'area not given'}) booked by USSD.\nPreferred date: around {d}\n\nReply at {app.config['PUBLIC_URL']}", "consult_request", pid)
            db().commit(); return ussd_reply(T["booked"].format(h=h["name"]), True)
        if choice == "2":
            if len(parts) == 1:
                return ussd_reply(T["visit"])
            if parts[1] == "2":  # a village visit for a group
                if len(parts) == 2:
                    return ussd_reply(T["people"])
                try:
                    n = int(parts[2])
                except ValueError:
                    return ussd_reply(T["invalid"], True)
                if not 2 <= n <= 200:
                    return ussd_reply(T["invalid"], True)
                try:
                    request_visit(pid, "ussd", "", "village", n)
                except Exception:
                    pass  # already requested
                db().commit(); return ussd_reply(T["vsent"].format(a=p["village"] or ""), True)
            if parts[1] != "1":
                return ussd_reply(T["cancel"], True)
            try:
                request_visit(pid, "ussd")
            except Exception:
                pass  # already requested
            db().commit(); return ussd_reply(T["visited"].format(a=p["village"] or ""), True)
        if choice == "3":
            v = patient_view(pid, False); st, nx = v["status"], v["next_appointment"]
            if lang == "sw":
                res = GRADES_SW[st["worst"]] if st else T["none"]
                appt = f"Miadi: {when_text(nx['when'], 'sw')}, {nx['location']}" if nx else T["noappt"]
            else:
                res = st["label"] if st else T["none"]
                appt = f"Appointment: {nx['when_text']}, {nx['location']}" if nx else T["noappt"]
            text = f"{res}.\n{appt}"
            if p["sms_consent"]:
                send_sms(phone, f"AfriEye: {text}".replace("\n", " "), "ussd_result", pid); db().commit()
            return ussd_reply(text, True)
        if choice == "4":
            nx = patient_view(pid, False)["next_appointment"]
            if not nx:
                return ussd_reply(T["noappt"], True)
            if len(parts) == 1:
                return ussd_reply(T["remind"])
            appt = datetime.fromisoformat(nx["when"]).replace(tzinfo=TZ)
            at = appt - timedelta(days=1) if parts[1] == "1" else appt.replace(hour=7, minute=0)
            db().execute("INSERT INTO reminders(appointment_id,user_id,remind_at,channels,created) VALUES(?,?,?,?,?)", (nx["id"], u["id"], max(at.timestamp(), time.time()), json.dumps(["sms"]), time.time())); db().commit()
            return ussd_reply(T["reminded"], True)
        if choice == "5":
            db().execute("UPDATE users SET lang=? WHERE id=?", ("sw" if lang == "en" else "en", u["id"])); db().commit()
            return ussd_reply(T["lang"], True)
        return ussd_reply(T["invalid"], True)

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
