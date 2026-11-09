"""Draft translations of the most-used screen text, written by Claude on 9 Nov 2026.
DRAFTS ONLY: a native speaker must check every line before real patients use them.
Run  python app/translations/drafts_2026_11.py  to copy them into empty cells of translations.csv
(it never overwrites a cell your team has filled), then run build.py."""
import csv, pathlib

K = ["Log in", "Register", "Phone number", "Password", "Sign out", "Settings", "Back", "Language", "Menu", "My eye health",
     "Profile", "Appointments", "Reminders", "Emergency", "Reports", "Family", "Messages", "Read aloud", "Next appointment", "None booked.",
     "Your eye health", "No specialist result yet", "Eye clinic visit needed", "Urgent eye care needed", "No diabetic eye damage seen",
     "Early eye changes seen", "Waiting for result", "Right eye", "Left eye", "Book a consultation",
     "Ask a health worker to visit me", "Set reminder", "Open", "Go to a hospital today if you have:", "Emergency numbers: 999 or 112",
     "Nearest hospitals with eye care", "Eye doctor", "Community health worker", "Today", "Continue",
     "Close", "Hello. This is how AfriEye reads aloud.", "Patient", "Health history", "Rate your care"]

D = {
"so": ["Gal", "Is diiwaangeli", "Lambarka taleefanka", "Furaha sirta ah", "Ka bax", "Dejinta", "Dib u noqo", "Luqadda", "Liiska", "Caafimaadka indhahayga",
       "Xogtayda", "Ballamada", "Xasuusin", "Xaalad degdeg ah", "Warbixinno", "Qoyska", "Fariimo", "Kor u akhri", "Ballanta xigta", "Ballan lama qabsan.",
       "Caafimaadka indhahaaga", "Weli natiijo takhtarka indhaha ma jirto", "Waxaa loo baahan yahay booqasho rugta indhaha", "Daryeel degdeg ah oo indhaha ayaa loo baahan yahay", "Lama arag dhaawac indhaha oo sonkorowgu keenay",
       "Isbeddel hore oo indhaha ah ayaa la arkay", "Natiijada ayaa la sugayaa", "Isha midig", "Isha bidix", "Ballan la qabso takhtar",
       "Codso in shaqaale caafimaad uu i soo booqdo", "Deji xasuusin", "Fur", "Maanta isbitaal tag haddii aad qabto:", "Lambarrada degdegga: 999 ama 112",
       "Isbitaallada ugu dhow ee indhaha daaweeya", "Takhtarka indhaha", "Shaqaalaha caafimaadka bulshada", "Maanta", "Sii wad",
       "Xir", "Salaan. Sidan ayuu AfriEye kor ugu akhriyaa.", "Bukaan", "Taariikhda caafimaadka", "Qiimee daryeelka aad heshay"],
"luo": ["Donj", "Ndik nyingi", "Namba simu", "Wach mopondo", "Wuog", "Chenro", "Dog chien", "Dhok", "Menu", "Ngima mar wenga",
        "Weche ma kuoma", "Romo gi laktar", "Weche mag paro", "Masira", "Ripot", "Joodu", "Ote", "Som matek", "Romo machielo", "Onge romo mondiki.",
        "Ngima mar wangi", "Laktar mar wang' pok ochiwo duoko", "Idwaro dhi e klinik mar wang'", "Idwaro thieth mar wang' piyo piyo", "Ok oneno hinyruok mar wang' ma sukari okelo",
        "Oneno lokruok matin e wang'", "Rito duoko", "Wang' korachwich", "Wang' koracham", "Ndik romo gi laktar",
        "Kwa jatich mar ngima mondo olimba", "Ket paro", "Yaw", "Dhi e osiptal kawuono ka in gi:", "Namba mag masira: 999 kata 112",
        "Osiptal machiegni ma thiedho wang'", "Laktar mar wang'", "Jatich mar ngima e gweng'", "Kawuono", "Dhi nyime",
        "Lor", "Misawa. Ma e kaka AfriEye somo matek.", "Jatuo", "Weche mag ngima mokalo", "Pim thieth ma iyudo"],
"ki": ["Ingira", "Ĩyandĩkithie", "Namba ya thimũ", "Ciugo cia hitho", "Uma", "Mĩbangĩre", "Cooka", "Rũthiomi", "Menyu", "Ũgima wa maitho makwa",
       "Ũhoro wakwa", "Mĩcemanio na ndagĩtarĩ", "Kũririkanio", "Ũgwati wa narua", "Ripoti", "Andũ a mũciĩ", "Ndũmĩrĩri", "Thoma na mũgambo", "Mũcemanio ũrĩa ũrooka", "Gũtirĩ mũcemanio mwandĩke.",
       "Ũgima wa maitho maku", "Ndagĩtarĩ wa maitho ndanaheana macokio", "Nĩwabatara gũthiĩ kĩrĩnĩkĩ ya maitho", "Nĩwabatara ũrigitani wa maitho narua", "Gũtirĩ ũgwati wa maitho wa mũrimũ wa cukari wonekete",
       "Kwonekete ũgarũrũku mũnini wa maitho", "Kweterera macokio", "Riitho rĩa ũrĩo", "Riitho rĩa ũmotho", "Ĩyandĩkithie kuonana na ndagĩtarĩ",
       "Ũria mũteithia wa ũgima ooke kũnyona mũciĩ", "Ĩkĩra kĩririkania", "Hingũra", "Thiĩ thibitarĩ ũmũthĩ angĩkorwo ũrĩ na:", "Namba cia ũgwati: 999 kana 112",
       "Thibitarĩ cia hakuhĩ irĩ na ũrigitani wa maitho", "Ndagĩtarĩ wa maitho", "Mũteithia wa ũgima wa kĩrĩndĩ", "Ũmũthĩ", "Thiĩ na mbere",
       "Hinga", "Wĩmwega. Ũũ nĩguo AfriEye ĩthomaga na mũgambo.", "Mũrwaru", "Ũhoro wa ũgima wa tene", "Thima ũteithio waku"],
"kam": ["Lika", "Ĩyandĩkithye", "Namba ya simu", "Ndeto ya kĩmbithĩ", "Uma", "Mĩvango", "Syoka", "Ũthyomo", "Menyu", "Ũima wa metho makwa",
        "Ũvoo wakwa", "Mĩtũngano na ndakitali", "Kũlilikanw'a", "Ũthũku wa mĩtũkĩ", "Livoti", "Andũ ma mũsyĩ", "Ndũmĩlĩlo", "Soma kwa wasya", "Mũtũngano ũla ũũkĩte", "Vai mũtũngano wĩ wandĩke.",
        "Ũima wa metho maku", "Ndakitali wa metho ndanamba kũnenga matokeo", "Nũkwenda kũthi kilinikinĩ ya metho", "Nũkwenda ũiito wa metho mĩtũkĩ", "Vai ũthũku wa metho wa ũwau wa sukali wonekete",
        "Kwonekete kũalyuka kũnini kwa metho", "Kweteela matokeo", "Ĩtho ya ũlĩo", "Ĩtho ya ũmotho", "Ĩyandĩkithye kwona ndakitali",
        "Kũlya mũthukumi wa ũima ũka kũnyona mũsyĩ", "Ĩkĩa kĩlilikanyo", "Vingũa", "Thi sivitalĩ ũmũnthĩ ethĩwa wĩ na:", "Namba sya ũthũku: 999 kana 112",
        "Sivitalĩ sya vakuvĩ syĩ na ũiito wa metho", "Ndakitali wa metho", "Mũthukumi wa ũima wa kĩsio", "Ũmũnthĩ", "Endeea",
        "Vinga", "Wĩ mwĩanĩe. Ũũ nĩ ũndũ AfriEye ĩsomaa kwa wasya.", "Mũwau", "Ũvoo wa ũima wa tene", "Thima ũtethyo waku"],
}

if __name__ == "__main__":
    p = pathlib.Path(__file__).resolve().parent / "translations.csv"
    rows = list(csv.reader(open(p, encoding="utf-8"))); head = rows[0]
    col = {h.split(" ")[0]: i for i, h in enumerate(head)}; idx = {r[0]: r for r in rows[1:]}
    added = 0
    for lang, vals in D.items():
        assert len(vals) == len(K), lang
        for k, v in zip(K, vals):
            r = idx.get(k)
            if r is None:
                r = [k] + [""] * (len(head) - 1); rows.append(r); idx[k] = r
            while len(r) < len(head): r.append("")
            if not r[col[lang]].strip(): r[col[lang]] = v; added += 1
    csv.writer(open(p, "w", newline="", encoding="utf-8")).writerows(rows)
    print("added", added)
