"""End-to-end checks of the server rules:  python -m pytest app/tests  (or run this file directly)."""
import base64, os, pathlib, sys, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from run import DEMO_USERS, seed
from server import create_app

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
            "patient": {"name": name, "birth_year": 1958, "village": "Kiminini", "phone": phone, "sms_consent": consent}, "family": family if family is not None else [{"name": "Jane Achieng", "relation": "Daughter", "phone": "0722000222", "receive_sms": True}]}


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


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t(); print("ok", t.__name__)
    print(len(tests), "passed")
