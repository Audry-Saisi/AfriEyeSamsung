"""Start the AfriEye app:  python app/run.py

The first run creates fictional demo accounts and labelled sample screenings. SMS stays in test mode, so nothing is
really sent, until AT_USERNAME and AT_API_KEY are set for Africa's Talking.
"""
import json, os, pathlib, random, secrets, socket, sqlite3, sys, time

from werkzeug.security import generate_password_hash

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from places import AREA  # noqa: E402
from server import SCHEMA, create_app  # noqa: E402

DEMO_PASSWORDS = {"chw": "demo-chw-2026", "clinician": "demo-eye-2026", "member": "demo-patient-2026"}
# Fictional people. Each demo hospital gets one fictional clinician.
DEMO_USERS = [("+254711000001", "Amina Wanjiru", "chw", "demo-chw-2026"), ("+254711000002", "Dr. Otieno Ouma", "clinician", "demo-eye-2026")]
DEMO_DOCTORS = {"knh": ("+254711000002", "Dr. Otieno Ouma"), "lions": ("+254711000011", "Dr. Faith Njeri"), "kikuyu_eye": ("+254711000012", "Dr. Peter Kamau"), "kitale_crh": ("+254711000013", "Dr. Ruth Chebet"),
                "mtrh": ("+254711000014", "Dr. Kevin Kiprop"), "jootrh": ("+254711000015", "Dr. Grace Akinyi"), "sabatia": ("+254711000016", "Dr. Brian Mudavadi"), "nakuru_crh": ("+254711000017", "Dr. Lucy Wairimu"),
                "kerugoya_crh": ("+254711000018", "Dr. John Muriuki"), "cgh": ("+254711000019", "Dr. Salma Hassan")}
SAMPLE_PATIENTS = [("Mary Achieng", 1958, "korogocho"), ("Joseph Mwangi", 1949, "korogocho"), ("Hadija Omar", 1962, "mathare"), ("Peter Wekesa", 1955, "kibera"), ("Grace Nduta", 1968, "korogocho"), ("Samuel Kariuki", 1944, "kawangware"),
                   ("Rose Atieno", 1951, "mathare"), ("David Kimani", 1973, "kibera"), ("Esther Muthoni", 1939, "korogocho"), ("Paul Odhiambo", 1960, "kawangware"), ("Lucy Nyambura", 1947, "mathare"), ("James Ochieng", 1956, "kikuyu")]


def secret():
    f = HERE / ".secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
    return f.read_text().strip()


def seed(path, sample=False):
    con = sqlite3.connect(path); con.executescript(SCHEMA); now = time.time()
    chw_pw, eye_pw = generate_password_hash("demo-chw-2026"), generate_password_hash("demo-eye-2026")
    con.execute("INSERT OR IGNORE INTO users(phone,name,role,pw,area,staff_id,approved,created) VALUES(?,?,?,?,?,?,1,?)", ("+254711000001", "Amina Wanjiru", "chw", chw_pw, "korogocho", "CHW-NBI-0417", now))
    for h, (phone, name) in DEMO_DOCTORS.items():
        con.execute("INSERT OR IGNORE INTO users(phone,name,role,pw,hospital_id,staff_id,approved,created) VALUES(?,?,?,?,?,?,1,?)", (phone, name, "clinician", eye_pw, h, "DEMO-" + h.upper(), now))
    con.commit()
    if sample and not con.execute("SELECT 1 FROM screenings WHERE is_sample=1").fetchone():
        add_sample_data(con)
    con.close()


def add_sample_data(con):
    """Fictional people and screenings, so the dashboards aren't empty in a demo. The app labels them as sample data."""
    from datetime import datetime, timedelta
    from server import TZ
    con.row_factory = sqlite3.Row
    rng = random.Random(7); now = datetime.now(TZ); chw = con.execute("SELECT id FROM users WHERE phone='+254711000001'").fetchone()["id"]; doc = con.execute("SELECT id FROM users WHERE phone='+254711000002'").fetchone()["id"]
    photos = sorted((HERE / "static" / "samples").glob("*.jpg")); pids = []
    for i, (name, by, area) in enumerate(SAMPLE_PATIENTS):
        phone = f"+2547220{i:05d}"
        uid = con.execute("INSERT INTO users(phone,name,role,pw,approved,created) VALUES(?,?,'member',?,1,?)", (phone, name, generate_password_hash("demo-patient-2026") if i == 0 else None, now.timestamp())).lastrowid
        pid = con.execute("INSERT INTO patients(name,birth_year,phone,village,area_id,sms_consent,created_by,created) VALUES(?,?,?,?,?,1,?,?)", (name, by, phone, AREA[area]["name"], area, chw, now.timestamp())).lastrowid
        con.execute("INSERT INTO links(patient_id,user_id,access,relation,added_by,created) VALUES(?,?,'owner','Self',?,?)", (pid, uid, chw, now.timestamp())); pids.append(pid)
    days = [0, 0, 0] + [min(now.day - 1, d) for d in (1, 2, 3, 4, 5)] + [8, 12, 15, 21, 27, 33, 41, 48, 56, 63, 72, 88, 95, 120, 140, 170, 200, 240]
    days = [d for d in days if (now - timedelta(days=d)).year == now.year]
    for n, d in enumerate(days):
        pid = pids[n % len(pids)]; at = min((now - timedelta(days=d)).replace(hour=9 + n % 7, minute=(n * 7) % 60), now - timedelta(minutes=5 * n + 1))
        outcome = rng.choices(["rescreen", "refer", "urgent"], [6, 3, 1])[0]
        if n == 0:
            outcome = "urgent"
        if n in (1, 12):  # n 12 gives the demo patient, Mary, a specialist result and an appointment
            outcome = "refer"
        waiting = n < 2 and outcome != "rescreen"
        score = {"rescreen": .08, "refer": .55, "urgent": .86}[outcome]
        eyes = json.dumps([dict(eye="right", level=outcome, message="Sample result.", p_referable=score, p_urgent=score / 2, p_quality=.9, retakes=0)])
        status = "no_referral" if outcome == "rescreen" else "waiting_review" if waiting else "confirmed"
        sid = con.execute("INSERT INTO screenings(client_ref,patient_id,chw_id,outcome,status,model,eyes,screened_at,created,hospital_id,referred_to,is_sample) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                          (f"sample-{n}", pid, chw, outcome, status, "Sample data", eyes, at.timestamp(), at.timestamp(), None if outcome == "rescreen" else "knh", None if outcome == "rescreen" else doc)).lastrowid
        con.execute("INSERT INTO screening_photos VALUES(?,?,?)", (sid, "right", photos[n % len(photos)].read_bytes()))
        if status == "confirmed":
            grade = "severe" if outcome == "urgent" else "moderate"; level = "urgent" if outcome == "urgent" else "refer"; reviewed = at + timedelta(days=1)
            rid = con.execute("INSERT INTO results(patient_id,right_grade,worst,level,note,clinician_id,created) VALUES(?,?,?,?,?,?,?)", (pid, grade, grade, level, "", doc, reviewed.timestamp())).lastrowid
            aid = con.execute("INSERT INTO appointments(patient_id,when_iso,location,clinician_id,result_id,created) VALUES(?,?,?,?,?,?)", (pid, (reviewed + timedelta(days=30)).strftime("%Y-%m-%dT09:00"), "Kenyatta National Hospital", doc, rid, reviewed.timestamp())).lastrowid
            con.execute("UPDATE screenings SET clinician_id=?,result_id=?,appointment_id=?,reviewed=? WHERE id=?", (doc, rid, aid, reviewed.timestamp(), sid))
    con.commit()


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ip = s.getsockname()[0]; s.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    os.environ.setdefault("AFRIEYE_SECRET", secret())
    production = os.environ.get("AFRIEYE_PRODUCTION") == "1"
    app = create_app({"AUTO_APPROVE": not production})
    if len(sys.argv) == 3 and sys.argv[1] == "approve":  # python app/run.py approve 0712345678
        import re
        seed(app.config["DB"]); phone = re.sub(r"[\s\-()]", "", sys.argv[2]); phone = "+254" + phone[1:] if phone.startswith("0") else phone
        con = sqlite3.connect(app.config["DB"]); n = con.execute("UPDATE users SET approved=1 WHERE phone=?", (phone,)).rowcount; con.commit(); con.close()
        print("Approved" if n else "No account found for", phone); sys.exit()
    port = int(os.environ.get("PORT", 8200))
    seed(app.config["DB"], sample=not production and os.environ.get("AFRIEYE_SAMPLE_DATA", "1") == "1")
    print("\nAfriEye is running. Research demo: don't enter real patient data.")
    print(f"  On this computer:  http://localhost:{port}")
    if lan_address():
        print(f"  On a phone on the same Wi-Fi:  http://{lan_address()}:{port}")
    print("\nDemo sign-ins (fictional people)")
    print("  CHW:            0711 000 001   password demo-chw-2026")
    print("  Eye specialist: 0711 000 002   password demo-eye-2026   (Kenyatta National Hospital)")
    print("  Patient:        0722 000 000   password demo-patient-2026")
    print("\nNew CHW and clinician accounts are approved automatically in this demo." if not production else "\nNew CHW and clinician accounts wait for: python app/run.py approve PHONE")
    print("SMS mode:", app.config["SMS"].name, "(test mode prints messages here and sends nothing)\n")
    app.run(host="0.0.0.0", port=port)
