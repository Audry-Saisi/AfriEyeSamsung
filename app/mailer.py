"""Email for AfriEye. Test mode prints messages and sends nothing, until SMTP settings are given.

To send real email, set these before starting the app:
  AFRIEYE_SMTP_HOST      for example smtp.gmail.com
  AFRIEYE_SMTP_PORT      587 (default)
  AFRIEYE_SMTP_USER      the sending account
  AFRIEYE_SMTP_PASSWORD  for Gmail, an app password, not the normal password
  AFRIEYE_EMAIL_FROM     optional; defaults to the sending account
"""
import os, re, secrets, smtplib, ssl
from email.message import EmailMessage

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


class TestModeEmail:
    name = "test"

    def send(self, to, subject, body, attachment=None):
        print(f"[TEST EMAIL, not sent] to {to}: {subject}" + (f" (attached: {attachment[0]})" if attachment else ""), flush=True)
        return {"ok": True, "status": "not sent (test mode)", "error": None}


class SmtpEmail:
    name = "smtp"

    def __init__(self, host, port, user, password, sender):
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender

    def send(self, to, subject, body, attachment=None):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["Message-ID"] = f"<{secrets.token_hex(12)}@afrieye>"
        msg.set_content(body)
        if attachment:  # (file name, bytes, "application/pdf")
            main, sub = attachment[2].split("/")
            msg.add_attachment(attachment[1], maintype=main, subtype=sub, filename=attachment[0])
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(self.user, self.password)
                s.send_message(msg)
            return {"ok": True, "status": "sent", "error": None}
        except (smtplib.SMTPException, OSError) as e:
            return {"ok": False, "status": "failed", "error": str(e)[:200]}


def email_from_env():
    host, user, pw = os.environ.get("AFRIEYE_SMTP_HOST"), os.environ.get("AFRIEYE_SMTP_USER"), os.environ.get("AFRIEYE_SMTP_PASSWORD")
    if host and user and pw:
        return SmtpEmail(host, int(os.environ.get("AFRIEYE_SMTP_PORT", 587)), user, pw, os.environ.get("AFRIEYE_EMAIL_FROM", user))
    return TestModeEmail()
