/* Stand-in server for the hosted demo. It keeps everything in this page's memory and follows the same rules as app/server.py. Nothing is saved or sent. */
(function () {
  const db = { screenings: [], patients: [], sms: [], results: [], next: 1 };
  const PEOPLE = { chw: { id: 1, name: "Amina Wanjiru", role: "chw" }, clinician: { id: 2, name: "Dr. Otieno", role: "clinician" } };
  const AI = { urgent: "AI first result: possible severe DR. Urgent referral.", refer: "AI first result: possible DR that needs an eye clinic.", rescreen: "AI first result: no referable DR found. Check again in 12 months." };
  const STATUS = { no_referral: "No referral needed", waiting_review: "Waiting for the eye specialist", confirmed: "Appointment confirmed", no_visit: "Specialist says no visit needed" };
  const LEVELS = { none: ["ok", "No diabetic eye damage seen"], mild: ["watch", "Early eye changes seen. Keep regular checks"], moderate: ["refer", "Eye changes seen. Please attend the eye clinic"], severe: ["urgent", "Serious eye changes seen. Please attend urgently"], proliferative: ["urgent", "Serious eye changes seen. Please attend urgently"], ungradable: ["refer", "Photo could not be read. An eye exam is needed"] };
  const LABEL = { ok: ["No diabetic eye damage seen", "Keep your regular checks and diabetes care."], watch: ["Early eye changes seen", "Keep your regular checks. Your eye clinic will tell you when to return."], refer: ["Eye clinic visit needed", "Please attend your appointment so that a specialist can examine your eyes."], urgent: ["Urgent eye care needed", "Please attend urgently. Do not wait for symptoms."] };
  const ORDER = ["none", "mild", "ungradable", "moderate", "severe", "proliferative"];
  let me = null;
  const fail = (status, text) => { const e = new Error(text); e.status = status; throw e; };
  const now = (t) => new Date(t).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }).replace(",", ",");
  const whenText = (iso) => new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).replace(",", "");
  function phone(raw) {
    let p = String(raw || "").replace(/[\s\-()]/g, "");
    if (/^0[17]\d{8}$/.test(p)) p = "+254" + p.slice(1); else if (/^254[17]\d{8}$/.test(p)) p = "+" + p;
    if (!/^\+254[17]\d{8}$/.test(p)) fail(400, "Enter a Kenyan mobile number, such as 0712 345 678.");
    return p;
  }
  const initials = (n) => n.split(/\s+/).slice(0, 3).map((x) => x[0].toUpperCase()).join(".") + ".";
  const compose = (p, b, w) => `AfriEye: Eye result for ${initials(p.name)}: ${LEVELS[w][1]}. Appointment: ${whenText(b.date + "T" + b.time)}, ${b.location}. https://afrieye.example Help: 0700000000`;
  const parts = (t) => (t.length <= 160 ? 1 : Math.ceil(t.length / 153));
  const worst = (b) => [b.left, b.right].filter(Boolean).sort((x, y) => ORDER.indexOf(y) - ORDER.indexOf(x))[0];
  function view(s) {
    const p = db.patients.find((x) => x.id === s.patient_id);
    return { ...s, ai_text: AI[s.outcome], status_text: STATUS[s.status], screened: now(s.at), patient: { id: p.id, name: p.name, birth_year: p.birth_year, village: p.village, sms_consent: p.sms_consent }, chw: { name: PEOPLE.chw.name, phone: "+254711000001" } };
  }
  function checkGrades(b) { if (!b.left && !b.right) fail(400, "Choose your result for at least one eye."); }
  function checkWhen(b) {
    if (!b.date || !b.time) fail(400, "Choose an appointment date and time.");
    if (new Date(b.date + "T" + b.time) < new Date()) fail(400, "Choose a date and time in the future.");
    if (!String(b.location || "").trim()) fail(400, "Enter the clinic location.");
  }
  const targets = (p) => [p.phone, ...p.family.filter((f) => f.receive_sms && f.active).map((f) => f.phone)];
  function record(p, s, w, b) {
    const r = { id: db.next++, date: new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }), left: b.left, right: b.right, worst: w, level: LEVELS[w][0], label: LABEL[LEVELS[w][0]][0], advice: LABEL[LEVELS[w][0]][1], note: b.note, clinician: PEOPLE.clinician.name };
    p.results.unshift(r); s.specialist_result = r.label; s.reviewed = now(Date.now()); s.review_note = b.note;
  }
  async function api(method, path, b) {
    let m;
    if (path === "/api/me") return me || { signed_in: false };
    if (path === "/api/auth/logout") { me = null; return { ok: true }; }
    if (path === "/api/chw/screenings" && method === "POST") {
      if (!b.screening_consent) fail(400, "Confirm that the patient agreed to this screening.");
      const tel = phone(b.patient.phone), rank = ["rescreen", "refer", "urgent"];
      let p = db.patients.find((x) => x.phone === tel && x.name === b.patient.name);
      if (!p) { p = { id: db.next++, name: b.patient.name, phone: tel, birth_year: +b.patient.birth_year || null, village: b.patient.village, sms_consent: !!b.patient.sms_consent, family: [], results: [], appts: [], msgs: [] }; db.patients.push(p); }
      (b.family || []).forEach((f) => p.family.push({ id: db.next++, name: f.name, phone: phone(f.phone), relation: f.relation, receive_sms: 1, access: "view", joined: 0, active: true }));
      const outcome = b.eyes.map((e) => e.level).sort((x, y) => rank.indexOf(y) - rank.indexOf(x))[0];
      const s = { id: db.next++, patient_id: p.id, outcome, status: outcome === "rescreen" ? "no_referral" : "waiting_review", model: b.model, at: Date.now(), eyes: b.eyes.map((e) => ({ ...e, photo_url: e.photo })) };
      db.screenings.push(s); return view(s);
    }
    if (path === "/api/chw/screenings") return db.screenings.slice().reverse().map(view);
    if (path === "/api/clinic/referrals") {
      const wait = db.screenings.filter((s) => s.status === "waiting_review").sort((a, c) => (c.outcome === "urgent") - (a.outcome === "urgent") || a.at - c.at);
      return { waiting: wait.map(view), reviewed: db.screenings.filter((s) => s.status === "confirmed" || s.status === "no_visit").reverse().map(view) };
    }
    if ((m = path.match(/^\/api\/clinic\/referrals\/(\d+)\/(preview|confirm|no-visit)$/))) {
      const s = db.screenings.find((x) => x.id === +m[1]), p = db.patients.find((x) => x.id === s.patient_id);
      if (s.status !== "waiting_review") fail(409, "This referral has already been reviewed.");
      if (m[2] === "no-visit") {
        checkGrades(b); if (!String(b.note || "").trim()) fail(400, "Enter the reason for the health worker.");
        record(p, s, worst(b), b); s.status = "no_visit"; return { screening: view(s) };
      }
      checkWhen(b); checkGrades(b); const w = worst(b), text = compose(p, b, w);
      if (m[2] === "preview") return { body: text, parts: parts(text), to: p.sms_consent ? targets(p) : [], consent: p.sms_consent };
      record(p, s, w, b); s.status = "confirmed"; s.appointment = { when_text: whenText(b.date + "T" + b.time), location: b.location }; p.next = { ...s.appointment, when: b.date + "T" + b.time };
      const sent = p.sms_consent ? targets(p).map((to) => { const x = { id: db.next++, to_phone: to, body: text, name: p.name, status: "not sent (test mode)", parts: parts(text), when: now(Date.now()), ok: true }; db.sms.unshift(x); return x; }) : [];
      if (p.sms_consent) p.msgs = sent.filter((x) => x.to_phone === p.phone).map((x) => ({ body: x.body, status: x.status, when: x.when })).concat(p.msgs);
      db.latest = p.id; return { screening: view(s), sms: sent, consent: p.sms_consent };
    }
    if (path === "/api/clinic/sms") return db.sms;
    if ((m = path.match(/^\/api\/patients\/(\d+)$/))) {
      const p = db.patients.find((x) => x.id === +m[1]);
      return { access: "owner", relation: "Self", patient: { id: p.id, name: p.name, birth_year: p.birth_year, sms_consent: p.sms_consent }, status: p.results[0] || null, next_appointment: p.next || null, results: p.results, appointments: [], messages: p.msgs,
        screenings: db.screenings.filter((s) => s.patient_id === p.id).reverse().map((s) => ({ date: now(s.at), text: AI[s.outcome], status: STATUS[s.status] })), family: p.family.filter((f) => f.active) };
    }
    if ((m = path.match(/^\/api\/patients\/(\d+)\/family$/))) {
      const p = db.patients.find((x) => x.id === +m[1]), tel = phone(b.phone);
      if (tel === p.phone) fail(400, "That number already belongs to the patient.");
      if (p.family.find((f) => f.phone === tel && f.active)) fail(409, "This person already has access.");
      p.family.push({ id: db.next++, name: b.name, phone: tel, relation: b.relation, receive_sms: b.receive_sms ? 1 : 0, access: "view", joined: 0, active: true }); return { ok: true };
    }
    if ((m = path.match(/^\/api\/patients\/(\d+)\/family\/(\d+)(\/sms)?$/))) {
      const f = db.patients.find((x) => x.id === +m[1]).family.find((x) => x.id === +m[2]);
      if (m[3]) f.receive_sms = b.receive_sms ? 1 : 0; else f.active = false; return { ok: true };
    }
    if ((m = path.match(/^\/api\/patients\/(\d+)\/sms-consent$/))) { db.patients.find((x) => x.id === +m[1]).sms_consent = !!b.consent; return { ok: true }; }
    fail(404, "Not available in this demo.");
  }
  window.AFRIEYE_DEMO = {
    api,
    quick: [{ who: "chw", label: "1. Community health worker: scan a patient", primary: true }, { who: "clinician", label: "2. Eye specialist: review the referral" }, { who: "member", label: "3. Patient or family: see the result" }],
    async quickLogin(who) {
      if (who === "member") {
        const p = db.patients.find((x) => x.id === db.latest);
        if (!p) fail(400, "No patient record yet. Send a referral as the CHW, then confirm it as the eye specialist.");
        me = { signed_in: true, id: 9, name: p.name, role: "member", csrf: "demo", sms_mode: "test", patients: [{ id: p.id, name: p.name, access: "owner", relation: "Self" }] };
      } else me = { signed_in: true, ...PEOPLE[who], csrf: "demo", sms_mode: "test", patients: [] };
      return me;
    },
  };
})();
