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


def setup(ok=True):
    d = tempfile.mkdtemp(); sms = FakeSMS(ok); path = os.path.join(d, "t.db")
    app = create_app({"SECRET_KEY": "x" * 40, "DB": path, "SMS": sms, "TESTING": True}); seed(path)
    return app, sms


def sign_in(app, i):
    c = app.test_client(); phone, _, _, pw = DEMO_USERS[i]
    r = c.post("/api/auth/login", json={"phone": phone, "password": pw}); assert r.status_code == 200, r.json
    c.environ_base["HTTP_X_CSRF"] = r.json["csrf"]
    return c


def screening(ref="r1", level="refer", name="Mary Achieng", phone="0722000111", consent=True, family=None):
    return {"client_ref": ref, "screening_consent": True, "model": "Small CNN", "eyes": [{"eye": "right", "level": level, "message": "m", "p_referable": .7, "p_urgent": .2, "p_quality": .8, "retakes": 0, "photo": JPEG}],
            "hospital": "knh", "clinician": DOC_ID, "patient": {"name": name, "birth_year": 1958, "area": "korogocho", "phone": phone, "sms_consent": consent}, "family": family if family is not None else [{"name": "Jane Achieng", "relation": "Daughter", "phone": "0722000222", "receive_sms": True}]}


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
    c, r = register(app, "clinician", "0733100004", hospital="knh", staff_id="KMPDC-1"); ok = c.post("/api/auth/activate", json={"phone": "0733100004", "code": r.json["test_code"], "password": "clinician-pass-1"})
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


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t(); print("ok", t.__name__)
    print(len(tests), "passed")
