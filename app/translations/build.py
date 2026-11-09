"""Builds the app's translation file from translations.csv:  python app/translations/build.py

translations.csv has one row per piece of text: the English in the first column, then one column
per language. Fill in a column, run this script, and restart the app. Empty cells show English.
Keep anything in curly brackets, such as {n} or {name}, exactly as it is: the app puts numbers
and names there.
"""
import csv, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
LANGS = ["sw", "ki", "luy", "luo", "kln", "kam", "mas", "guz", "mer", "tuv", "so", "nyf"]
NAMES = {"sw": "Kiswahili", "ki": "Gikuyu (Kikuyu)", "luy": "Luluhya (Luhya)", "luo": "Dholuo (Luo)", "kln": "Kalenjin", "kam": "Kikamba (Kamba)", "mas": "Maa (Maasai)",
         "guz": "Ekegusii (Kisii)", "mer": "Kimeru (Meru)", "tuv": "Turkana", "so": "Somali", "nyf": "Mijikenda"}


def make_csv():
    """Creates translations.csv from the Kiswahili draft, with empty columns for the other languages."""
    sys.path.insert(0, str(HERE)); from sw_draft import SW
    with open(HERE / "translations.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["english"] + [f"{c} {NAMES[c]}" for c in LANGS])
        for en, sw in SW.items():
            w.writerow([en, sw] + [""] * (len(LANGS) - 1))


def build():
    out = {c: {} for c in LANGS}
    with open(HERE / "translations.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    head = [h.split()[0] for h in rows[0][1:]]
    for r in rows[1:]:
        for code, text in zip(head, r[1:]):
            if text.strip() and code in out:
                out[code][r[0]] = text.strip()
    target = HERE.parent / "static" / "translations.js"
    target.write_text("window.AFRIEYE_T = " + json.dumps(out, ensure_ascii=False, indent=0) + ";\n", encoding="utf-8")
    print("Saved", target, {c: len(v) for c, v in out.items()})


if __name__ == "__main__":
    if not (HERE / "translations.csv").exists() or "--from-draft" in sys.argv:
        make_csv()
    build()
