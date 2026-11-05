"""SMS wording and delivery. Wording stays in GSM-7 characters so each part holds 160 characters."""
import json, os, secrets, urllib.error, urllib.parse, urllib.request
from datetime import datetime

GRADES = {
    "none": ("ok", "No diabetic eye damage seen"),
    "mild": ("watch", "Early eye changes seen. Keep regular checks"),
    "moderate": ("refer", "Eye changes seen. Please attend the eye clinic"),
    "severe": ("urgent", "Serious eye changes seen. Please attend urgently"),
    "proliferative": ("urgent", "Serious eye changes seen. Please attend urgently"),
    "ungradable": ("refer", "Photo could not be read. An eye exam is needed"),
}
ORDER = ["none", "mild", "ungradable", "moderate", "severe", "proliferative"]
GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ!\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")


def worst(left, right):
    return max([g for g in (left, right) if g], key=ORDER.index)


def initials(name):
    return ".".join(p[0].upper() for p in name.split()[:3]) + "."


def segments(text):
    """How many SMS parts a message costs."""
    gsm = all(c in GSM7 for c in text)
    single, multi = (160, 153) if gsm else (70, 67)
    n = len(text)
    return 1 if n <= single else -(-n // multi)


def when_text(iso):
    return datetime.fromisoformat(iso).strftime("%a %d %b, %H:%M")


def compose(kind, name, when_iso, location, grade, url, clinic_phone, detail="full"):
    """kind: 'booked' (result included), 'pending' (result not ready) or 'released' (result now ready)."""
    who, when = initials(name), when_text(when_iso)
    if kind == "pending":
        return f"AfriEye: Eye appointment for {who}: {when}, {location}. Result still under review. We will SMS you again. {url} Help: {clinic_phone}"
    if detail == "minimal":
        return f"AfriEye: The eye result for {who} is ready. Appointment: {when}, {location}. Log in to view: {url} Help: {clinic_phone}"
    return f"AfriEye: Eye result for {who}: {GRADES[grade][1]}. Appointment: {when}, {location}. {url} Help: {clinic_phone}"


class TestModeSMS:
    """Records messages without sending them. The default until a provider is configured."""
    name = "test"

    def send(self, to, body):
        print(f"[TEST SMS, not sent] to {to}: {body}", flush=True)  # lets you read sign-in codes while testing
        return {"ok": True, "status": "not sent (test mode)", "provider_id": "test-" + secrets.token_hex(6), "error": None}


class AfricasTalkingSMS:
    """Africa's Talking messaging API. Set username 'sandbox' to use its simulator."""
    name = "africastalking"
    OK_CODES = {100, 101, 102}

    def __init__(self, username, api_key, sender_id=None, url=None):
        self.username, self.api_key, self.sender_id = username, api_key, sender_id
        self.url = url or ("https://api.sandbox.africastalking.com/version1/messaging" if username == "sandbox" else "https://api.africastalking.com/version1/messaging")

    def send(self, to, body):
        form = {"username": self.username, "to": to, "message": body}
        if self.sender_id:
            form["from"] = self.sender_id
        req = urllib.request.Request(self.url, urllib.parse.urlencode(form).encode(), {"apiKey": self.api_key, "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            rec = data["SMSMessageData"]["Recipients"][0]
            ok = int(rec["statusCode"]) in self.OK_CODES
            return {"ok": ok, "status": "sent to network" if ok else "failed", "provider_id": rec.get("messageId"), "error": None if ok else rec.get("status")}
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
            return {"ok": False, "status": "failed", "provider_id": None, "error": type(e).__name__}


def provider_from_env():
    user, key = os.environ.get("AT_USERNAME"), os.environ.get("AT_API_KEY")
    if user and key:
        return AfricasTalkingSMS(user, key, os.environ.get("AT_SENDER_ID"))
    return TestModeSMS()
