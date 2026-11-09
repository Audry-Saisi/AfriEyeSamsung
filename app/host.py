"""Starts AfriEye on a web host such as PythonAnywhere (any WSGI server works the same way).

The host's WSGI file only needs:
    import os, sys
    os.environ["AFRIEYE_PUBLIC_URL"] = "https://YOURNAME.pythonanywhere.com"
    sys.path.insert(0, "/home/YOURNAME/afrieye/app")
    from host import application

Demo mode stays on (fictional sample data and demo logins), so never enter real patient data.
Set AFRIEYE_PRODUCTION=1 only for a real pilot with real SMS, email and approvals.
"""
import os, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run  # noqa: E402

os.environ.setdefault("AFRIEYE_SECRET", run.secret())
os.environ.setdefault("AFRIEYE_HTTPS", "1")  # the host serves it over https, so cookies are marked secure
production = os.environ.get("AFRIEYE_PRODUCTION") == "1"
# On the public demo, anyone can log in with the demo accounts, so their password, email and data can't be changed or deleted.
locked = () if production else ("+254711000", "+2547220", "+25472299")
application = run.create_app({"AUTO_APPROVE": not production, "USSD_KEY": os.environ.get("AFRIEYE_USSD_KEY") or ("" if production else "demo"), "LOCKED_PREFIXES": locked})
run.seed(application.config["DB"], sample=not production)
