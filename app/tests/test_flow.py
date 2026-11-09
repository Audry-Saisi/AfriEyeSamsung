"""End-to-end checks of the server rules:  python -m pytest app/tests  (or run this file directly)."""
import base64, os, pathlib, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from run import DEMO_USERS, seed
from server import create_app

DOC_ID = 2
JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"0" * 200).decode()


class FakeSMS:
    name = "test"
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok
    def send(self, to, body):
        self.sent.append((to, body))
        return {"ok": self.ok, "status": "not sent (test mode)" if self.ok else "failed", "provider_id": "x", "error": None}


class FakeEmail:
    name = "test"
    def __init__(self):
        self.sent = []
    def send(self, to, subject, body, attachment=None):
        self.sent.append((to, subject, body, attachment)); return {"ok": True, "status": "not sent (test mode)", "error": None}


MAIL = {}


def setup(ok=True):
    d = tempfile.mkdtemp(); sms = FakeSMS(ok); path = os.path.join(d, "t.db"); mail = FakeEmail()
    app = create_app({"SECRET_KEY": "x" * 40, "DB": path, "SMS": sms, "EMAIL": mail, "TESTING": True}); seed(path); MAIL[id(app)] = mail
    return app, sms


def sign_in(app, i):
    c = app.test_client(); phone, _, _, pw = DEMO_USERS[i]
    r = c.post("/api/auth/login", json={"phone": phone, "password": pw}); assert r.status_code == 200, r.json
    c.environ_base["HTTP_X_CSRF"] = r.json["csrf"]
    return c


def screening(ref="r1", level="refer", name="Mary Achieng", phone="0722000111", consent=True, family=None):
    return {"client_ref": ref, "screening_consent": True, "model": "Small CNN", "eyes": [{"eye": "right", "level": level, "message": "m", "p_referable": .7, "p_urgent": .2, "p_quality": .8, "retakes": 0, "photo": JPEG}],
            "hospital": "knh", "clinician": DOC_ID, "history": {"items": ["High blood pressure"], "diabetes_type": "Type 2", "years": 12, "insulin": False}, "patient": {"name": name, "birth_year": 1958, "area": "korogocho", "phone": phone, "sms_consent": consent}, "family": family if family is not None else [{"name": "Jane Achieng", "relation": "Daughter", "phone": "0722000222", "receive_sms": True}]}


def booking(**k):
    d = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    return {"date": d, "time": "09:00", "location": "Kitale eye clinic", "right": "moderate", **k}


def test_no_sms_until_specialist_confirms():
    app, sms = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    r = chw.post("/api/chw/screenings", json=screening()); assert r.status_code == 201 and r.json["status"] == "waiting_review"
    assert sms.sent == [], "No SMS may go out before the specialist confirms"
    q = doc.get("/api/clinic/referrals").json; assert len(q["waiting"]) == 1
    sid = q["waiting"][0]["id"]
    assert doc.get(f"/api/screenings/{sid}/photo/right").mimetype == "image/jpeg"
    p = doc.post(f"/api/clinic/referrals/{sid}/preview", json=booking()).json; assert set(p["to"]) == {"+254722000111", "+254722000222"}
    r = doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking()); assert r.status_code == 200, r.json
    to = [t for t, _ in sms.sent]
    assert to.count("+254722000111") == 2 and to.count("+254722000222") == 2, to  # appointment SMS and sign-in code for each
    assert "Please attend the eye clinic" in sms.sent[0][1] and "Kitale" in sms.sent[0][1]
    assert doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking()).status_code == 409
    mine = chw.get("/api/chw/screenings").json[0]; assert mine["status"] == "confirmed" and "Kitale" in mine["appointment"]["location"]


def test_family_signs_in_and_sees_the_record():
    app, sms = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    chw.post("/api/chw/screenings", json=screening()); sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]
    doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking())
    code = next(b for t, b in sms.sent if t == "+254722000222" and "code" in b).split("code: ")[1][:6]
    fam = app.test_client(); r = fam.post("/api/auth/activate", json={"phone": "0722000222", "code": code, "password": "family-pass-1"}); assert r.status_code == 200
    pid = r.json["patients"][0]["id"]; rec = fam.get(f"/api/patients/{pid}").json
    assert rec["access"] == "view" and rec["next_appointment"] and rec["status"]["level"] == "refer" and "family" not in rec
    fam.environ_base["HTTP_X_CSRF"] = r.json["csrf"]
    assert fam.post(f"/api/patients/{pid}/sms-consent", json={"consent": False}).status_code == 403
    other = app.test_client(); assert other.get(f"/api/patients/{pid}").status_code == 401


def test_clear_result_needs_no_referral_or_sms():
    app, sms = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    r = chw.post("/api/chw/screenings", json=screening(level="rescreen")); assert r.json["status"] == "no_referral"
    assert doc.get("/api/clinic/referrals").json["waiting"] == [] and sms.sent == []


def test_urgent_goes_first():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    chw.post("/api/chw/screenings", json=screening("a", "refer", "First Person", "0722000301", family=[]))
    chw.post("/api/chw/screenings", json=screening("b", "urgent", "Second Person", "0722000302", family=[]))
    assert doc.get("/api/clinic/referrals").json["waiting"][0]["patient"]["name"] == "Second Person"


def test_offline_upload_sent_twice_is_stored_once():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    assert chw.post("/api/chw/screenings", json=screening()).status_code == 201
    assert chw.post("/api/chw/screenings", json=screening()).status_code == 200
    assert len(doc.get("/api/clinic/referrals").json["waiting"]) == 1


def test_no_consent_means_no_sms():
    app, sms = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    chw.post("/api/chw/screenings", json=screening(consent=False)); sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]
    r = doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking()); assert r.json["consent"] is False and sms.sent == []


def test_no_visit_tells_the_chw():
    app, sms = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    chw.post("/api/chw/screenings", json=screening()); sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]
    assert doc.post(f"/api/clinic/referrals/{sid}/no-visit", json={"right": "none"}).status_code == 400
    r = doc.post(f"/api/clinic/referrals/{sid}/no-visit", json={"right": "none", "note": "Photo shows a normal retina."}); assert r.status_code == 200
    mine = chw.get("/api/chw/screenings").json[0]; assert mine["status"] == "no_visit" and "normal" in mine["review_note"] and sms.sent == []


def test_failed_sms_keeps_the_appointment():
    app, sms = setup(False); chw, doc = sign_in(app, 0), sign_in(app, 1)
    chw.post("/api/chw/screenings", json=screening()); sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]
    r = doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking()); assert r.status_code == 200 and not r.json["sms"][0]["ok"]
    assert doc.get("/api/clinic/referrals").json["reviewed"][0]["status"] == "confirmed"
    log = doc.get("/api/clinic/sms").json; assert any(m["status"] == "failed" for m in log)


def test_roles_are_kept_apart():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1); nobody = app.test_client()
    assert nobody.get("/api/clinic/referrals").status_code == 401 and chw.get("/api/clinic/referrals").status_code == 403
    assert doc.post("/api/chw/screenings", json=screening()).status_code == 403
    chw.post("/api/chw/screenings", json=screening()); sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]
    assert nobody.get(f"/api/screenings/{sid}/photo/right").status_code == 401
    assert chw.post(f"/api/clinic/referrals/{sid}/confirm", json=booking()).status_code == 403


def test_bad_input_is_refused():
    app, _ = setup(); chw = sign_in(app, 0)
    bad = screening("c"); bad["screening_consent"] = False; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    bad = screening("d"); bad["eyes"][0]["level"] = "retake"; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    bad = screening("e"); bad["eyes"][0]["photo"] = "data:image/jpeg;base64,AAAA"; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    bad = screening("f", phone="12345"); assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    bad = screening("g", phone="0711000002"); assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    c = app.test_client(); c.post("/api/auth/login", json={"phone": DEMO_USERS[0][0], "password": DEMO_USERS[0][3]})
    assert c.post("/api/chw/screenings", json=screening("h")).status_code == 403  # no CSRF token


def register(app, role, phone, **k):
    c = app.test_client(); r = c.post("/api/auth/register", json={"role": role, "name": "Test Person", "phone": phone, **k})
    return c, r


def test_register_each_role_needs_the_sms_code():
    app, sms = setup()
    c, r = register(app, "patient", "0733100001", area="korogocho", birth_year=1950, sms_consent=True); assert r.status_code == 201
    bad = c.post("/api/auth/activate", json={"phone": "0733100001", "code": "000000", "password": "patient-pass-1"}); assert bad.status_code == 400
    ok = c.post("/api/auth/activate", json={"phone": "0733100001", "code": r.json["test_code"], "password": "patient-pass-1"}); assert ok.status_code == 200 and ok.json["role"] == "member" and ok.json["patients"]
    _, r = register(app, "patient", "0733100001", area="korogocho"); assert r.status_code == 409
    assert register(app, "boss", "0733100002")[1].status_code == 400
    assert register(app, "chw", "0733100003", area="korogocho")[1].status_code == 400  # no CHW ID


def test_new_staff_cannot_see_patients_until_verified():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1); chw.post("/api/chw/screenings", json=screening())
    c, r = register(app, "clinician", "0733100004", hospital="knh", staff_id="KMPDC-1", email="new.doc@example.org"); ok = c.post("/api/auth/activate", json={"phone": "0733100004", "code": r.json["test_code"], "password": "clinician-pass-1"})
    assert ok.json["approved"] is False; c.environ_base["HTTP_X_CSRF"] = ok.json["csrf"]
    assert c.get("/api/clinic/referrals").status_code == 403 and c.get("/api/screenings/1/photo/right").status_code == 403


def test_a_patient_cannot_register_over_an_existing_record():
    app, _ = setup(); chw = sign_in(app, 0); chw.post("/api/chw/screenings", json=screening())
    assert register(app, "patient", "0722000111", area="kibera")[1].status_code == 409


def test_hospital_and_clinician_are_checked_and_referrals_stay_at_that_hospital():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    bad = screening("x1"); del bad["hospital"]; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    bad = screening("x2"); bad["hospital"] = "cgh"; assert chw.post("/api/chw/screenings", json=bad).status_code == 400  # that clinician isn't at this hospital
    other = chw.get("/api/chw/directory").json; cgh = next(h for h in other["hospitals"] if h["id"] == "cgh")["clinicians"][0]["id"]
    ok = screening("x3"); ok["hospital"], ok["clinician"] = "cgh", cgh; r = chw.post("/api/chw/screenings", json=ok); assert r.status_code == 201 and r.json["hospital"] == "Coast General Hospital"
    assert doc.get("/api/clinic/referrals").json["waiting"] == []
    assert doc.post(f"/api/clinic/referrals/{r.json['id']}/confirm", json=booking()).status_code == 403
    assert doc.get(f"/api/screenings/{r.json['id']}/photo/right").status_code == 403


def test_chw_summary_counts_ages_and_areas():
    app, _ = setup(); chw = sign_in(app, 0); chw.post("/api/chw/screenings", json=screening()); chw.post("/api/chw/screenings", json=screening("b", "rescreen", "Other Person", "0722000333", family=[]))
    s = chw.get("/api/chw/summary").json["periods"]; assert s["today"]["count"] == 2 and s["year"]["count"] == 2
    assert s["today"]["areas"] == [{"label": "Korogocho", "count": 2}] and sum(a["count"] for a in s["today"]["ages"]) == 2 and s["today"]["outcomes"]["refer"] == 1


def test_face_photo_needs_consent_and_is_private():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    bad = screening("f1"); bad["face"] = JPEG; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    ok = screening("f2"); ok["face"], ok["face_consent"] = JPEG, True; r = chw.post("/api/chw/screenings", json=ok); pid = r.json["patient"]["id"]; assert r.json["patient"]["has_face"]
    assert chw.get(f"/api/patients/{pid}/face").mimetype == "image/jpeg" and doc.get(f"/api/patients/{pid}/face").mimetype == "image/jpeg"
    assert app.test_client().get(f"/api/patients/{pid}/face").status_code == 401
    c, rr = register(app, "patient", "0733100009", area="kibera"); a = c.post("/api/auth/activate", json={"phone": "0733100009", "code": rr.json["test_code"], "password": "patient-pass-1"}); assert c.get(f"/api/patients/{pid}/face").status_code == 403


def test_patient_books_a_consultation_and_clinic_confirms():
    app, sms = setup(); doc = sign_in(app, 1); c, r = register(app, "patient", "0733100005", area="korogocho", birth_year=1950, sms_consent=True)
    a = c.post("/api/auth/activate", json={"phone": "0733100005", "code": r.json["test_code"], "password": "patient-pass-1"}).json; pid = a["patients"][0]["id"]; c.environ_base["HTTP_X_CSRF"] = a["csrf"]; sms.sent.clear()
    b = booking(); assert c.post(f"/api/patients/{pid}/consultations", json={"hospital": "knh", "date": b["date"], "reason": "Blurry sight"}).status_code == 201
    assert c.post(f"/api/patients/{pid}/consultations", json={"hospital": "knh", "date": b["date"], "reason": "Again"}).status_code == 409
    q = doc.get("/api/clinic/referrals").json["consultations"]; assert q[0]["reason"] == "Blurry sight"
    assert doc.post(f"/api/clinic/consultations/{q[0]['id']}/confirm", json=b).status_code == 200 and "confirmed" in sms.sent[0][1]
    rec = c.get(f"/api/patients/{pid}").json; assert rec["next_appointment"]["doctor"] == "Dr. Otieno Ouma" and rec["consultations"][0]["status"] == "confirmed"
    assert c.post(f"/api/patients/{pid}/location", json={"area": "kibera"}).status_code == 200 and c.get(f"/api/patients/{pid}").json["patient"]["village"] == "Kibera"


def test_emails_reach_clinician_patient_and_family():
    app, _ = setup(); mail = MAIL[id(app)]; chw, doc = sign_in(app, 0), sign_in(app, 1)
    b = screening(); b["patient"]["email"] = "Mary@Example.org"; b["family"][0]["email"] = "jane@example.org"
    assert chw.post("/api/chw/screenings", json=b).status_code == 201
    assert [m[0] for m in mail.sent] == ["knh.eye@example.org"] and "referral" in mail.sent[0][1].lower(); mail.sent.clear()
    sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]; r = doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking())
    to = [m[0] for m in mail.sent]; assert to == ["mary@example.org", "jane@example.org", "knh.eye@example.org"], to
    assert "Kitale eye clinic" in mail.sent[0][2] and "Dr. Otieno Ouma" in mail.sent[0][2] and len(r.json["emails"]) == 3
    assert doc.get("/api/clinic/emails").json["rows"][0]["to_email"] == "knh.eye@example.org"


def test_no_consent_no_patient_email_and_bad_email_refused():
    app, _ = setup(); mail = MAIL[id(app)]; chw, doc = sign_in(app, 0), sign_in(app, 1)
    bad = screening("e1"); bad["patient"]["email"] = "not-an-email"; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    b = screening("e2", consent=False); b["patient"]["email"] = "mary@example.org"; chw.post("/api/chw/screenings", json=b); mail.sent.clear()
    sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]; doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking())
    assert [m[0] for m in mail.sent] == ["knh.eye@example.org"]


def test_clinician_registration_needs_email_and_patient_can_set_one():
    app, _ = setup(); mail = MAIL[id(app)]
    assert register(app, "clinician", "0733100020", hospital="knh", staff_id="K1")[1].status_code == 400
    assert register(app, "clinician", "0733100021", hospital="knh", staff_id="K2", email="knh.eye@example.org")[1].status_code == 409
    c, r = register(app, "patient", "0733100022", area="korogocho", sms_consent=True); a = c.post("/api/auth/activate", json={"phone": "0733100022", "code": r.json["test_code"], "password": "patient-pass-1"}).json
    c.environ_base["HTTP_X_CSRF"] = a["csrf"]; assert c.post("/api/me/email", json={"email": "me@example.org"}).json["email"] == "me@example.org"
    pid = a["patients"][0]["id"]; mail.sent.clear(); c.post(f"/api/patients/{pid}/consultations", json={"hospital": "knh", "date": booking()["date"], "reason": "Check"})
    assert [m[0] for m in mail.sent] == ["knh.eye@example.org"]; mail.sent.clear()
    doc = sign_in(app, 1); cid = doc.get("/api/clinic/referrals").json["consultations"][0]["id"]; doc.post(f"/api/clinic/consultations/{cid}/confirm", json=booking())
    assert [m[0] for m in mail.sent] == ["me@example.org"] and c.get(f"/api/patients/{pid}").json["emails"]


def test_health_history_is_required_and_reaches_the_clinician_and_patient():
    app, _ = setup(); chw, doc = sign_in(app, 0), sign_in(app, 1)
    bad = screening("h1"); del bad["history"]; assert chw.post("/api/chw/screenings", json=bad).status_code == 400
    chw.post("/api/chw/screenings", json=screening()); s = doc.get("/api/clinic/referrals").json["waiting"][0]
    assert s["patient"]["conditions"]["items"] == ["High blood pressure"] and s["deadline"]["days_left"] == 7


def patient_client(app, sms):
    code = next(b for t, b in sms.sent if t == "+254722000111" and "code" in b).split("code: ")[1][:6]
    c = app.test_client(); a = c.post("/api/auth/activate", json={"phone": "0722000111", "code": code, "password": "patient-pass-1"}).json
    c.environ_base["HTTP_X_CSRF"] = a["csrf"]; return c, a["patients"][0]["id"]


def confirmed(app):
    app_, sms = app; chw, doc = sign_in(app_, 0), sign_in(app_, 1); chw.post("/api/chw/screenings", json=screening())
    sid = doc.get("/api/clinic/referrals").json["waiting"][0]["id"]; doc.post(f"/api/clinic/referrals/{sid}/confirm", json=booking())
    return chw, doc, sid


def test_day_3_alert_and_day_7_hospital_email():
    import sqlite3, time as tm
    app, sms = setup(); mail = MAIL[id(app)]; chw, doc = sign_in(app, 0), sign_in(app, 1); chw.post("/api/chw/screenings", json=screening()); mail.sent.clear(); sms.sent.clear()
    con = sqlite3.connect(app.config["DB"]); con.execute("UPDATE screenings SET created=?", (tm.time() - 3.2 * 86400,)); con.commit()
    with app.app_context():
        assert app.run_due_jobs()["alerts"] == 1 and app.run_due_jobs()["alerts"] == 0
    assert [m[0] for m in mail.sent] == ["knh.eye@example.org"] and any(t == "+254711000002" for t, _ in sms.sent)
    assert any("waiting" in n["title"] for n in doc.get("/api/notifications").json["items"])
    con.execute("UPDATE screenings SET created=?", (tm.time() - 7.1 * 86400,)); con.commit(); mail.sent.clear()
    with app.app_context():
        assert app.run_due_jobs()["escalations"] == 1
    assert mail.sent[0][0] == "knh.eyeclinic@example.org" and "Dr. Otieno Ouma has not taken any action for a period of 7 days" in mail.sent[0][2]
    assert doc.get("/api/clinic/referrals").json["waiting"][0]["deadline"]["escalated"]


def test_reminders_reviews_reports_and_settings():
    import sqlite3, time as tm
    app, sms = setup(); mail = MAIL[id(app)]; chw, doc, sid = confirmed((app, sms)); pat, pid = patient_client(app, sms)
    rec = pat.get(f"/api/patients/{pid}").json; aid = rec["next_appointment"]["id"]
    assert pat.post(f"/api/appointments/{aid}/reminders", json={"when": "day_before", "channels": ["app", "sms"]}).status_code == 201
    assert pat.post(f"/api/appointments/{aid}/reminders", json={"when": "day_before", "channels": ["email"]}).status_code == 400  # no email yet
    con = sqlite3.connect(app.config["DB"]); con.execute("UPDATE reminders SET remind_at=?", (tm.time() - 1,)); con.commit(); sms.sent.clear()
    with app.app_context():
        assert app.run_due_jobs()["reminders"] == 1
    assert "reminder" in sms.sent[0][1] and any(n["title"] == "Appointment reminder" for n in pat.get("/api/notifications").json["items"])
    people = pat.get(f"/api/patients/{pid}").json["people"]; assert {p["role"] for p in people} == {"chw", "clinician"}
    assert pat.post(f"/api/patients/{pid}/reviews", json={"target_id": people[0]["id"], "stars": 5, "comment": "Very kind"}).status_code == 200
    assert pat.post(f"/api/patients/{pid}/reviews", json={"target_id": 9999, "stars": 5}).status_code == 400
    assert chw.get("/api/chw/summary").json["ratings"]["average"] == 5 or doc.get("/api/clinic/summary").json["ratings"]["average"] == 5
    r = doc.post(f"/api/patients/{pid}/reports"); assert r.status_code == 201
    pdf = pat.get(f"/api/reports/{r.json['id']}/pdf"); assert pdf.mimetype == "application/pdf" and pdf.data[:4] == b"%PDF"
    assert pat.get(f"/api/patients/{pid}").json["reports"]; mail.sent.clear()
    assert pat.post(f"/api/reports/{r.json['id']}/email", json={"email": "son@example.org"}).status_code == 200 and mail.sent[0][3][0].endswith(".pdf")
    other = app.test_client(); assert other.get(f"/api/reports/{r.json['id']}/pdf").status_code == 401
    assert pat.post("/api/me/password", json={"current": "wrong", "new": "newpass-123"}).status_code == 400
    assert pat.post("/api/me/password", json={"current": "patient-pass-1", "new": "newpass-123"}).status_code == 200
    assert pat.post("/api/me/lang", json={"lang": "sw"}).status_code == 200 and pat.get("/api/me").json["lang"] == "sw"
    assert b"Mary Achieng" in pat.get("/api/me/export").data
    assert chw.post("/api/me/delete", json={"password": "demo-chw-2026"}).status_code == 400
    assert pat.post("/api/me/delete", json={"password": "newpass-123"}).status_code == 200
    assert doc.get("/api/clinic/referrals").json["reviewed"] == []


def test_ussd_register_book_visit_result_and_reminder():
    app, sms = setup(); app.config["USSD_KEY"] = "k"; c = app.test_client(); chw, doc, sid = confirmed((app, sms))
    def dial(text, phone="+254733400001"):
        return c.post("/api/ussd/k", data={"sessionId": "s", "serviceCode": "*384#", "phoneNumber": phone, "text": text}).get_data(as_text=True)
    assert c.post("/api/ussd/wrong", data={}).status_code == 403
    assert dial("").startswith("CON Welcome") and dial("1*Akinyi Otieno*1*1").startswith("END You are registered")
    assert "Book an eye doctor" in dial("") and "Kenyatta" in dial("1")
    assert dial("1*1*2").startswith("END Request sent to Kenyatta") and doc.get("/api/clinic/referrals").json["consultations"][0]["name"] == "Akinyi Otieno"
    assert dial("2*1").startswith("END Request sent") and chw.get("/api/chw/visits").json[0]["name"] == "Akinyi Otieno"
    vid = chw.get("/api/chw/visits").json[0]["id"]; assert chw.post(f"/api/chw/visits/{vid}/accept").status_code == 200
    out = dial("3", "+254722000111"); assert out.startswith("END Serious eye changes") or "Eye" in out; assert "Appointment:" in out
    assert dial("4*1", "+254722000111").startswith("END Done") and dial("5", "+254722000111").startswith("END Language")
    assert "Panga miadi" in dial("", "+254722000111")


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t(); print("ok", t.__name__)
    print(len(tests), "passed")


def test_chw_report_goes_to_clinician_first():
    app, sms = setup(); chw, doc, sid = confirmed((app, sms)); pat, pid = patient_client(app, sms)
    assert chw.get(f"/api/patients/{pid}/reports/preview").data[:4] == b"%PDF"  # the CHW checks it first
    r = chw.post(f"/api/patients/{pid}/reports"); assert r.status_code == 201 and r.json["to"]
    rid = r.json["id"]
    assert not pat.get(f"/api/patients/{pid}").json["reports"]  # the patient doesn't see it yet
    assert pat.get(f"/api/reports/{rid}/pdf").status_code == 403
    rows = doc.get("/api/clinic/chw-reports").json["rows"]; assert rows[0]["id"] == rid and rows[0]["waiting"]
    assert doc.get(f"/api/reports/{rid}/pdf").data[:4] == b"%PDF"
    assert doc.post(f"/api/reports/{rid}/release").status_code == 200
    assert doc.post(f"/api/reports/{rid}/release").status_code == 409
    assert pat.get(f"/api/patients/{pid}").json["reports"][0]["id"] == rid
    assert pat.get(f"/api/reports/{rid}/pdf").data[:4] == b"%PDF"
    assert any(n["title"] == "Report sent to the patient" for n in chw.get("/api/notifications").json["items"])
