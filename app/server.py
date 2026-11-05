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
    from .sms import GRADES, compose, provider_from_env, segments, when_text, worst
except ImportError:
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
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw TEXT, role TEXT NOT NULL DEFAULT 'member', code_hash TEXT, code_expires REAL, code_tries INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, birth_year INTEGER, phone TEXT NOT NULL, village TEXT, sms_consent INTEGER NOT NULL DEFAULT 0, created_by INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS links(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, user_id INTEGER NOT NULL, access TEXT NOT NULL, relation TEXT, receive_sms INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, added_by INTEGER, created REAL, UNIQUE(patient_id, user_id));
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, left_grade TEXT, right_grade TEXT, worst TEXT NOT NULL, level TEXT NOT NULL, note TEXT, clinician_id INTEGER NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, when_iso TEXT NOT NULL, location TEXT NOT NULL, clinician_id INTEGER NOT NULL, result_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS screenings(id INTEGER PRIMARY KEY, client_ref TEXT UNIQUE NOT NULL, patient_id INTEGER NOT NULL, chw_id INTEGER NOT NULL, outcome TEXT NOT NULL, status TEXT NOT NULL, model TEXT, eyes TEXT NOT NULL, screened_at REAL, created REAL, clinician_id INTEGER, result_id INTEGER, appointment_id INTEGER, review_note TEXT, reviewed REAL);
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
        SECRET_KEY=os.environ.get("AFRIEYE_SECRET"), DB=os.environ.get("AFRIEYE_DB", str(HERE / "afrieye.db")),
        PUBLIC_URL=os.environ.get("AFRIEYE_PUBLIC_URL", "http://localhost:8200"), CLINIC_PHONE=os.environ.get("AFRIEYE_CLINIC_PHONE", "0700000000"),
        SMS_DETAIL=os.environ.get("AFRIEYE_SMS_DETAIL", "full"), WEBHOOK_KEY=os.environ.get("AFRIEYE_WEBHOOK_KEY", ""), SMS=None,
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
        open_paths = {"/api/auth/login", "/api/auth/activate", "/api/auth/forgot"}
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

    @app.post("/api/auth/forgot")
    def forgot():
        phone = normalise_phone(body().get("phone"))
        u = db().execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if u and time.time() > (u["code_expires"] or 0) - 47.9 * 3600:  # at most one code every 6 minutes
            code = issue_code(u["id"]); send_sms(phone, f"AfriEye: Your sign-in code is {code}. It expires in 48 hours. Do not share it.", "code"); db().commit()
        return jsonify(ok=True, message="If that number has an account, we sent a code by SMS.")

    @app.post("/api/auth/logout")
    def logout():
        session.clear(); return jsonify(ok=True)

    def me_json():
        u = me()
        rows = db().execute("SELECT p.id,p.name,l.access,l.relation FROM links l JOIN patients p ON p.id=l.patient_id WHERE l.user_id=? AND l.active=1 ORDER BY l.access='owner' DESC,p.name", (u["id"],)).fetchall()
        return jsonify(signed_in=True, id=u["id"], name=u["name"], role=u["role"], csrf=session.get("csrf"), patients=[dict(r) for r in rows], sms_mode=app.config["SMS"].name)

    @app.get("/api/me")
    def api_me():
        if not me():
            return jsonify(signed_in=False)
        return me_json()

    # ---- Community health worker
    def saved_patient(b, chw_id):
        info = b.get("patient") or {}
        name = clean(info.get("name"), "patient's name"); phone = normalise_phone(info.get("phone")); village = clean(info.get("village"), "village", 60, False)
        by = info.get("birth_year")
        if by in (None, ""):
            by = None
        else:
            try:
                by = int(by); assert 1900 <= by <= datetime.now(TZ).year
            except (ValueError, TypeError, AssertionError):
                abort(400, "Enter a valid birth year.")
        consent = 1 if info.get("sms_consent") else 0
        row = db().execute("SELECT * FROM patients WHERE phone=? AND name=?", (phone, name)).fetchone()
        if row:
            db().execute("UPDATE patients SET sms_consent=?, village=COALESCE(NULLIF(?,''),village), birth_year=COALESCE(?,birth_year) WHERE id=?", (consent, village, by, row["id"]))
            return row["id"]
        u, _ = ensure_user(phone, name)
        if u["role"] != "member":
            abort(400, "Use the patient's own phone number.")
        pid = db().execute("INSERT INTO patients(name,birth_year,phone,village,sms_consent,created_by,created) VALUES(?,?,?,?,?,?,?)", (name, by, phone, village, consent, chw_id, time.time())).lastrowid
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
            out["patient"] = dict(id=p["id"], name=p["name"], birth_year=p["birth_year"], village=p["village"], sms_consent=bool(p["sms_consent"]))
            out["chw"] = dict(name=c["name"], phone=c["phone"])
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
        pid = saved_patient(b, session["uid"])
        for rel in b.get("family") or []:
            link_relative(pid, rel, session["uid"])
        now = time.time()
        try:
            at = float(b.get("screened_at") or now)
        except (TypeError, ValueError):
            at = now
        sid = db().execute("INSERT INTO screenings(client_ref,patient_id,chw_id,outcome,status,model,eyes,screened_at,created) VALUES(?,?,?,?,?,?,?,?,?)",
                           (ref, pid, session["uid"], outcome, "no_referral" if outcome == "rescreen" else "waiting_review", clean(b.get("model"), "model name", 60, False), json.dumps(summary), min(at, now), now)).lastrowid
        for eye, raw in photos.items():
            db().execute("INSERT INTO screening_photos VALUES(?,?,?)", (sid, eye, raw))
        audit("screening", pid, outcome); db().commit()
        return jsonify(screening_json(db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone())), 201

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
        if not (u["role"] == "clinician" or (u["role"] == "chw" and s["chw_id"] == u["id"])):
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
        waiting = db().execute("SELECT * FROM screenings WHERE status='waiting_review' ORDER BY outcome='urgent' DESC, created ASC").fetchall()
        done = db().execute("SELECT * FROM screenings WHERE status IN ('confirmed','no_visit') ORDER BY reviewed DESC LIMIT 50").fetchall()
        return jsonify(waiting=[screening_json(s) for s in waiting], reviewed=[screening_json(s) for s in done])

    def waiting_referral(sid):
        s = db().execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()
        if not s:
            abort(404, "Referral not found.")
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
        return dict(id=a["id"], when=a["when_iso"], when_text=when_text(a["when_iso"]), location=a["location"], upcoming=a["when_iso"] >= datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"))

    def patient_view(pid, include_family):
        p = db().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404, "Patient not found.")
        results = [result_json(r) for r in db().execute("SELECT * FROM results WHERE patient_id=? ORDER BY created DESC,id DESC", (pid,))]
        appts = [appt_json(a) for a in db().execute("SELECT * FROM appointments WHERE patient_id=? ORDER BY when_iso DESC", (pid,))]
        msgs = [dict(body=m["body"], status=m["status"], when=stamp(m["created"])) for m in db().execute("SELECT * FROM sms WHERE patient_id=? AND to_phone=? AND kind NOT IN ('invite','code') ORDER BY created DESC,id DESC LIMIT 10", (pid, p["phone"]))]
        scans = [dict(date=stamp(s["screened_at"]), text=AI_TEXT[s["outcome"]], status=STATUS_TEXT[s["status"]]) for s in db().execute("SELECT * FROM screenings WHERE patient_id=? ORDER BY screened_at DESC", (pid,))]
        up = sorted((a for a in appts if a["upcoming"]), key=lambda a: a["when"])
        out = dict(patient=dict(id=p["id"], name=p["name"], birth_year=p["birth_year"], sms_consent=bool(p["sms_consent"])), status=results[0] if results else None, next_appointment=up[0] if up else None, appointments=appts, results=results, messages=msgs, screenings=scans)
        if include_family:
            out["family"] = [dict(r) for r in db().execute("SELECT l.id,u.name,u.phone,l.relation,l.access,l.receive_sms,u.pw IS NOT NULL joined FROM links l JOIN users u ON u.id=l.user_id WHERE l.patient_id=? AND l.active=1 ORDER BY l.access='owner' DESC,u.name", (pid,))]
        return out

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
