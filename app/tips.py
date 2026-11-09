"""Short eye and diabetes tips: shown on the patient's dashboard, read aloud, and sent weekly by SMS to those who ask.
Each tip is in English and Kiswahili, and fits in one SMS."""
TIPS = [
    ("Diabetes can damage the eyes with no pain and no early signs. A yearly eye check finds it early.", "Kisukari kinaweza kuharibu macho bila maumivu wala dalili za mapema. Pima macho kila mwaka ili kugundua mapema."),
    ("Keep your blood sugar in the range your clinic gave you. Good control protects your eyes.", "Weka sukari ya damu katika kiwango ulichopewa na kliniki. Udhibiti mzuri hulinda macho yako."),
    ("High blood pressure makes diabetic eye damage worse. Check your pressure and take your medicine.", "Shinikizo la damu huongeza madhara ya kisukari machoni. Pima shinikizo na umeze dawa zako."),
    ("Take your diabetes medicine every day, even when you feel well.", "Meza dawa zako za kisukari kila siku, hata unapojisikia vizuri."),
    ("Eat more vegetables, beans and whole grains. Eat less sugar, white bread and fried food.", "Kula mboga, maharagwe na nafaka zisizokobolewa zaidi. Punguza sukari, mkate mweupe na vyakula vya kukaanga."),
    ("Walk for 30 minutes on most days. Moving helps control blood sugar.", "Tembea dakika 30 siku nyingi za wiki. Mazoezi husaidia kudhibiti sukari."),
    ("If your sight becomes blurred, or you see dark spots or flashes, see an eye clinic soon.", "Ukiona ukungu, madoa meusi au miale ya mwanga, nenda kliniki ya macho mapema."),
    ("Do not smoke. Smoking damages the small blood vessels in your eyes.", "Usivute sigara. Uvutaji huharibu mishipa midogo ya damu machoni."),
    ("Laser and eye injections can save sight when damage is found early. Attend every eye appointment.", "Matibabu ya leza na sindano za macho huokoa uwezo wa kuona yakianza mapema. Hudhuria kila miadi ya macho."),
    ("Bring your medicine list and clinic book to every visit.", "Beba orodha ya dawa zako na kitabu cha kliniki kila unapoenda."),
    ("Ask your family to remind you of eye appointments. You can add them in AfriEye.", "Waombe familia wakukumbushe miadi ya macho. Unaweza kuwaongeza kwenye AfriEye."),
    ("SHA can pay for diabetes and eye care. Ask your health worker to help you register.", "SHA inaweza kulipia matibabu ya kisukari na macho. Mwombe mhudumu wa afya akusaidie kujiandikisha."),
]


def tip_for_week(week):
    en, sw = TIPS[week % len(TIPS)]
    return dict(index=week % len(TIPS), en=en, sw=sw)
