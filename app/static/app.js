/* AfriEye front end: one page with a view for each role. No frameworks, so it can run offline on a phone. */
(function () {
  "use strict";
  const DEMO = window.AFRIEYE_DEMO || null;
  const E = window.AfriEngine;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const app = $("#app");
  const S = { me: null, tab: null, flow: null, msg: null, offline: false, patient: null, queue: null, selected: null, booking: {} };
  const tick = () => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 20)));

  // ---- Talking to the server
  async function api(method, path, body) {
    if (DEMO) return DEMO.api(method, path, body);
    let r;
    try {
      r = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-CSRF": (S.me && S.me.csrf) || "" }, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
    } catch (e) {
      const err = new Error("No connection. Check the internet and try again."); err.network = true; throw err;
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const err = new Error(data.error || "Something went wrong. Try again."); err.status = r.status; throw err; }
    return data;
  }

  // ---- A small store on the phone, for screenings waiting to be sent
  const Store = {
    memory: [],
    open() {
      return new Promise((res, rej) => {
        try {
          const q = indexedDB.open("afrieye", 1);
          q.onupgradeneeded = () => q.result.createObjectStore("outbox", { keyPath: "client_ref" });
          q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
        } catch (e) { rej(e); }
      });
    },
    async run(mode, fn) {
      const d = await this.open();
      return new Promise((res, rej) => { const t = d.transaction("outbox", mode), r = fn(t.objectStore("outbox")); t.oncomplete = () => res(r.result); t.onerror = () => rej(t.error); });
    },
    async all() { try { return await this.run("readonly", (s) => s.getAll()); } catch (e) { return this.memory; } },
    async put(x) { try { await this.run("readwrite", (s) => s.put(x)); } catch (e) { this.memory = this.memory.filter((m) => m.client_ref !== x.client_ref).concat(x); } },
    async del(ref) { try { await this.run("readwrite", (s) => s.delete(ref)); } catch (e) { /* the memory copy below is still removed */ } this.memory = this.memory.filter((m) => m.client_ref !== ref); },
  };
  function remember(me) { try { if (me) localStorage.setItem("afrieye.me", JSON.stringify(me)); else localStorage.removeItem("afrieye.me"); } catch (e) { /* offline start isn't available in this browser */ } }
  function remembered() { try { return JSON.parse(localStorage.getItem("afrieye.me") || "null"); } catch (e) { return null; } }

  // ---- The AI scan. It runs on this device and sends nothing anywhere.
  const Model = { name: "Small CNN", T: null, net: null, session: null, cfg: null };
  function mk(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h === undefined ? w : h; return c; }
  const ctx2 = (c) => c.getContext("2d", { willReadFrequently: true });

  async function loadModel() {
    const j = window.AFRIEYE_MODEL;
    Model.net = E.loadModel(j); Model.T = j.thresholds; Model.name = j.name || "Small CNN";
    if (DEMO) return;
    try { // An exported model from notebook 12 replaces the built-in one.
      const r = await fetch("/static/model/model_config.json");
      if (!r.ok) return;
      const cfg = await r.json();
      await new Promise((res, rej) => { const s = document.createElement("script"); s.src = "/static/vendor/ort.min.js"; s.onload = res; s.onerror = rej; document.head.appendChild(s); });
      window.ort.env.wasm.wasmPaths = "/static/vendor/";
      Model.session = await window.ort.InferenceSession.create("/static/model/" + cfg.file, { executionProviders: ["wasm"] });
      Model.cfg = cfg; Model.T = cfg.thresholds; Model.name = cfg.name;
    } catch (e) { S.msg = { kind: "warn", text: "The exported model could not load, so the app is using the built-in Small CNN." }; }
  }

  function cropBox(img) {
    const w = img.naturalWidth, h = img.naturalHeight, sc = 256 / Math.max(w, h), tw = Math.max(1, Math.round(w * sc)), th = Math.max(1, Math.round(h * sc));
    const c = mk(tw, th), x = ctx2(c); x.drawImage(img, 0, 0, tw, th);
    const d = x.getImageData(0, 0, tw, th).data; let a = tw, b = th, cc = -1, e = -1, n = 0;
    for (let y = 0; y < th; y++) for (let q = 0; q < tw; q++) { const i = (y * tw + q) * 4; if (Math.max(d[i], d[i + 1], d[i + 2]) > 18) { n++; if (q < a) a = q; if (q > cc) cc = q; if (y < b) b = y; if (y > e) e = y; } }
    let box = [0, 0, w, h];
    if (n >= 0.02 * tw * th) box = [Math.floor(a / sc), Math.floor(b / sc), Math.min(w, Math.floor((cc + 1) / sc)), Math.min(h, Math.floor((e + 1) / sc))];
    const cw = box[2] - box[0], ch = box[3] - box[1];
    return { img, box, cw, ch, side: Math.max(cw, ch) };
  }
  function drawCrop(q, size) {
    const c = mk(size), x = ctx2(c); x.fillStyle = "#000"; x.fillRect(0, 0, size, size); x.imageSmoothingEnabled = true; x.imageSmoothingQuality = "high";
    const f = size / q.side; x.drawImage(q.img, q.box[0], q.box[1], q.cw, q.ch, ((q.side - q.cw) / 2) * f, ((q.side - q.ch) / 2) * f, q.cw * f, q.ch * f); return c;
  }
  async function modelScores(q) {
    if (Model.session) {
      const s = Model.cfg.size, d = ctx2(drawCrop(q, s)).getImageData(0, 0, s, s).data, x = new Float32Array(3 * s * s);
      for (let c = 0; c < 3; c++) for (let i = 0; i < s * s; i++) x[c * s * s + i] = (d[i * 4 + c] / 255 - Model.cfg.mean[c]) / Model.cfg.std[c];
      const out = await Model.session.run({ [Model.session.inputNames[0]]: new window.ort.Tensor("float32", x, [1, 3, s, s]) });
      return { p: Array.from(out[Model.session.outputNames[0]].data), cam: null };
    }
    const n = Model.net.size, d = ctx2(drawCrop(q, n)).getImageData(0, 0, n, n).data, rgb = new Uint8Array(n * n * 3);
    for (let i = 0; i < n * n; i++) { rgb[i * 3] = d[i * 4]; rgb[i * 3 + 1] = d[i * 4 + 1]; rgb[i * 3 + 2] = d[i * 4 + 2]; }
    const o = E.forward(Model.net, E.toInput(E.localContrast(rgb, n), n));
    return { p: o.p, cam: o.cam, camSize: o.camSize };
  }
  async function scan(img, retakes) {
    const small = mk(128), sx = ctx2(small); sx.imageSmoothingQuality = "high"; sx.drawImage(img, 0, 0, 128, 128);
    const problem = E.photoProblem(E.photoChecks(sx.getImageData(0, 0, 128, 128).data));
    const q = cropBox(img), m = await modelScores(q);
    let t = E.triage(m.p, Model.T, retakes);
    if (problem) t = retakes < 2 ? { cat: "Retake photo", msg: problem, level: "retake" } : { cat: "Refer", msg: "Still can't be read after 2 retakes. Refer for an in-person eye exam.", level: "refer" };
    const crop = drawCrop(q, 512);
    return { p: m.p, cam: m.cam, camSize: m.camSize, tri: t, crop, photo: drawCrop(q, Math.min(1024, Math.max(512, q.side))).toDataURL("image/jpeg", 0.85) };
  }
  function paintEye(canvas, r, showFocus) {
    const x = canvas.getContext("2d"); x.clearRect(0, 0, 512, 512); x.drawImage(r.crop, 0, 0);
    if (!showFocus || !r.cam) return;
    const n = r.camSize, t = mk(n), tx = ctx2(t), id = tx.createImageData(n, n); let mx = 1e-6; for (const v of r.cam) if (v > mx) mx = v;
    for (let i = 0; i < n * n; i++) { const v = Math.max(0, r.cam[i]) / mx; id.data[i * 4] = 255; id.data[i * 4 + 1] = Math.round(230 * (1 - v)); id.data[i * 4 + 2] = 40; id.data[i * 4 + 3] = Math.round(210 * Math.pow(v, 1.3)); }
    tx.putImageData(id, 0, 0); x.imageSmoothingEnabled = true; x.imageSmoothingQuality = "high"; x.drawImage(t, 0, 0, 512, 512);
  }

  // ---- Page frame
  const ROLE = { chw: "Community health worker", clinician: "Eye specialist", member: "Patient and family" };
  const LEVEL = {
    urgent: { label: "Urgent referral", head: "Possible severe DR found", cls: "urgent" }, refer: { label: "Refer", head: "Possible DR found", cls: "refer" },
    rescreen: { label: "No referral", head: "No referable DR found", cls: "ok" }, retake: { label: "Retake photo", head: "This photo can't be read", cls: "watch" },
  };
  function frame(body) {
    const m = S.msg, signed = S.me;
    app.innerHTML = `<header class="band"><div class="band-in"><div class="brand"><h1>AfriEye</h1>${signed ? `<span>${esc(ROLE[signed.role])}: ${esc(signed.name)}</span>` : "<span>Diabetic retinopathy screening</span>"}</div>${signed ? `<button class="btn ghost" data-act="logout">Sign out</button>` : ""}</div></header>
    ${S.offline ? `<div class="strip">You're offline. Scanning still works, and screenings are saved on this phone until you reconnect.</div>` : ""}
    <main class="wrap">${m ? `<div class="note ${esc(m.kind)}" role="status">${esc(m.text)}</div>` : ""}${body}</main>
    <footer class="foot">Research demo, not a medical device. The AI gives a first result, and an eye specialist makes the decision.</footer>`;
  }
  function tabs(list) {
    return `<nav class="tabs" aria-label="Sections">${list.map(([k, t]) => `<button class="tab" data-tab="${k}" aria-current="${S.tab === k ? "page" : "false"}">${t}</button>`).join("")}</nav>`;
  }
  function say(kind, text) { S.msg = text ? { kind, text } : null; }
  const fmtPct = (v) => (v * 100).toFixed(0) + "%";

  // ---- Sign in
  function renderLogin(mode) {
    mode = mode || "login";
    const quick = DEMO ? `<section class="card"><h2>Try the demo</h2><p class="muted">Follow one patient from the village to the clinic. Everything stays in this page, no real SMS is sent, and refreshing clears it.</p><div class="stack top">${DEMO.quick.map((q) => `<button class="btn ${q.primary ? "primary" : ""}" data-act="quick" data-who="${q.who}">${esc(q.label)}</button>`).join("")}</div></section>` : "";
    const form = DEMO ? "" : `<section class="card"><h2>${mode === "login" ? "Sign in" : "Create your password"}</h2>
      ${mode === "login" ? "" : `<p class="muted">Enter the code we sent you by SMS.</p>`}
      <form id="auth" class="stack top" data-mode="${mode}">
        <label>Phone number<input name="phone" inputmode="tel" autocomplete="username" required placeholder="0712 345 678"></label>
        ${mode === "login" ? "" : `<label>Code from SMS<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>`}
        <label>${mode === "login" ? "Password" : "New password, at least 8 characters"}<input name="password" type="password" autocomplete="${mode === "login" ? "current-password" : "new-password"}" required></label>
        <button class="btn primary">${mode === "login" ? "Sign in" : "Save and continue"}</button>
      </form>
      <p class="top"><button class="link" data-act="auth-mode" data-mode="${mode === "login" ? "activate" : "login"}">${mode === "login" ? "First time, or forgot your password? Use an SMS code" : "Back to sign in"}</button></p>
      ${mode === "login" ? "" : `<p class="top"><button class="link" data-act="forgot">Send me a new code</button></p>`}
    </section>`;
    frame(`<div class="narrow">${quick}${form}</div>`);
  }

  // ---- Community health worker
  const newFlow = () => ({ consent: false, eyes: { right: { retakes: 0 }, left: { retakes: 0 } }, family: [], step: "scan", ref: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8), focus: {}, form: {}, done: null });
  const EYE = { right: "Right eye", left: "Left eye" };
  function worstLevel(f) {
    const rank = ["rescreen", "refer", "urgent"]; let w = null;
    for (const k of ["left", "right"]) { const r = f.eyes[k].res; if (r && rank.indexOf(r.tri.level) > (w === null ? -1 : rank.indexOf(w))) w = r.tri.level; }
    return w;
  }
  function eyeCard(f, k) {
    const e = f.eyes[k], r = e.res, lv = r && LEVEL[r.tri.level];
    const pick = `<div class="row"><label class="btn ${r ? "" : "primary"}" tabindex="0"><input type="file" accept="image/*" capture="environment" data-photo="${k}" hidden>${r ? "Take again" : "Take photo"}</label><label class="btn" tabindex="0"><input type="file" accept="image/*" data-photo="${k}" hidden>Upload photo</label></div>`;
    if (e.busy) return `<article class="card eye"><h3>${EYE[k]}</h3><div class="scanning" role="status"><span class="spin"></span>AI is scanning the photo…</div></article>`;
    if (!r) return `<article class="card eye"><h3>${EYE[k]}</h3><p class="muted">Take the photo through the retinal adapter, or upload one.</p>${e.error ? `<p class="bad">${esc(e.error)}</p>` : ""}${pick}</article>`;
    const isRetake = r.tri.level === "retake";
    return `<article class="card eye"><div class="eye-head"><h3>${EYE[k]}</h3><span class="pill ${lv.cls}">${esc(r.tri.cat)}</span></div>
      <canvas width="512" height="512" class="fundus" data-eye="${k}" role="img" aria-label="${EYE[k]} retinal photo"></canvas>
      ${r.cam ? `<label class="check small"><input type="checkbox" data-focus="${k}" ${f.focus[k] ? "checked" : ""}> Show where the AI looked</label>` : ""}
      <p>${esc(r.tri.msg)}</p>
      <dl class="scores"><div><dt>DR score</dt><dd>${fmtPct(r.p[1])}</dd></div><div><dt>Severe DR score</dt><dd>${fmtPct(r.p[2])}</dd></div><div><dt>Photo quality score</dt><dd>${fmtPct(r.p[4])}</dd></div></dl>
      ${isRetake ? `<p class="muted small">Retake ${e.retakes + 1} of 2. If it still can't be read after that, AfriEye refers the patient for an in-person exam.</p>` : ""}${pick}</article>`;
  }
  function samplePicker(f) {
    if (!(window.AFRIEYE_SAMPLES || []).length) return "";
    const open = f.samples ? "open" : "";
    return `<details class="card" ${open}><summary>No adapter here? Use a sample photo</summary><p class="muted small">Public test photos from the DeepDRiD dataset, which the model didn't train on. Tap <b>Right</b> or <b>Left</b> to scan one as that eye. The specialist grade is shown only so you can compare.</p>
      <div class="samples">${window.AFRIEYE_SAMPLES.map((s, i) => `<div class="sample"><img src="${esc(s.src)}" alt="Sample retina, grade ${s.grade}" loading="lazy"><span class="small">Grade ${s.grade}${s.good ? "" : ", poor photo"}</span><div class="row tight"><button class="btn mini" data-sample="${i}" data-eye="right">Right</button><button class="btn mini" data-sample="${i}" data-eye="left">Left</button></div></div>`).join("")}</div></details>`;
  }
  function nextStep(level) {
    if (level === "urgent") return ["Send the referral below now.", "Tell the patient that an eye specialist will review it, and that an SMS will confirm the appointment.", "If sight has suddenly got worse, tell them to go to the nearest eye clinic today."];
    if (level === "refer") return ["Send the referral below.", "Tell the patient that an eye specialist will review it, and that an SMS will confirm the appointment.", "Remind them to keep taking their diabetes treatment."];
    return ["Save this result below.", "Advise them to keep their blood sugar and blood pressure under control.", "Screen again in 12 months, or sooner if their sight changes."];
  }
  function renderScan(f) {
    const eyes = ["right", "left"], scanned = eyes.filter((k) => f.eyes[k].res), pending = scanned.some((k) => f.eyes[k].res.tri.level === "retake"), missing = eyes.filter((k) => !f.eyes[k].res);
    const ready = scanned.length && !pending && f.consent;
    return `<ol class="steps" aria-label="Steps"><li class="on">Scan</li><li>Next step</li><li>Patient details</li></ol>
      <section class="card"><h2>New screening</h2><label class="check top"><input type="checkbox" id="consent" ${f.consent ? "checked" : ""}> The patient agreed to have their eyes photographed and checked by AI.</label></section>
      ${f.consent ? `<div class="eyes">${eyeCard(f, "right")}${eyeCard(f, "left")}</div>${samplePicker(f)}` : ""}
      ${f.consent && scanned.length ? `<div class="actions">${pending ? `<p class="muted">Retake the photo marked above to continue.</p>` : ""}${ready ? `<button class="btn primary big" data-act="to-result">${missing.length ? `Continue without the ${EYE[missing[0]].toLowerCase()}` : "See the next step"}</button>` : ""}</div>` : ""}`;
  }
  function renderResult(f) {
    const lv = worstLevel(f), L = LEVEL[lv], form = f.form, refer = lv !== "rescreen", eyes = ["right", "left"].filter((k) => f.eyes[k].res);
    return `<ol class="steps" aria-label="Steps"><li class="was">Scan</li><li class="on">Next step</li><li class="on">Patient details</li></ol>
    <section class="result ${L.cls}"><p class="eyebrow">AI first result</p><h2>${esc(L.head)}</h2><p>${eyes.map((k) => `${EYE[k]}: ${esc(f.eyes[k].res.tri.cat)}`).join(" · ")}</p>
      <h3>What to do next</h3><ol class="todo">${nextStep(lv).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
      <p class="small">${refer ? "This isn't a diagnosis. The eye specialist decides." : "This isn't a full eye exam, and macular oedema wasn't checked. Scanned by " + esc(Model.name) + "."}</p></section>
    <form id="patient" class="card stack">
      <h2>Patient details</h2>
      <label>Full name<input name="name" required value="${esc(form.name)}" autocomplete="off"></label>
      <div class="two"><label>Birth year<input name="birth_year" inputmode="numeric" maxlength="4" value="${esc(form.birth_year)}"></label><label>Village<input name="village" value="${esc(form.village)}"></label></div>
      <label>Phone number<input name="phone" inputmode="tel" required placeholder="0712 345 678" value="${esc(form.phone)}"></label>
      <label class="check"><input type="checkbox" name="sms_consent" ${form.sms_consent !== false ? "checked" : ""}> The patient agreed to get their appointment and result by SMS.</label>
      ${refer ? `<fieldset><legend>Family members who also get the SMS</legend><p class="muted small">Only add people the patient chose. They'll see this person's eye result.</p>
        <div id="fam">${f.family.map((m, i) => `<div class="fam"><label>Name<input data-fam="${i}" data-k="name" value="${esc(m.name)}"></label><label>Relationship<input data-fam="${i}" data-k="relation" value="${esc(m.relation)}" placeholder="Daughter"></label><label>Phone<input data-fam="${i}" data-k="phone" inputmode="tel" value="${esc(m.phone)}"></label><button type="button" class="link" data-act="fam-del" data-i="${i}">Remove</button></div>`).join("")}</div>
        <button type="button" class="btn" data-act="fam-add">Add a family member</button></fieldset>` : ""}
      <div class="row between"><button type="button" class="link" data-act="to-scan">Back to the photos</button><button class="btn primary big">${refer ? (lv === "urgent" ? "Send urgent referral" : "Send referral") : "Save result"}</button></div>
    </form>`;
  }
  function renderDone(f) {
    const d = f.done, refer = d.level !== "rescreen";
    return `<section class="result ${LEVEL[d.level].cls}"><p class="eyebrow">${d.queued ? "Saved on this phone" : "Done"}</p>
      <h2>${d.queued ? (refer ? "The referral will send when you're back online" : "The result will upload when you're back online") : refer ? "Referral sent to the eye specialist" : "Result saved"}</h2>
      <p>${refer ? "The SMS goes to the patient and their chosen family members after the specialist confirms the appointment." : "No SMS is sent. Tell the patient the result, and screen them again in 12 months."}</p></section>
      <div class="actions"><button class="btn primary big" data-act="new-screen">Screen the next patient</button><button class="btn" data-tab="patients">See my patients</button></div>`;
  }
  async function renderChw() {
    S.tab = S.tab || "screen";
    const box = await Store.all();
    const head = tabs([["screen", "New screening"], ["patients", "My patients"]]) + (box.length ? `<div class="note warn row between"><span>${box.length} screening${box.length > 1 ? "s" : ""} not sent yet.${S.syncError ? " " + esc(S.syncError) : ""}</span><button class="btn mini" data-act="sync">Send now</button></div>` : "");
    if (S.tab === "patients") {
      frame(head + `<div id="list" class="muted">Loading…</div>`);
      let rows = [];
      try { rows = await api("GET", "/api/chw/screenings"); } catch (e) { if (!e.network) say("bad", e.message); }
      const queued = box.map((b) => `<article class="card line"><div><b>${esc(b.patient.name)}</b><p class="small muted">Saved on this phone, not sent yet</p></div><span class="pill watch">Waiting for internet</span></article>`).join("");
      const list = rows.map((s) => `<article class="card line"><div><b>${esc(s.patient.name)}</b>${s.patient.village ? `<span class="muted"> · ${esc(s.patient.village)}</span>` : ""}<p class="small muted">${esc(s.screened)} · ${esc(s.ai_text)}</p>
        ${s.appointment ? `<p><b>Appointment:</b> ${esc(s.appointment.when_text)}, ${esc(s.appointment.location)}</p>` : ""}${s.specialist_result ? `<p><b>Specialist:</b> ${esc(s.specialist_result)}</p>` : ""}${s.status === "no_visit" ? `<p><b>Tell the patient:</b> ${esc(s.review_note)}</p>` : ""}</div>
        <span class="pill ${s.status === "confirmed" ? "ok" : s.status === "waiting_review" ? "refer" : "watch"}">${esc(s.status_text)}</span></article>`).join("");
      const l = $("#list"); if (l) { l.className = "stack"; l.innerHTML = queued + list || `<p class="muted">No patients yet. Screenings you send appear here, with the specialist's decision.</p>`; }
      return;
    }
    S.flow = S.flow || newFlow();
    const f = S.flow;
    frame(head + (f.step === "scan" ? renderScan(f) : f.step === "result" ? renderResult(f) : renderDone(f)));
    $$("canvas.fundus").forEach((c) => paintEye(c, f.eyes[c.dataset.eye].res, f.focus[c.dataset.eye]));
  }
  async function takePhoto(k, file, src) {
    const f = S.flow, e = f.eyes[k], before = e.res;
    if (before && before.tri.level === "retake") e.retakes += 1;
    e.busy = true; e.error = null; say(); renderChw(); await tick();
    try {
      const img = new Image();
      await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("That file couldn't be opened as a photo.")); img.src = src || URL.createObjectURL(file); });
      e.res = await scan(img, e.retakes);
      e.src = null;
    } catch (err) { e.error = err.message || "The scan failed. Try again."; e.res = null; }
    e.busy = false; renderChw();
  }
  function payload(f, values) {
    const eyes = ["right", "left"].filter((k) => f.eyes[k].res).map((k) => { const e = f.eyes[k], r = e.res; return { eye: k, level: r.tri.level, message: r.tri.msg, p_referable: r.p[1], p_urgent: r.p[2], p_quality: r.p[4], retakes: e.retakes, photo: r.photo }; });
    return { client_ref: f.ref, screening_consent: f.consent, screened_at: Date.now() / 1000, model: Model.name, eyes, patient: { name: values.name, birth_year: values.birth_year, village: values.village, phone: values.phone, sms_consent: values.sms_consent }, family: worstLevel(f) === "rescreen" ? [] : f.family.map((m) => ({ ...m, receive_sms: true })) };
  }
  const phoneOk = (p) => /^(0[17]\d{8}|(\+?254)[17]\d{8})$/.test(String(p || "").replace(/[\s\-()]/g, ""));
  async function submitPatient(form) {
    const f = S.flow, fd = new FormData(form), values = { name: fd.get("name").trim(), birth_year: fd.get("birth_year").trim(), village: fd.get("village").trim(), phone: fd.get("phone").trim(), sms_consent: fd.get("sms_consent") === "on" };
    f.form = values;
    if (!phoneOk(values.phone)) { say("bad", "Enter a Kenyan mobile number, such as 0712 345 678."); return renderChw(); }
    for (const m of f.family) if (!m.name || !m.relation || !phoneOk(m.phone)) { say("bad", "Complete each family member's name, relationship and phone number, or remove them."); return renderChw(); }
    const level = worstLevel(f), body = payload(f, values); say();
    try { await api("POST", "/api/chw/screenings", body); f.done = { level, queued: false }; }
    catch (e) {
      if (e.network || e.status === 401 || e.status === 403) {
        await Store.put(body); f.done = { level, queued: true };
        if (!e.network) S.syncError = "Sign in again to send it.";
      } else { say("bad", e.message); return renderChw(); }
    }
    f.step = "done"; window.scrollTo(0, 0); renderChw();
  }
  async function sync() {
    const box = await Store.all(); S.syncError = null;
    for (const b of box) {
      try { await api("POST", "/api/chw/screenings", b); await Store.del(b.client_ref); }
      catch (e) { S.syncError = e.network ? "Still no connection." : e.status === 401 || e.status === 403 ? "Sign in again to send it." : `${b.patient.name}: ${e.message}`; break; }
    }
    if (S.me && S.me.role === "chw") renderChw();
  }

  // ---- Eye specialist
  const GRADE_OPTIONS = [["", "Choose"], ["none", "No DR"], ["mild", "Mild"], ["moderate", "Moderate"], ["severe", "Severe"], ["proliferative", "Proliferative"], ["ungradable", "Can't be graded"]];
  const defaultDate = () => { const d = new Date(Date.now() + 7 * 864e5); return new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10); };
  const age = (y) => (y ? new Date().getFullYear() - y + " yrs" : "");
  const photoUrl = (s, e) => e.photo_url || `/api/screenings/${s.id}/photo/${e.eye}`;
  async function renderClinic() {
    S.tab = S.tab || "referrals";
    const head = tabs([["referrals", "Referrals"], ["reviewed", "Reviewed"], ["sms", "SMS log"]]);
    frame(head + `<div id="pane" class="muted">Loading…</div>`);
    const pane = $("#pane");
    try {
      if (S.tab === "sms") {
        const rows = await api("GET", "/api/clinic/sms");
        pane.className = "stack"; pane.innerHTML = `${S.me.sms_mode === "test" ? `<div class="note warn">Test mode: messages are recorded here, and no real SMS is sent. Sign-in codes appear here only in test mode.</div>` : ""}${rows.map((m) => `<article class="card line"><div><b>${esc(m.to_phone)}</b>${m.name ? `<span class="muted"> · ${esc(m.name)}</span>` : ""}<p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)} · ${m.parts} part${m.parts > 1 ? "s" : ""}</p></div><span class="pill ${m.status === "failed" ? "urgent" : "watch"}">${esc(m.status)}</span>${m.status === "failed" ? `<button class="btn mini" data-retry="${m.id}">Send again</button>` : ""}</article>`).join("") || `<p class="muted">No messages yet.</p>`}`;
        return;
      }
      const q = await api("GET", "/api/clinic/referrals"); S.queue = q;
      if (S.tab === "reviewed") {
        pane.className = "stack"; pane.innerHTML = q.reviewed.map((s) => `<article class="card line"><div><b>${esc(s.patient.name)}</b><p class="small muted">${esc(s.reviewed)} · ${esc(s.specialist_result || "")}</p>${s.appointment ? `<p>${esc(s.appointment.when_text)}, ${esc(s.appointment.location)}</p>` : `<p>${esc(s.review_note)}</p>`}</div><span class="pill ${s.status === "confirmed" ? "ok" : "watch"}">${esc(s.status_text)}</span></article>`).join("") || `<p class="muted">Nothing reviewed yet.</p>`;
        return;
      }
      if (!q.waiting.find((s) => s.id === S.selected)) S.selected = q.waiting.length ? q.waiting[0].id : null;
      pane.className = "split";
      pane.innerHTML = `<aside class="queue"><h2>Waiting for review (${q.waiting.length})</h2>${q.waiting.map((s) => `<button class="qitem" data-pick="${s.id}" aria-current="${s.id === S.selected ? "true" : "false"}"><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span><b>${esc(s.patient.name)}</b><span class="small muted">${esc(s.patient.village || "")} ${esc(age(s.patient.birth_year))}</span><span class="small muted">${esc(s.screened)}</span></button>`).join("") || `<p class="muted">No referrals are waiting. They'll appear here when a CHW sends one.</p>`}</aside><div id="review">${S.selected ? "" : ""}</div>`;
      if (S.selected) renderReview();
    } catch (e) { pane.className = "note bad"; pane.textContent = e.message; }
  }
  function renderReview() {
    const s = S.queue.waiting.find((x) => x.id === S.selected), b = S.booking[s.id] || (S.booking[s.id] = { date: defaultDate(), time: "09:00", location: "Eye clinic", note: "", left: "", right: "" }), box = $("#review");
    const eyes = ["right", "left"], byEye = Object.fromEntries(s.eyes.map((e) => [e.eye, e]));
    box.innerHTML = `<section class="card"><div class="eye-head"><div><h2>${esc(s.patient.name)}</h2><p class="muted">${esc(age(s.patient.birth_year))}${s.patient.village ? " · " + esc(s.patient.village) : ""} · Referred by ${esc(s.chw.name)}, ${esc(s.chw.phone)}</p></div><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span></div>
      <p class="top">${esc(s.ai_text)} <span class="muted">This is the AI's first result, from ${esc(s.model || "the phone")}. Please grade the photos yourself.</span></p></section>
      <div class="eyes">${eyes.map((k) => { const e = byEye[k]; return e ? `<article class="card eye"><div class="eye-head"><h3>${EYE[k]}</h3><span class="pill ${LEVEL[e.level].cls}">${esc(LEVEL[e.level].label)}</span></div><img class="fundus" src="${esc(photoUrl(s, e))}" alt="${EYE[k]} retinal photo"><dl class="scores"><div><dt>DR score</dt><dd>${fmtPct(e.p_referable)}</dd></div><div><dt>Severe score</dt><dd>${fmtPct(e.p_urgent)}</dd></div><div><dt>Quality</dt><dd>${fmtPct(e.p_quality)}</dd></div></dl>${e.retakes ? `<p class="small muted">Retaken ${e.retakes} time${e.retakes > 1 ? "s" : ""}.</p>` : ""}<label>Your grade, ${EYE[k].toLowerCase()}<select data-book="${k}">${GRADE_OPTIONS.map(([v, t]) => `<option value="${v}" ${b[k] === v ? "selected" : ""}>${t}</option>`).join("")}</select></label></article>` : `<article class="card eye muted"><h3>${EYE[k]}</h3><p>Not scanned.</p></article>`; }).join("")}</div>
      <section class="card stack"><h2>Confirm the appointment</h2><div class="three"><label>Date<input type="date" data-book="date" value="${esc(b.date)}"></label><label>Time<input type="time" data-book="time" value="${esc(b.time)}"></label><label>Clinic<input data-book="location" maxlength="60" value="${esc(b.location)}"></label></div>
        <label>Note<textarea data-book="note" rows="2" maxlength="500" placeholder="Required if no visit is needed. The CHW sees this.">${esc(b.note)}</textarea></label>
        <div id="smsbox" class="smsbox" aria-live="polite"><p class="muted">Choose your grade to preview the SMS.</p></div>
        <div class="row between"><button class="btn" data-act="no-visit">No appointment needed</button><button class="btn primary big" data-act="confirm">Confirm appointment and send SMS</button></div></section>`;
    previewSms();
  }
  async function previewSms() {
    const s = S.queue && S.queue.waiting.find((x) => x.id === S.selected), b = s && S.booking[s.id], box = $("#smsbox");
    if (!box || !b || !(b.left || b.right)) return;
    try {
      const p = await api("POST", `/api/clinic/referrals/${s.id}/preview`, b);
      box.innerHTML = p.consent ? `<p class="small muted">This SMS goes to ${p.to.map(esc).join(", ")} when you confirm:</p><p class="sms">${esc(p.body)}</p><p class="small muted">${p.parts} SMS part${p.parts > 1 ? "s" : ""}</p>` : `<p class="note warn">The patient didn't agree to SMS, so nothing is sent. The CHW sees the appointment and tells them.</p>`;
    } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  }

  // ---- Patient and family
  async function renderFamily(pid) {
    const list = S.me.patients || [];
    if (!list.length) { frame(`<section class="card narrow"><h2>No record yet</h2><p>Your record appears here after a health worker adds you, or a patient adds you as family.</p></section>`); return; }
    pid = pid || S.patient || list[0].id; S.patient = pid;
    frame(`<div id="rec" class="muted">Loading…</div>`);
    let d; try { d = await api("GET", `/api/patients/${pid}`); } catch (e) { $("#rec").className = "note bad"; $("#rec").textContent = e.message; return; }
    const st = d.status, next = d.next_appointment, owner = d.access === "owner";
    const chooser = list.length > 1 ? `<label class="narrow">Whose record?<select id="who">${list.map((p) => `<option value="${p.id}" ${p.id === pid ? "selected" : ""}>${esc(p.name)}${p.relation && p.relation !== "Self" ? ` (${esc(p.relation)})` : ""}</option>`).join("")}</select></label>` : "";
    const rec = $("#rec"); rec.className = "stack narrow";
    rec.innerHTML = `${chooser}<h2>${esc(d.patient.name)}</h2>
      ${st ? `<section class="result ${st.level === "urgent" ? "urgent" : st.level === "refer" ? "refer" : st.level === "ok" ? "ok" : "watch"}"><p class="eyebrow">Result from your eye specialist, ${esc(st.date)}</p><h2>${esc(st.label)}</h2><p>${esc(st.advice)}</p>${st.note ? `<p class="small">Specialist's note: ${esc(st.note)}</p>` : ""}</section>` : `<section class="card"><p>No specialist result yet.</p></section>`}
      <section class="card"><h3>Next appointment</h3>${next ? `<p class="big-text">${esc(next.when_text)}</p><p>${esc(next.location)}</p>` : `<p class="muted">None booked.</p>`}</section>
      ${d.screenings.length ? `<section class="card"><h3>Eye screenings</h3>${d.screenings.map((s) => `<div class="hist"><p>${esc(s.text)}</p><p class="small muted">${esc(s.date)} · ${esc(s.status)}</p></div>`).join("")}<p class="small muted top">The AI gives a first result. It isn't a diagnosis or a full eye exam.</p></section>` : ""}
      ${owner ? `<section class="card stack"><h3>Family who can see this record</h3>${(d.family || []).filter((f) => f.access !== "owner").map((f) => `<div class="fam-row"><div><b>${esc(f.name)}</b> <span class="muted">${esc(f.relation)}</span><p class="small muted">${esc(f.phone)}${f.joined ? "" : " · hasn't signed in yet"}</p></div><label class="check small"><input type="checkbox" data-famsms="${f.id}" ${f.receive_sms ? "checked" : ""}> Gets SMS</label><button class="link" data-famdel="${f.id}">Remove</button></div>`).join("") || `<p class="muted">No family members yet.</p>`}
        <form id="addfam" class="stack"><div class="three"><label>Name<input name="name" required></label><label>Relationship<input name="relation" required placeholder="Son"></label><label>Phone<input name="phone" inputmode="tel" required></label></div><label class="check"><input type="checkbox" name="receive_sms" checked> Also send them appointment SMS</label><button class="btn">Add family member</button></form>
        <label class="check top"><input type="checkbox" id="smsok" ${d.patient.sms_consent ? "checked" : ""}> Send appointment and result SMS for this record</label></section>` : `<p class="muted small">You can view this record because ${esc(d.patient.name.split(" ")[0])} added you as family.</p>`}
      ${d.messages.length ? `<section class="card"><h3>SMS sent</h3>${d.messages.map((m) => `<div class="hist"><p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)} · ${esc(m.status)}</p></div>`).join("")}</section>` : ""}`;
  }

  // ---- Routing
  async function render() {
    if (!S.me) return renderLogin(S.authMode);
    if (S.me.role === "chw") return renderChw();
    if (S.me.role === "clinician") return renderClinic();
    return renderFamily();
  }
  async function afterSignIn(me) { S.me = me; S.tab = null; S.flow = null; S.selected = null; S.patient = null; remember(me.role === "chw" ? me : null); say(); await render(); if (me.role === "chw") sync(); }

  // ---- Events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-act],[data-tab],[data-pick],[data-sample],[data-retry],[data-famdel]"); if (!t) return;
    if (t.dataset.tab) { S.tab = t.dataset.tab; say(); if (t.dataset.tab === "screen" && S.flow && S.flow.step === "done") S.flow = null; return render(); }
    if (t.dataset.pick) { S.selected = +t.dataset.pick; say(); return renderClinic(); }
    if (t.dataset.sample) { const s = window.AFRIEYE_SAMPLES[+t.dataset.sample]; S.flow.samples = true; return takePhoto(t.dataset.eye, null, s.src); }
    if (t.dataset.retry) { try { await api("POST", `/api/clinic/sms/${t.dataset.retry}/retry`, {}); } catch (e) { say("bad", e.message); } return renderClinic(); }
    if (t.dataset.famdel) { try { await api("DELETE", `/api/patients/${S.patient}/family/${t.dataset.famdel}`); say("ok", "Removed."); } catch (e) { say("bad", e.message); } return renderFamily(); }
    const act = t.dataset.act, f = S.flow;
    if (act === "logout") {
      const box = S.me.role === "chw" ? await Store.all() : [];
      if (box.length) { say("warn", `${box.length} screening${box.length > 1 ? "s are" : " is"} still waiting to send. Send ${box.length > 1 ? "them" : "it"} before signing out.`); return render(); }
      try { await api("POST", "/api/auth/logout", {}); } catch (e) { /* signing out locally is enough when offline */ }
      S.me = null; remember(null); say(); S.authMode = "login"; return render();
    }
    if (act === "quick") { try { await afterSignIn(await DEMO.quickLogin(t.dataset.who)); } catch (e) { say("warn", e.message); renderLogin(); } return; }
    if (act === "auth-mode") { S.authMode = t.dataset.mode; say(); return render(); }
    if (act === "forgot") { const phone = $("#auth [name=phone]").value; try { const r = await api("POST", "/api/auth/forgot", { phone }); say("ok", r.message); } catch (e) { say("bad", e.message); } return render(); }
    if (act === "to-result") { f.step = "result"; say(); window.scrollTo(0, 0); return renderChw(); }
    if (act === "to-scan") { keepForm(); f.step = "scan"; return renderChw(); }
    if (act === "new-screen") { S.flow = newFlow(); say(); return renderChw(); }
    if (act === "fam-add") { keepForm(); f.family.push({ name: "", relation: "", phone: "" }); return renderChw(); }
    if (act === "fam-del") { keepForm(); f.family.splice(+t.dataset.i, 1); return renderChw(); }
    if (act === "sync") return sync();
    if (act === "confirm" || act === "no-visit") {
      const s = S.queue.waiting.find((x) => x.id === S.selected), b = S.booking[s.id]; t.disabled = true;
      try {
        const r = await api("POST", `/api/clinic/referrals/${s.id}/${act === "confirm" ? "confirm" : "no-visit"}`, b);
        if (act === "confirm") {
          const n = (r.sms || []).length, failed = (r.sms || []).filter((x) => !x.ok).length;
          say(failed ? "bad" : "ok", !r.consent ? `Appointment confirmed for ${s.patient.name}. They didn't agree to SMS, so the CHW will tell them.` : failed ? `Appointment saved, but ${failed} of ${n} SMS failed. Open the SMS log to send again.` : `Appointment confirmed for ${s.patient.name}. SMS ${S.me.sms_mode === "test" ? "recorded in test mode" : "sent"} for ${n} ${n > 1 ? "people" : "person"}.`);
        } else say("ok", `No appointment for ${s.patient.name}. The CHW will see your note.`);
        delete S.booking[s.id]; S.selected = null; window.scrollTo(0, 0);
      } catch (e) { say("bad", e.message); window.scrollTo(0, 0); }
      return renderClinic();
    }
  });
  function keepForm() {
    const form = $("#patient"); if (!form) return;
    const fd = new FormData(form); S.flow.form = { name: fd.get("name"), birth_year: fd.get("birth_year"), village: fd.get("village"), phone: fd.get("phone"), sms_consent: fd.get("sms_consent") === "on" };
  }
  document.addEventListener("input", (ev) => {
    const t = ev.target;
    if (t.dataset.fam !== undefined) S.flow.family[+t.dataset.fam][t.dataset.k] = t.value;
  });
  document.addEventListener("change", async (ev) => {
    const t = ev.target;
    if (t.id === "consent") { S.flow.consent = t.checked; return renderChw(); }
    if (t.dataset.photo && t.files[0]) return takePhoto(t.dataset.photo, t.files[0]);
    if (t.dataset.focus) { S.flow.focus[t.dataset.focus] = t.checked; const c = $(`canvas[data-eye=${t.dataset.focus}]`); return paintEye(c, S.flow.eyes[t.dataset.focus].res, t.checked); }
    if (t.dataset.book) { S.booking[S.selected][t.dataset.book] = t.value; return previewSms(); }
    if (t.id === "who") { S.patient = +t.value; return renderFamily(); }
    if (t.dataset.famsms) { try { await api("POST", `/api/patients/${S.patient}/family/${t.dataset.famsms}/sms`, { receive_sms: t.checked }); } catch (e) { say("bad", e.message); renderFamily(); } return; }
    if (t.id === "smsok") { try { await api("POST", `/api/patients/${S.patient}/sms-consent`, { consent: t.checked }); } catch (e) { say("bad", e.message); renderFamily(); } }
  });
  document.addEventListener("keydown", (ev) => { if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("label.btn")) { ev.preventDefault(); ev.target.querySelector("input").click(); } });
  document.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const form = ev.target;
    if (form.id === "auth") {
      const fd = Object.fromEntries(new FormData(form));
      try { await afterSignIn(await api("POST", form.dataset.mode === "login" ? "/api/auth/login" : "/api/auth/activate", fd)); } catch (e) { say("bad", e.message); render(); }
    } else if (form.id === "patient") {
      form.querySelector(".btn.primary").disabled = true; await submitPatient(form);
    } else if (form.id === "addfam") {
      const fd = new FormData(form);
      try { await api("POST", `/api/patients/${S.patient}/family`, { name: fd.get("name"), relation: fd.get("relation"), phone: fd.get("phone"), receive_sms: fd.get("receive_sms") === "on" }); say("ok", "Added. We sent them an SMS with how to sign in."); } catch (e) { say("bad", e.message); }
      renderFamily();
    }
  });
  function setOffline() { S.offline = !navigator.onLine; if (S.me && S.me.role === "chw") { if (!S.offline) sync(); else render(); } }
  window.addEventListener("online", setOffline); window.addEventListener("offline", setOffline);

  // ---- Start
  async function start() {
    S.offline = !navigator.onLine;
    await loadModel();
    if (!DEMO && !window.AFRIEYE_SAMPLES) {
      try { const list = await (await fetch("/static/samples.json")).json(); window.AFRIEYE_SAMPLES = list.map((s) => ({ ...s, src: "/static/samples/" + s.file })); } catch (e) { window.AFRIEYE_SAMPLES = []; }
    }
    try {
      const me = await api("GET", "/api/me");
      if (me.signed_in) { S.me = me; remember(me.role === "chw" ? me : null); await render(); if (me.role === "chw") sync(); return; }
      remember(null);
    } catch (e) {
      const saved = remembered();
      if (saved) { S.me = saved; S.offline = true; return render(); }
      say("warn", "You're offline. Connect once to sign in.");
    }
    render();
  }
  if (!DEMO && "serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => { /* needs localhost or HTTPS */ });
  window.addEventListener("error", () => { /* keep the page usable */ });
  start();
})();
