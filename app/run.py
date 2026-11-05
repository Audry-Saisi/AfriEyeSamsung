"""Start the AfriEye app:  python app/run.py

The first run creates a demo CHW and eye specialist. SMS stays in test mode, so nothing is
really sent, until AT_USERNAME and AT_API_KEY are set for Africa's Talking.
"""
import os, pathlib, secrets, socket, sqlite3, sys, time

from werkzeug.security import generate_password_hash

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import SCHEMA, create_app  # noqa: E402

DEMO_USERS = [("+254711000001", "Amina Wanjiru", "chw", "demo-chw-2026"), ("+254711000002", "Dr. Otieno", "clinician", "demo-eye-2026")]


def secret():
    f = HERE / ".secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
    return f.read_text().strip()


def seed(path):
    con = sqlite3.connect(path); con.executescript(SCHEMA)
    for phone, name, role, pw in DEMO_USERS:
        con.execute("INSERT OR IGNORE INTO users(phone,name,role,pw,created) VALUES(?,?,?,?,?)", (phone, name, role, generate_password_hash(pw), time.time()))
    con.commit(); con.close()


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1)); ip = s.getsockname()[0]; s.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    os.environ.setdefault("AFRIEYE_SECRET", secret())
    port = int(os.environ.get("PORT", 8200))
    app = create_app()
    seed(app.config["DB"])
    print("\nAfriEye is running. Research demo: don't enter real patient data.")
    print(f"  On this computer:  http://localhost:{port}")
    if lan_address():
        print(f"  On a phone on the same Wi-Fi:  http://{lan_address()}:{port}")
    print("\nDemo sign-ins")
    print("  CHW:            0711 000 001   password demo-chw-2026")
    print("  Eye specialist: 0711 000 002   password demo-eye-2026")
    print("\nSMS mode:", app.config["SMS"].name, "(test mode prints messages here and sends nothing)\n")
    app.run(host="0.0.0.0", port=port)
