"""Packs the app into one HTML file that runs with no server:  python app/build_demo.py"""
import base64, json, pathlib

S = pathlib.Path(__file__).resolve().parent / "static"
read = lambda n: (S / n).read_text(encoding="utf-8")
samples = [dict(s, src="data:image/jpeg;base64," + base64.b64encode((S / "samples" / s["file"]).read_bytes()).decode()) for s in json.loads(read("samples.json"))]
html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AfriEye Demo</title><meta name="theme-color" content="#0b1f3f">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:wght@600;800&display=swap">
<style>{read("style.css")}</style></head>
<body><div id="app"><p style="padding:24px">Loading AfriEye…</p></div>
<script>{read("engine.js")}</script>
<script>{read("model.js")}</script>
<script>window.AFRIEYE_SAMPLES={json.dumps(samples)};</script>
<script>{read("demo.js")}</script>
<script>{read("app.js")}</script>
</body></html>
"""
out = S.parent / "dist"; out.mkdir(exist_ok=True); (out / "afrieye_demo.html").write_text(html, encoding="utf-8")
body = html.split("<body>")[1].split("</body>")[0]
fonts = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:wght@600;800&display=swap">'
(out / "afrieye_demo_hosted.html").write_text(f"""<title>AfriEye Demo</title><meta name="claude:viewport-fit-sides" content="cover">{fonts}<style>{read("style.css")}</style>{body}""", encoding="utf-8")
print("Saved", out / "afrieye_demo.html", round(len(html) / 1e6, 2), "MB")
