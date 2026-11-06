/* AfriEye front end: one page with a view for each role. No frameworks, so it can run offline on a phone. */
(function () {
  "use strict";
  const E = window.AfriEngine;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const app = $("#app");
  const S = { me: null, tab: null, flow: null, msg: null, offline: false, patient: null, queue: null, selected: null, booking: {} };
  const tick = () => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 20)));

  // ---- Talking to the server
  async function api(method, path, body) {
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
    let cfg = null;
    try { const r = await fetch("/static/model/model_config.json"); if (r.ok) cfg = await r.json(); } catch (e) { return; } // none saved yet, so the built-in model stays
    if (!cfg) return;
    try { // An exported model from notebook 12 replaces the built-in one.
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

  // ---- Page frame and shared pieces
  const ROLE = { chw: "Community health worker", clinician: "Ophthalmologist / clinician", member: "Patient" };
  const THEME = { chw: "chw", clinician: "clinic", member: "patient" };
  const PIE = { chw: ["#2A5BD7", "#D1294B", "#0FA08F", "#C77A12", "#7A4BC2", "#3B9B4F"], clinic: ["#1C6FB5", "#C4633F", "#1F9E92", "#8A5FB8", "#5E9E4F", "#C2478A"] };
  const LEVEL = {
    urgent: { label: "Urgent referral", head: "Possible severe DR found", cls: "urgent" }, refer: { label: "Refer", head: "Possible DR found", cls: "refer" },
    rescreen: { label: "No referral", head: "No referable DR found", cls: "ok" }, retake: { label: "Retake photo", head: "This photo can't be read", cls: "watch" },
  };
  const EYE = { right: "Right eye", left: "Left eye" };
  const fmtPct = (v) => (v * 100).toFixed(0) + "%";
  const age = (y) => (y ? new Date().getFullYear() - y + " yrs" : "");
  const initials = (n) => String(n || "?").split(/\s+/).filter((w) => !/^dr\.?$/i.test(w)).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const setTheme = (t) => { document.documentElement.dataset.appTheme = t; };
  function say(kind, text) { S.msg = text ? { kind, text } : null; }
  function avatar(pid, has, name, big) {
    const c = "avatar" + (big ? " big" : "");
    return has ? `<img class="${c}" src="/api/patients/${pid}/face?v=${S.faceV || 0}" alt="Photo of ${esc(name)}">` : `<span class="${c}" aria-hidden="true">${esc(initials(name))}</span>`;
  }
  function frame(body, sub) {
    const m = S.msg, u = S.me;
    setTheme(u ? THEME[u.role] : "auth");
    app.innerHTML = `<header class="band"><div class="band-in"><div class="brand"><h1>AfriEye</h1><span>${esc(sub || ROLE[u.role])}</span></div><div class="who"><span>${esc(u.name)}</span><button class="btn ghost" data-act="logout">Sign out</button></div></div></header>
    ${S.offline ? `<div class="strip">You're offline. Scanning still works, and screenings are saved on this phone until you reconnect.</div>` : ""}
    <main class="wrap">${m ? `<div class="note ${esc(m.kind)}" role="status">${esc(m.text)}</div>` : ""}${body}</main>
    <footer class="foot">Research demo, not a medical device. The AI gives a first result, and an eye specialist makes the decision.</footer>`;
  }
  const tabs = (list) => `<nav class="tabs" aria-label="Sections">${list.map(([k, t]) => `<button class="tab" data-tab="${k}" aria-current="${S.tab === k ? "page" : "false"}">${t}</button>`).join("")}</nav>`;
  const tile = (n, label, cls) => `<div class="tile ${cls || ""}"><b>${esc(n)}</b><span>${esc(label)}</span></div>`;

  // A pie with a table beside it, so that colour is never the only way to read it.
  function pie(title, items, palette) {
    const total = items.reduce((a, b) => a + b.count, 0);
    if (!total) return `<figure class="chart"><figcaption>${esc(title)}</figcaption><p class="muted">No scans in this period.</p></figure>`;
    let angle = -Math.PI / 2;
    const r = 92, c = 100, marks = items.map((it, i) => {
      const share = it.count / total, tip = `${it.label}: ${it.count} (${Math.round(share * 100)}%)`, col = palette[i % palette.length];
      if (share >= 0.9999) return `<circle cx="${c}" cy="${c}" r="${r}" fill="${col}"><title>${esc(tip)}</title></circle>`;
      const a2 = angle + share * 2 * Math.PI, p = (a) => `${(c + r * Math.cos(a)).toFixed(2)} ${(c + r * Math.sin(a)).toFixed(2)}`;
      const d = `M${c} ${c} L${p(angle)} A${r} ${r} 0 ${share > 0.5 ? 1 : 0} 1 ${p(a2)} Z`; angle = a2;
      return `<path d="${d}" fill="${col}" class="slice"><title>${esc(tip)}</title></path>`;
    }).join("");
    return `<figure class="chart"><figcaption>${esc(title)}</figcaption><div class="chart-in"><svg viewBox="0 0 200 200" role="img" aria-label="${esc(title)}: pie chart">${marks}</svg>
      <table class="legend"><thead><tr><th>Group</th><th>Scans</th><th>Share</th></tr></thead><tbody>${items.map((it, i) => `<tr><td><span class="swatch" style="background:${palette[i % palette.length]}"></span>${esc(it.label)}</td><td>${it.count}</td><td>${Math.round((it.count / total) * 100)}%</td></tr>`).join("")}</tbody></table></div></figure>`;
  }
  async function shrinkFace(file) {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error("That file couldn't be opened as a photo.")); img.src = URL.createObjectURL(file); });
    const n = Math.min(img.naturalWidth, img.naturalHeight), c = mk(320), x = c.getContext("2d"); x.imageSmoothingQuality = "high";
    x.drawImage(img, (img.naturalWidth - n) / 2, (img.naturalHeight - n) / 2, n, n, 0, 0, 320, 320);
    return c.toDataURL("image/jpeg", 0.85);
  }

  // ---- Places. A copy stays on the phone for offline use.
  function cached(key, value) {
    try { if (value !== undefined) { localStorage.setItem(key, JSON.stringify(value)); return value; } return JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { return value === undefined ? null : value; }
  }
  async function loadPlaces() {
    try { S.places = cached("afrieye.places", await api("GET", "/api/places")); } catch (e) { S.places = cached("afrieye.places") || { areas: [], hospitals: [] }; }
  }
  async function loadDirectory() {
    try { S.directory = cached("afrieye.directory", await api("GET", "/api/chw/directory")); } catch (e) { S.directory = cached("afrieye.directory"); }
  }
  function distance(a, b) {
    const rad = (d) => (d * Math.PI) / 180, dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
    return 6371 * 2 * Math.asin(Math.sqrt(Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2));
  }
  const areaById = (id) => S.places.areas.find((a) => a.id === id);
  function nearest(areaId, list) {
    const a = areaById(areaId); if (!a) return [];
    return list.map((h) => ({ ...h, km: distance(a, h) })).sort((x, y) => x.km - y.km);
  }
  const areaOptions = (sel) => `<option value="">Choose an area</option>${S.places.areas.map((a) => `<option value="${a.id}" ${a.id === sel ? "selected" : ""}>${esc(a.name)}, ${esc(a.county)}</option>`).join("")}`;

  // ---- Sign in and register
  function renderAuth() {
    setTheme("auth");
    const mode = S.authMode || "login", m = S.msg, role = S.regRole || "chw";
    const tabs2 = mode === "code" ? "" : `<div class="switch" role="tablist"><button class="sw" role="tab" aria-selected="${mode === "login"}" data-act="auth-mode" data-mode="login">Log in</button><button class="sw" role="tab" aria-selected="${mode === "register"}" data-act="auth-mode" data-mode="register">Register</button></div>`;
    let form = "";
    if (mode === "login") form = `<form id="auth" class="stack" data-mode="login"><label>Phone number<input name="phone" inputmode="tel" autocomplete="username" required placeholder="0712 345 678"></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button class="btn primary big">Log in</button></form><p class="top"><button class="link" data-act="auth-mode" data-mode="code">First time here, or forgot your password? Use an SMS code</button></p>`;
    if (mode === "register") form = `<form id="register" class="stack"><fieldset class="roles"><legend>I am registering as</legend>${[["chw", "CHW"], ["clinician", "Ophthalmologist / Clinician"], ["patient", "Patient"]].map(([k, t]) => `<label class="role-pick"><input type="radio" name="role" value="${k}" ${role === k ? "checked" : ""}><span>${t}</span></label>`).join("")}</fieldset>
      <label>Full name<input name="name" required autocomplete="name"></label><label>Phone number<input name="phone" inputmode="tel" required placeholder="0712 345 678"></label>
      ${role === "clinician" ? `<label>Hospital<select name="hospital" required><option value="">Choose your hospital</option>${S.places.hospitals.map((h) => `<option value="${h.id}">${esc(h.name)}, ${esc(h.county)}</option>`).join("")}</select></label><label>Licence number<input name="staff_id" required></label>` : `<label>Area<select name="area" required>${areaOptions("")}</select></label>`}
      ${role === "chw" ? `<label>CHW ID number<input name="staff_id" required></label>` : ""}
      ${role === "patient" ? `<label>Birth year<input name="birth_year" inputmode="numeric" maxlength="4"></label><label class="check"><input type="checkbox" name="sms_consent" checked> I agree to get my appointments and results by SMS.</label>` : ""}
      <button class="btn primary big">Continue</button></form>${role === "patient" ? "" : `<p class="small muted top">Staff accounts are checked before they can see patient records. In this demo, they're approved automatically.</p>`}`;
    if (mode === "code") form = `<h2>${S.regPhone ? "Confirm your phone" : "Use an SMS code"}</h2><p class="muted">Enter the code we sent by SMS, then choose a password.</p>${S.testCode ? `<div class="note warn top">Test mode: no SMS was sent. Your code is <b>${esc(S.testCode)}</b>.</div>` : ""}<form id="auth" class="stack top" data-mode="activate"><label>Phone number<input name="phone" inputmode="tel" required value="${esc(S.regPhone || "")}"></label><label>Code from SMS<input name="code" inputmode="numeric" autocomplete="one-time-code" required value="${esc(S.testCode || "")}"></label><label>New password, at least 8 characters<input name="password" type="password" autocomplete="new-password" required></label><button class="btn primary big">Save and continue</button></form><p class="top"><button class="link" data-act="forgot">Send me a new code</button></p><p><button class="link" data-act="auth-mode" data-mode="login">Back to log in</button></p>`;
    app.innerHTML = `<main class="auth"><section class="auth-pic"><img src="/static/login.jpg" alt="An older man having his retina examined with a phone held to his eye. The phone screen shows a photo of his retina."><div class="auth-copy"><h1>AfriEye</h1><p>Eye checks that start at home, for older adults with diabetes.</p></div></section>
      <section class="auth-panel"><div class="auth-box">${m ? `<div class="note ${esc(m.kind)}" role="status">${esc(m.text)}</div>` : ""}${tabs2}${form}<p class="small muted top">Research demo, not a medical device. Don't enter real patient data.</p></div></section></main>`;
  }

  // ---- Community health worker
  const newFlow = () => ({ consent: false, eyes: { right: { retakes: 0 }, left: { retakes: 0 } }, family: [], step: "scan", ref: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8), focus: {}, form: {}, done: null, choice: null, face: null, faceConsent: false });
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
      <p class="small muted">Scanned on this device by ${esc(Model.name)}${r.ms ? ` in ${(r.ms / 1000).toFixed(1)} seconds` : ""}.</p>
      ${isRetake ? `<p class="muted small">Retake ${e.retakes + 1} of 2. If it still can't be read after that, AfriEye refers the patient for an in-person exam.</p>` : ""}${pick}</article>`;
  }
  function samplePicker(f) {
    if (!(window.AFRIEYE_SAMPLES || []).length) return "";
    return `<details class="card" ${f.samples ? "open" : ""}><summary>No adapter here? Use a sample photo</summary><p class="muted small">Public test photos from the DeepDRiD dataset, which the model didn't train on. Tap <b>Right</b> or <b>Left</b> to scan one as that eye. The specialist grade is shown only so you can compare.</p>
      <div class="samples">${window.AFRIEYE_SAMPLES.map((s, i) => `<div class="sample"><img src="${esc(s.src)}" alt="Sample retina, grade ${s.grade}" loading="lazy"><span class="small">Grade ${s.grade}${s.good ? "" : ", poor photo"}</span><div class="row tight"><button class="btn mini" data-sample="${i}" data-eye="right">Right</button><button class="btn mini" data-sample="${i}" data-eye="left">Left</button></div></div>`).join("")}</div></details>`;
  }
  function nextStep(level) {
    if (level === "urgent") return ["Choose the nearest hospital below, and send the referral now.", "Tell the patient that an eye specialist will review it, and that an SMS will confirm the appointment.", "If sight has suddenly got worse, tell them to go to the nearest eye clinic today."];
    if (level === "refer") return ["Choose the nearest hospital below, and send the referral.", "Tell the patient that an eye specialist will review it, and that an SMS will confirm the appointment.", "Remind them to keep taking their diabetes treatment."];
    return ["Save this result below.", "Advise them to keep their blood sugar and blood pressure under control.", "Screen again in 12 months, or sooner if their sight changes."];
  }
  function renderScan(f) {
    const eyes = ["right", "left"], scanned = eyes.filter((k) => f.eyes[k].res), pending = scanned.some((k) => f.eyes[k].res.tri.level === "retake"), missing = eyes.filter((k) => !f.eyes[k].res), ready = scanned.length && !pending && f.consent;
    return `<ol class="steps" aria-label="Steps"><li class="on">Scan</li><li>Next step</li><li>Patient and hospital</li></ol>
      <section class="card"><h2>New screening</h2><label class="check top"><input type="checkbox" id="consent" ${f.consent ? "checked" : ""}> The patient agreed to have their eyes photographed and checked by AI.</label></section>
      ${f.consent ? `<div class="eyes">${eyeCard(f, "right")}${eyeCard(f, "left")}</div>${samplePicker(f)}` : ""}
      ${f.consent && scanned.length ? `<div class="actions">${pending ? `<p class="muted">Retake the photo marked above to continue.</p>` : ""}${ready ? `<button class="btn primary big" data-act="to-result">${missing.length ? `Continue without the ${EYE[missing[0]].toLowerCase()}` : "See the next step"}</button>` : ""}</div>` : ""}`;
  }
  function hospitalPicker(f) {
    const areaId = f.form.area || S.me.area_id;
    if (!areaId) return `<fieldset class="hosp"><legend>Refer to</legend><p class="muted">Choose the patient's area above to see the nearest hospitals.</p></fieldset>`;
    if (!S.directory) return `<fieldset class="hosp"><legend>Refer to</legend><p class="bad">Connect to the internet once to download the hospital list.</p></fieldset>`;
    const near = nearest(areaId, S.directory.hospitals).slice(0, 4), first = near.find((h) => h.clinicians.length);
    if (!f.choice && first) f.choice = `${first.id}|${first.clinicians[0].id}`;
    return `<fieldset class="hosp"><legend>Refer to the nearest hospital</legend><p class="muted small">Nearest first, by straight-line distance from ${esc((areaById(areaId) || {}).name || "")}. Hospital names are demo examples, and clinicians are fictional.</p>
      ${near.map((h, i) => `<div class="hosp-item"><div class="hosp-head"><b>${esc(h.name)}</b><span class="small muted">About ${h.km < 10 ? h.km.toFixed(1) : Math.round(h.km)} km${i === 0 ? " · nearest" : ""}</span></div>
        ${h.clinicians.length ? h.clinicians.map((c) => `<label class="check"><input type="radio" name="referto" value="${h.id}|${c.id}" ${f.choice === `${h.id}|${c.id}` ? "checked" : ""}> ${esc(c.name)}</label>`).join("") : `<p class="small muted">No clinician available.</p>`}</div>`).join("")}</fieldset>`;
  }
  function renderResult(f) {
    const lv = worstLevel(f), L = LEVEL[lv], form = f.form, refer = lv !== "rescreen", eyes = ["right", "left"].filter((k) => f.eyes[k].res);
    return `<ol class="steps" aria-label="Steps"><li class="was">Scan</li><li class="on">Next step</li><li class="on">Patient and hospital</li></ol>
    <section class="result ${L.cls}"><p class="eyebrow">AI first result</p><h2>${esc(L.head)}</h2><p>${eyes.map((k) => `${EYE[k]}: ${esc(f.eyes[k].res.tri.cat)}`).join(" · ")}</p>
      <h3>What to do next</h3><ol class="todo">${nextStep(lv).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
      <p class="small">${refer ? "This isn't a diagnosis. The eye specialist decides." : "This isn't a full eye exam, and macular oedema wasn't checked."} Scanned by ${esc(Model.name)}.</p></section>
    <form id="patient" class="card stack"><h2>Patient details</h2>
      <div class="face-row">${f.face ? `<img class="avatar big" src="${esc(f.face)}" alt="Patient's face photo">` : `<span class="avatar big" aria-hidden="true">${esc(initials(form.name || "?"))}</span>`}<div class="stack small-gap"><div class="row"><label class="btn" tabindex="0"><input type="file" accept="image/*" capture="user" data-face="flow" hidden>Take face photo</label><label class="btn" tabindex="0"><input type="file" accept="image/*" data-face="flow" hidden>Upload</label></div>${f.face ? `<label class="check small"><input type="checkbox" id="faceok" ${f.faceConsent ? "checked" : ""}> The patient agreed to store this photo, so staff can confirm who was screened.</label>` : `<p class="small muted">Optional. Only with the patient's agreement.</p>`}</div></div>
      <label>Full name<input name="name" required value="${esc(form.name)}" autocomplete="off"></label>
      <div class="two"><label>Birth year<input name="birth_year" inputmode="numeric" maxlength="4" value="${esc(form.birth_year)}"></label><label>Area<select name="area" id="patient-area" required>${areaOptions(form.area || S.me.area_id)}</select></label></div>
      <label>Phone number<input name="phone" inputmode="tel" required placeholder="0712 345 678" value="${esc(form.phone)}"></label>
      <label class="check"><input type="checkbox" name="sms_consent" ${form.sms_consent !== false ? "checked" : ""}> The patient agreed to get their appointment and result by SMS.</label>
      ${refer ? hospitalPicker(f) + `<fieldset><legend>Family members who also get the SMS</legend><p class="muted small">Only add people the patient chose. They'll see this person's eye result.</p>
        <div id="fam">${f.family.map((m, i) => `<div class="fam"><label>Name<input data-fam="${i}" data-k="name" value="${esc(m.name)}"></label><label>Relationship<input data-fam="${i}" data-k="relation" value="${esc(m.relation)}" placeholder="Daughter"></label><label>Phone<input data-fam="${i}" data-k="phone" inputmode="tel" value="${esc(m.phone)}"></label><button type="button" class="link" data-act="fam-del" data-i="${i}">Remove</button></div>`).join("")}</div>
        <button type="button" class="btn" data-act="fam-add">Add a family member</button></fieldset>` : ""}
      <div class="row between"><button type="button" class="link" data-act="to-scan">Back to the photos</button><button class="btn primary big">${refer ? (lv === "urgent" ? "Send urgent referral" : "Send referral") : "Save result"}</button></div></form>`;
  }
  function renderDone(f) {
    const d = f.done, refer = d.level !== "rescreen";
    return `<section class="result ${LEVEL[d.level].cls}"><p class="eyebrow">${d.queued ? "Saved on this phone" : "Done"}</p>
      <h2>${d.queued ? (refer ? "The referral will send when you're back online" : "The result will upload when you're back online") : refer ? `Referral sent to ${esc(d.hospital || "the eye specialist")}` : "Result saved"}</h2>
      <p>${refer ? "The SMS goes to the patient and their chosen family members after the specialist confirms the appointment." : "No SMS is sent. Tell the patient the result, and screen them again in 12 months."}</p></section>
      <div class="actions"><button class="btn primary big" data-act="new-screen">Screen the next patient</button><button class="btn" data-tab="home">Back to dashboard</button></div>`;
  }
  function patientLine(s) {
    return `<article class="card line person">${avatar(s.patient.id, s.patient.has_face, s.patient.name)}<div><b>${esc(s.patient.name)}</b><span class="muted"> · ${esc(age(s.patient.birth_year))}${s.patient.village ? " · " + esc(s.patient.village) : ""}</span><p class="small muted">${esc(s.screened)} · ${esc(s.ai_text)}</p>
      ${s.hospital ? `<p class="small">Referred to ${esc(s.hospital)}${s.referred_to ? ", " + esc(s.referred_to) : ""}</p>` : ""}${s.appointment ? `<p><b>Appointment:</b> ${esc(s.appointment.when_text)}, ${esc(s.appointment.location)}</p>` : ""}${s.specialist_result ? `<p><b>Specialist:</b> ${esc(s.specialist_result)}</p>` : ""}${s.status === "no_visit" ? `<p><b>Tell the patient:</b> ${esc(s.review_note)}</p>` : ""}</div>
      <span class="pill ${s.status === "confirmed" ? "ok" : s.status === "waiting_review" ? "refer" : "watch"}">${esc(s.status_text)}</span></article>`;
  }
  async function renderChwHome(head) {
    S.period = S.period || "today";
    frame(head + `<div id="dash" class="muted">Loading your dashboard…</div>`);
    let sum, rows = [];
    try { [sum, rows] = await Promise.all([api("GET", "/api/chw/summary"), api("GET", "/api/chw/screenings")]); S.chwCache = { sum, rows }; cached("afrieye.chwSummary", { sum }); }
    catch (e) { const c = S.chwCache || cached("afrieye.chwSummary"); if (!c) { const d = $("#dash"); if (d) d.textContent = "Connect to the internet to see your dashboard. You can still start a new screening."; return; } sum = c.sum; rows = c.rows || []; }
    const p = sum.periods, cur = p[S.period], d = $("#dash"), u = S.me, label = { today: "today", month: "this month", year: "this year" };
    if (!d) return;
    d.className = "stack";
    d.innerHTML = `<div class="dash-top"><section class="card profile"><span class="avatar big" aria-hidden="true">${esc(initials(u.name))}</span><div><h2>${esc(u.name)}</h2><p class="muted">Community health worker</p><dl class="facts"><div><dt>CHW ID</dt><dd>${esc(u.staff_id || "Not given")}</dd></div><div><dt>Area</dt><dd>${esc(u.area || "Not given")}${u.county ? ", " + esc(u.county) : ""}</dd></div><div><dt>Phone</dt><dd>${esc(u.phone)}</dd></div><div><dt>Status</dt><dd>${u.approved ? "Verified" : "Waiting for verification"}</dd></div></dl></div></section>
      <section class="card cta"><h2>Ready for your next visit?</h2><p class="muted">Take or upload an eye photo. The AI scan works without internet.</p><button class="btn primary big" data-tab="screen">Start new screening</button></section></div>
      ${sum.sample_count ? `<div class="note warn">Includes ${sum.sample_count} fictional sample screenings, so that the charts aren't empty in this demo.</div>` : ""}
      <section><h2>Elders scanned</h2><div class="period" role="group" aria-label="Choose a period">${[["today", "Today"], ["month", "This month"], ["year", "This year"]].map(([k, t]) => `<button class="tile pick ${S.period === k ? "on" : ""}" data-period="${k}" aria-pressed="${S.period === k}"><b>${p[k].count}</b><span>${t}</span></button>`).join("")}</div></section>
      <section class="card"><div class="chart-head"><h2>Who was scanned ${label[S.period]}</h2><p class="small muted">${cur.outcomes.urgent} urgent · ${cur.outcomes.refer} referred · ${cur.outcomes.rescreen} no referral</p></div><div class="charts">${pie("By age", cur.ages, PIE.chw)}${pie("By area", cur.areas, PIE.chw)}</div></section>
      <section><div class="row between"><h2>Recent patients</h2><button class="link" data-tab="patients">See all</button></div><div class="stack top">${rows.slice(0, 4).map(patientLine).join("") || `<p class="muted">No patients yet.</p>`}</div></section>`;
  }
  async function renderChw() {
    S.tab = S.tab || "home";
    const box = await Store.all();
    const head = tabs([["home", "Dashboard"], ["screen", "New screening"], ["patients", "My patients"]]) + (box.length ? `<div class="note warn row between"><span>${box.length} screening${box.length > 1 ? "s" : ""} not sent yet.${S.syncError ? " " + esc(S.syncError) : ""}</span><button class="btn mini" data-act="sync">Send now</button></div>` : "");
    if (S.tab === "home") return renderChwHome(head);
    if (S.tab === "patients") {
      frame(head + `<div id="list" class="muted">Loading…</div>`);
      let rows = [];
      try { rows = await api("GET", "/api/chw/screenings"); } catch (e) { if (!e.network) say("bad", e.message); }
      const queued = box.map((b) => `<article class="card line"><div><b>${esc(b.patient.name)}</b><p class="small muted">Saved on this phone, not sent yet</p></div><span class="pill watch">Waiting for internet</span></article>`).join("");
      const l = $("#list"); if (l) { l.className = "stack"; l.innerHTML = queued + rows.map(patientLine).join("") || `<p class="muted">No patients yet. Screenings you send appear here, with the specialist's decision.</p>`; }
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
      const t0 = performance.now(); e.res = await scan(img, e.retakes); e.res.ms = performance.now() - t0;
    } catch (err) { e.error = err.message || "The scan failed. Try again."; e.res = null; }
    e.busy = false; renderChw();
  }
  function keepForm() {
    const form = $("#patient"); if (!form) return;
    const fd = new FormData(form); S.flow.form = { name: fd.get("name"), birth_year: fd.get("birth_year"), area: fd.get("area"), phone: fd.get("phone"), sms_consent: fd.get("sms_consent") === "on" };
    const r = fd.get("referto"); if (r) S.flow.choice = r;
    const ok = $("#faceok"); if (ok) S.flow.faceConsent = ok.checked;
  }
  const phoneOk = (p) => /^(0[17]\d{8}|(\+?254)[17]\d{8})$/.test(String(p || "").replace(/[\s\-()]/g, ""));
  async function submitPatient() {
    keepForm();
    const f = S.flow, v = f.form, level = worstLevel(f), refer = level !== "rescreen";
    if (!phoneOk(v.phone)) { say("bad", "Enter a Kenyan mobile number, such as 0712 345 678."); return renderChw(); }
    if (!v.area) { say("bad", "Choose the patient's area."); return renderChw(); }
    if (f.face && !f.faceConsent) { say("bad", "Confirm the patient agreed to store their face photo, or remove it by starting again."); return renderChw(); }
    if (refer && !f.choice) { say("bad", "Choose a hospital and clinician for this referral."); return renderChw(); }
    for (const m of f.family) if (!m.name || !m.relation || !phoneOk(m.phone)) { say("bad", "Complete each family member's name, relationship and phone number, or remove them."); return renderChw(); }
    const [hospital, clinician] = refer ? f.choice.split("|") : [null, null];
    const eyes = ["right", "left"].filter((k) => f.eyes[k].res).map((k) => { const e = f.eyes[k], r = e.res; return { eye: k, level: r.tri.level, message: r.tri.msg, p_referable: r.p[1], p_urgent: r.p[2], p_quality: r.p[4], retakes: e.retakes, photo: r.photo }; });
    const body = { client_ref: f.ref, screening_consent: f.consent, screened_at: Date.now() / 1000, model: Model.name, eyes, hospital, clinician: clinician ? +clinician : null, face: f.face, face_consent: f.faceConsent, patient: { name: v.name.trim(), birth_year: v.birth_year.trim(), area: v.area, phone: v.phone.trim(), sms_consent: v.sms_consent }, family: refer ? f.family.map((m) => ({ ...m, receive_sms: true })) : [] };
    say();
    const hName = hospital && S.directory ? (S.directory.hospitals.find((h) => h.id === hospital) || {}).name : null;
    try { await api("POST", "/api/chw/screenings", body); f.done = { level, queued: false, hospital: hName }; }
    catch (e) {
      if (e.network || e.status === 401 || e.status === 403) { await Store.put(body); f.done = { level, queued: true }; if (!e.network) S.syncError = "Sign in again to send it."; }
      else { say("bad", e.message); return renderChw(); }
    }
    f.step = "done"; window.scrollTo(0, 0); renderChw();
  }
  async function sync() {
    const box = await Store.all(); S.syncError = null;
    if (!box.length) return; // nothing to send, so leave the screen as it is
    for (const b of box) {
      try { await api("POST", "/api/chw/screenings", b); await Store.del(b.client_ref); }
      catch (e) { S.syncError = e.network ? "Still no connection." : e.status === 401 || e.status === 403 ? "Sign in again to send it." : `${b.patient.name}: ${e.message}`; break; }
    }
    if (S.me && S.me.role === "chw") renderChw();
  }

  // ---- Ophthalmologist / clinician
  const GRADE_OPTIONS = [["", "Choose"], ["none", "No DR"], ["mild", "Mild"], ["moderate", "Moderate"], ["severe", "Severe"], ["proliferative", "Proliferative"], ["ungradable", "Can't be graded"]];
  const defaultDate = () => { const d = new Date(Date.now() + 7 * 864e5); return new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10); };
  async function renderClinic() {
    S.tab = S.tab || "referrals";
    let q = null, sum = null;
    try { [sum, q] = await Promise.all([api("GET", "/api/clinic/summary"), api("GET", "/api/clinic/referrals")]); S.queue = q; }
    catch (e) { frame(`<div class="note bad">${esc(e.message)}</div>`, S.me.hospital); return; }
    const strip = `<section class="tiles">${tile(q.waiting.length, "Waiting for review")}${tile(sum.urgent, "Urgent waiting", sum.urgent ? "alert" : "")}${tile(sum.confirmed_month, "Confirmed this month")}${tile(q.consultations.length, "Consultation requests")}</section>`;
    const head = `<div class="hosp-title"><h2>${esc(sum.hospital)}</h2><p class="muted">${esc(sum.county)} County · ${esc(S.me.name)}${S.me.staff_id ? " · " + esc(S.me.staff_id) : ""}</p></div>` + strip + tabs([["referrals", "Referrals"], ["consults", `Consultations (${q.consultations.length})`], ["reviewed", "Reviewed"], ["summary", "Summary"], ["sms", "SMS log"]]);
    frame(head + `<div id="pane"></div>`, ROLE.clinician);
    const pane = $("#pane");
    try {
      if (S.tab === "summary") {
        pane.className = "stack"; pane.innerHTML = `${sum.sample_count ? `<div class="note warn">Includes ${sum.sample_count} fictional sample screenings.</div>` : ""}<section class="card"><h2>This month</h2><p class="muted small">${sum.referrals_month} referrals received.</p><div class="charts top">${pie("Your decisions", sum.results, PIE.clinic)}${pie("Referrals by area", sum.areas, PIE.clinic)}</div></section>`;
        return;
      }
      if (S.tab === "sms") {
        const rows = await api("GET", "/api/clinic/sms");
        pane.className = "stack"; pane.innerHTML = `${S.me.sms_mode === "test" ? `<div class="note warn">Test mode: messages are recorded here, and no real SMS is sent. Sign-in codes appear here only in test mode.</div>` : ""}${rows.map((m) => `<article class="card line"><div><b>${esc(m.to_phone)}</b>${m.name ? `<span class="muted"> · ${esc(m.name)}</span>` : ""}<p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)} · ${m.parts} part${m.parts > 1 ? "s" : ""}</p></div><span class="pill ${m.status === "failed" ? "urgent" : "watch"}">${esc(m.status)}</span>${m.status === "failed" ? `<button class="btn mini" data-retry="${m.id}">Send again</button>` : ""}</article>`).join("") || `<p class="muted">No messages yet.</p>`}`;
        return;
      }
      if (S.tab === "reviewed") {
        pane.className = "stack"; pane.innerHTML = q.reviewed.map((s) => `<article class="card line person">${avatar(s.patient.id, s.patient.has_face, s.patient.name)}<div><b>${esc(s.patient.name)}</b><p class="small muted">${esc(s.reviewed)} · ${esc(s.specialist_result || "")}</p>${s.appointment ? `<p>${esc(s.appointment.when_text)}, ${esc(s.appointment.location)}</p>` : `<p>${esc(s.review_note)}</p>`}</div><span class="pill ${s.status === "confirmed" ? "ok" : "watch"}">${esc(s.status_text)}</span></article>`).join("") || `<p class="muted">Nothing reviewed yet.</p>`;
        return;
      }
      if (S.tab === "consults") {
        pane.className = "stack"; pane.innerHTML = q.consultations.map((c) => `<article class="card stack"><div class="row between"><div><b>${esc(c.name)}</b> <span class="muted">${esc(age(c.birth_year))}${c.village ? " · " + esc(c.village) : ""}</span><p class="small muted">Requested ${esc(c.requested)} · prefers ${esc(c.preferred_date)}</p></div></div><p>${esc(c.reason)}</p>
          <div class="three"><label>Date<input type="date" id="c-date-${c.id}" value="${esc(c.preferred_date)}"></label><label>Time<input type="time" id="c-time-${c.id}" value="09:00"></label><label>Note, if you decline<input id="c-note-${c.id}"></label></div><div class="row between"><button class="btn" data-cdecline="${c.id}">Decline</button><button class="btn primary" data-cconfirm="${c.id}">Confirm and send SMS</button></div></article>`).join("") || `<p class="muted">No consultation requests.</p>`;
        return;
      }
      if (!q.waiting.find((s) => s.id === S.selected)) S.selected = q.waiting.length ? q.waiting[0].id : null;
      pane.className = "split";
      pane.innerHTML = `<aside class="queue"><h2>Waiting for review (${q.waiting.length})</h2>${q.waiting.map((s) => `<button class="qitem" data-pick="${s.id}" aria-current="${s.id === S.selected ? "true" : "false"}"><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span><b>${esc(s.patient.name)}</b><span class="small muted">${esc(s.patient.village || "")} ${esc(age(s.patient.birth_year))}</span><span class="small muted">${esc(s.screened)}</span></button>`).join("") || `<p class="muted">No referrals are waiting. They'll appear here when a CHW sends one.</p>`}</aside><div id="review"></div>`;
      if (S.selected) renderReview();
    } catch (e) { pane.className = "note bad"; pane.textContent = e.message; }
  }
  function renderReview() {
    const s = S.queue.waiting.find((x) => x.id === S.selected), b = S.booking[s.id] || (S.booking[s.id] = { date: defaultDate(), time: "09:00", location: S.me.hospital || "Eye clinic", note: "", left: "", right: "" }), box = $("#review"), byEye = Object.fromEntries(s.eyes.map((e) => [e.eye, e]));
    box.innerHTML = `<section class="card"><div class="eye-head"><div class="person">${avatar(s.patient.id, s.patient.has_face, s.patient.name, true)}<div><h2>${esc(s.patient.name)}</h2><p class="muted">${esc(age(s.patient.birth_year))}${s.patient.village ? " · " + esc(s.patient.village) : ""}</p><p class="muted small">Referred by ${esc(s.chw.name)}, ${esc(s.chw.phone)}${s.referred_to ? ` · To ${esc(s.referred_to)}` : ""}</p></div></div><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span></div>
      <p class="top">${esc(s.ai_text)} <span class="muted">This is the AI's first result, from ${esc(s.model || "the phone")}. Please grade the photos yourself.</span></p></section>
      <div class="eyes">${["right", "left"].map((k) => { const e = byEye[k]; return e ? `<article class="card eye"><div class="eye-head"><h3>${EYE[k]}</h3><span class="pill ${LEVEL[e.level].cls}">${esc(LEVEL[e.level].label)}</span></div><img class="fundus" src="/api/screenings/${s.id}/photo/${e.eye}" alt="${EYE[k]} retinal photo"><dl class="scores"><div><dt>DR score</dt><dd>${fmtPct(e.p_referable)}</dd></div><div><dt>Severe score</dt><dd>${fmtPct(e.p_urgent)}</dd></div><div><dt>Quality</dt><dd>${fmtPct(e.p_quality)}</dd></div></dl>${e.retakes ? `<p class="small muted">Retaken ${e.retakes} time${e.retakes > 1 ? "s" : ""}.</p>` : ""}<label>Your grade, ${EYE[k].toLowerCase()}<select data-book="${k}">${GRADE_OPTIONS.map(([v, t]) => `<option value="${v}" ${b[k] === v ? "selected" : ""}>${t}</option>`).join("")}</select></label></article>` : `<article class="card eye muted"><h3>${EYE[k]}</h3><p>Not scanned.</p></article>`; }).join("")}</div>
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
    frame(`<div id="rec" class="muted">Loading…</div>`, list.find((p) => p.id === pid).access === "owner" ? ROLE.member : "Family member");
    let d; try { d = await api("GET", `/api/patients/${pid}`); } catch (e) { $("#rec").className = "note bad"; $("#rec").textContent = e.message; return; }
    const st = d.status, next = d.next_appointment, owner = d.access === "owner", P = d.patient, future = d.appointments.filter((a) => a.upcoming).sort((a, b) => (a.when < b.when ? -1 : 1)), past = d.appointments.filter((a) => !a.upcoming);
    const cls = st ? (st.level === "urgent" ? "urgent" : st.level === "refer" ? "refer" : st.level === "ok" ? "ok" : "watch") : "watch";
    const hospitals = nearest(P.area_id, S.places.hospitals), hlist = hospitals.length ? hospitals : S.places.hospitals, waitingConsult = d.consultations.some((c) => c.status === "requested");
    const chooser = list.length > 1 ? `<label>Whose record?<select id="who">${list.map((p) => `<option value="${p.id}" ${p.id === pid ? "selected" : ""}>${esc(p.name)}${p.relation && p.relation !== "Self" ? ` (${esc(p.relation)})` : ""}</option>`).join("")}</select></label>` : "";
    const rec = $("#rec"); rec.className = "stack";
    rec.innerHTML = `${chooser}<div class="pt-top"><section class="card profile">${avatar(P.id, P.has_face, P.name, true)}<div class="grow"><h2>${esc(P.name)}</h2><p class="muted">${esc(age(P.birth_year))}${P.village ? " · " + esc(P.village) : ""}${P.county ? ", " + esc(P.county) : ""}</p>
        ${owner ? `<div class="row top"><label class="btn mini" tabindex="0"><input type="file" accept="image/*" capture="user" data-face="own" hidden>Take photo</label><label class="btn mini" tabindex="0"><input type="file" accept="image/*" data-face="own" hidden>Upload photo</label>${P.has_face ? `<button class="link" data-act="face-del">Remove</button>` : ""}</div>` : ""}</div></section>
      <section class="result ${cls}"><p class="eyebrow">${st ? `From your eye specialist, ${esc(st.date)}` : "Your eye health"}</p><h2>${esc(st ? st.label : "No specialist result yet")}</h2><p>${esc(st ? st.advice : "Your result appears here after an eye specialist reviews your photos.")}</p>${st && st.note ? `<p class="small">Specialist's note: ${esc(st.note)}</p>` : ""}</section></div>
      <section class="tiles trio">${tile(future.length, "Future appointments")}${tile(past.length, "Past appointments")}${tile(d.screenings.length, "Eye screenings")}</section>
      <div class="pt-grid"><section class="card"><h3>Your details</h3><dl class="facts one"><div><dt>Condition</dt><dd>${esc(d.condition ? "Diabetic retinopathy check: " + d.condition : "Waiting for a specialist result")}</dd></div><div><dt>Eye doctor</dt><dd>${esc(d.doctor || "Not assigned yet")}</dd></div><div><dt>Community health worker</dt><dd>${d.chw ? `${esc(d.chw.name)}, ${esc(d.chw.phone)}` : "Not assigned yet"}</dd></div><div><dt>Phone</dt><dd>${esc(P.phone)}</dd></div></dl>
          ${owner ? `<label class="top">Your location<select id="myarea">${areaOptions(P.area_id)}</select></label><label class="check top"><input type="checkbox" id="smsok" ${P.sms_consent ? "checked" : ""}> Send me appointment and result SMS</label>` : `<p class="top"><b>Location:</b> ${esc(P.village || "Not given")}</p>`}</section>
        <section class="card"><h3>Next appointment</h3>${next ? `<p class="big-text">${esc(next.when_text)}</p><p>${esc(next.location)}</p><p class="muted">${esc(next.doctor)}</p>` : `<p class="muted">None booked.</p>`}${future.length > 1 ? `<h3 class="top">Also coming up</h3>${future.slice(1).map((a) => `<p>${esc(a.when_text)}, ${esc(a.location)}</p>`).join("")}` : ""}${past.length ? `<h3 class="top">Past</h3>${past.slice(0, 3).map((a) => `<p class="small muted">${esc(a.when_text)}, ${esc(a.location)}${a.doctor ? ", " + esc(a.doctor) : ""}</p>`).join("")}` : ""}</section></div>
      ${owner ? `<section class="card"><h3>Book a consultation</h3>${waitingConsult ? `<p class="muted">Your request is waiting for the clinic. They'll reply by SMS.</p>` : `<form id="consult" class="stack top"><div class="two"><label>Hospital, nearest first<select name="hospital" required>${hlist.map((h) => `<option value="${h.id}">${esc(h.name)}${h.km !== undefined ? `, about ${h.km < 10 ? h.km.toFixed(1) : Math.round(h.km)} km` : ""}</option>`).join("")}</select></label><label>Preferred date<input type="date" name="date" required value="${defaultDate()}"></label></div><label>What would you like to discuss?<textarea name="reason" rows="2" maxlength="300" required></textarea></label><button class="btn primary">Request consultation</button></form>`}
          ${d.consultations.length ? `<div class="top">${d.consultations.map((c) => `<div class="hist"><div class="row between"><b>${esc(c.hospital)}</b><span class="pill ${c.status === "confirmed" ? "ok" : c.status === "declined" ? "urgent" : "watch"}">${esc({ requested: "Waiting", confirmed: "Confirmed", declined: "Declined" }[c.status])}</span></div><p class="small muted">${esc(c.reason)}${c.note ? ` · Clinic: ${esc(c.note)}` : ""}</p></div>`).join("")}</div>` : ""}</section>` : ""}
      ${d.screenings.length ? `<section class="card"><h3>Eye screenings</h3>${d.screenings.map((s) => `<div class="hist"><p>${esc(s.text)}</p><p class="small muted">${esc(s.date)} · ${esc(s.status)}</p></div>`).join("")}<p class="small muted top">The AI gives a first result. It isn't a diagnosis or a full eye exam.</p></section>` : ""}
      ${owner ? `<section class="card stack"><h3>Family who can see this record</h3>${(d.family || []).filter((f) => f.access !== "owner").map((f) => `<div class="fam-row"><div><b>${esc(f.name)}</b> <span class="muted">${esc(f.relation)}</span><p class="small muted">${esc(f.phone)}${f.joined ? "" : " · hasn't signed in yet"}</p></div><label class="check small"><input type="checkbox" data-famsms="${f.id}" ${f.receive_sms ? "checked" : ""}> Gets SMS</label><button class="link" data-famdel="${f.id}">Remove</button></div>`).join("") || `<p class="muted">No family members yet.</p>`}
        <form id="addfam" class="stack"><div class="three"><label>Name<input name="name" required></label><label>Relationship<input name="relation" required placeholder="Son"></label><label>Phone<input name="phone" inputmode="tel" required></label></div><label class="check"><input type="checkbox" name="receive_sms" checked> Also send them appointment SMS</label><button class="btn">Add family member</button></form></section>` : `<p class="muted small">You can view this record because ${esc(P.name.split(" ")[0])} added you as family.</p>`}
      ${d.messages.length ? `<section class="card"><h3>SMS sent</h3>${d.messages.map((m) => `<div class="hist"><p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)} · ${esc(m.status)}</p></div>`).join("")}</section>` : ""}`;
  }

  // ---- Routing
  function renderPending() {
    frame(`<section class="card narrow"><h2>Your account is waiting for verification</h2><p class="top">Your ${S.me.role === "chw" ? "CHW ID" : "licence"} needs to be checked before you can see patient records. Sign out and try again later.</p></section>`);
  }
  async function render() {
    if (!S.me) return renderAuth();
    if (S.me.role !== "member" && !S.me.approved) return renderPending();
    if (S.me.role === "chw") return renderChw();
    if (S.me.role === "clinician") return renderClinic();
    return renderFamily();
  }
  async function afterSignIn(me) {
    S.me = me; S.tab = null; S.flow = null; S.selected = null; S.patient = null; S.regPhone = null; S.testCode = null; S.regRole = null; S.authMode = "login"; remember(me.role === "chw" ? me : null); say();
    if (me.role === "chw") await loadDirectory();
    await render(); if (me.role === "chw" && me.approved) sync();
  }

  // ---- Events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-act],[data-tab],[data-pick],[data-sample],[data-retry],[data-famdel],[data-period],[data-cconfirm],[data-cdecline]"); if (!t) return;
    if (t.dataset.tab) { keepForm(); S.tab = t.dataset.tab; say(); if (t.dataset.tab === "screen" && S.flow && S.flow.step === "done") S.flow = null; return render(); }
    if (t.dataset.period) { S.period = t.dataset.period; return renderChw(); }
    if (t.dataset.pick) { S.selected = +t.dataset.pick; say(); return renderClinic(); }
    if (t.dataset.sample) { const s = window.AFRIEYE_SAMPLES[+t.dataset.sample]; S.flow.samples = true; return takePhoto(t.dataset.eye, null, s.src); }
    if (t.dataset.retry) { try { await api("POST", `/api/clinic/sms/${t.dataset.retry}/retry`, {}); } catch (e) { say("bad", e.message); } return renderClinic(); }
    if (t.dataset.famdel) { try { await api("DELETE", `/api/patients/${S.patient}/family/${t.dataset.famdel}`); say("ok", "Removed."); } catch (e) { say("bad", e.message); } return renderFamily(); }
    if (t.dataset.cconfirm || t.dataset.cdecline) {
      const id = t.dataset.cconfirm || t.dataset.cdecline; t.disabled = true;
      try {
        if (t.dataset.cconfirm) { const r = await api("POST", `/api/clinic/consultations/${id}/confirm`, { date: $(`#c-date-${id}`).value, time: $(`#c-time-${id}`).value }); say("ok", r.consent ? `Consultation confirmed. SMS ${S.me.sms_mode === "test" ? "recorded in test mode" : "sent"}.` : "Consultation confirmed. The patient didn't agree to SMS, so please call them."); }
        else { await api("POST", `/api/clinic/consultations/${id}/decline`, { note: $(`#c-note-${id}`).value }); say("ok", "Request declined. The patient can see your note."); }
      } catch (e) { say("bad", e.message); }
      window.scrollTo(0, 0); return renderClinic();
    }
    const act = t.dataset.act, f = S.flow;
    if (act === "logout") {
      const box = S.me.role === "chw" ? await Store.all() : [];
      if (box.length) { say("warn", `${box.length} screening${box.length > 1 ? "s are" : " is"} still waiting to send. Send ${box.length > 1 ? "them" : "it"} before signing out.`); return render(); }
      try { await api("POST", "/api/auth/logout", {}); } catch (e) { /* signing out locally is enough when offline */ }
      S.me = null; remember(null); try { localStorage.removeItem("afrieye.directory"); localStorage.removeItem("afrieye.chwSummary"); } catch (e) { /* nothing was stored */ } S.chwCache = null; say(); S.authMode = "login"; return render();
    }
    if (act === "auth-mode") { S.authMode = t.dataset.mode; S.testCode = null; S.regPhone = t.dataset.mode === "code" ? S.regPhone : null; say(); return render(); }
    if (act === "forgot") { const phone = $("#auth [name=phone]").value; try { const r = await api("POST", "/api/auth/forgot", { phone }); S.regPhone = phone; S.testCode = r.test_code || null; say("ok", r.message); } catch (e) { say("bad", e.message); } return render(); }
    if (act === "to-result") { f.step = "result"; say(); window.scrollTo(0, 0); return renderChw(); }
    if (act === "to-scan") { keepForm(); f.step = "scan"; return renderChw(); }
    if (act === "new-screen") { S.flow = newFlow(); say(); return renderChw(); }
    if (act === "fam-add") { keepForm(); f.family.push({ name: "", relation: "", phone: "" }); return renderChw(); }
    if (act === "fam-del") { keepForm(); f.family.splice(+t.dataset.i, 1); return renderChw(); }
    if (act === "sync") return sync();
    if (act === "face-del") { try { await api("DELETE", `/api/patients/${S.patient}/face`); S.faceV = Date.now(); say("ok", "Photo removed."); } catch (e) { say("bad", e.message); } return renderFamily(); }
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
  document.addEventListener("input", (ev) => {
    const t = ev.target;
    if (t.dataset.fam !== undefined) S.flow.family[+t.dataset.fam][t.dataset.k] = t.value;
  });
  document.addEventListener("change", async (ev) => {
    const t = ev.target;
    if (t.name === "role" && t.form && t.form.id === "register") { S.regRole = t.value; return render(); }
    if (t.id === "consent") { S.flow.consent = t.checked; return renderChw(); }
    if (t.id === "patient-area") { keepForm(); S.flow.choice = null; return renderChw(); }
    if (t.id === "faceok") { S.flow.faceConsent = t.checked; return; }
    if (t.dataset.photo && t.files[0]) return takePhoto(t.dataset.photo, t.files[0]);
    if (t.dataset.face && t.files[0]) {
      try {
        const data = await shrinkFace(t.files[0]);
        if (t.dataset.face === "flow") { keepForm(); S.flow.face = data; S.flow.faceConsent = false; return renderChw(); }
        await api("POST", `/api/patients/${S.patient}/face`, { photo: data }); S.faceV = Date.now(); say("ok", "Photo saved.");
      } catch (e) { say("bad", e.message); }
      return render();
    }
    if (t.dataset.focus) { S.flow.focus[t.dataset.focus] = t.checked; const c = $(`canvas[data-eye=${t.dataset.focus}]`); return paintEye(c, S.flow.eyes[t.dataset.focus].res, t.checked); }
    if (t.dataset.book) { S.booking[S.selected][t.dataset.book] = t.value; return previewSms(); }
    if (t.id === "who") { S.patient = +t.value; return renderFamily(); }
    if (t.id === "myarea") { try { await api("POST", `/api/patients/${S.patient}/location`, { area: t.value }); say("ok", "Location saved."); } catch (e) { say("bad", e.message); } return renderFamily(); }
    if (t.dataset.famsms) { try { await api("POST", `/api/patients/${S.patient}/family/${t.dataset.famsms}/sms`, { receive_sms: t.checked }); } catch (e) { say("bad", e.message); renderFamily(); } return; }
    if (t.id === "smsok") { try { await api("POST", `/api/patients/${S.patient}/sms-consent`, { consent: t.checked }); } catch (e) { say("bad", e.message); renderFamily(); } }
  });
  document.addEventListener("keydown", (ev) => { if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("label.btn")) { ev.preventDefault(); ev.target.querySelector("input").click(); } });
  document.addEventListener("submit", async (ev) => {
    ev.preventDefault(); const form = ev.target;
    if (form.id === "auth") {
      const fd = Object.fromEntries(new FormData(form));
      try { await afterSignIn(await api("POST", form.dataset.mode === "login" ? "/api/auth/login" : "/api/auth/activate", fd)); } catch (e) { say("bad", e.message); render(); }
    } else if (form.id === "register") {
      const fd = Object.fromEntries(new FormData(form)); fd.sms_consent = fd.sms_consent === "on";
      try { const r = await api("POST", "/api/auth/register", fd); S.regPhone = fd.phone; S.testCode = r.test_code || null; S.authMode = "code"; say(); render(); } catch (e) { say("bad", e.message); render(); }
    } else if (form.id === "patient") {
      form.querySelector(".btn.primary").disabled = true; await submitPatient();
    } else if (form.id === "consult") {
      const fd = Object.fromEntries(new FormData(form));
      try { await api("POST", `/api/patients/${S.patient}/consultations`, fd); say("ok", "Request sent. The clinic will reply by SMS."); } catch (e) { say("bad", e.message); }
      window.scrollTo(0, 0); renderFamily();
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
    await Promise.all([loadModel(), loadPlaces()]);
    try { const list = await (await fetch("/static/samples.json")).json(); window.AFRIEYE_SAMPLES = list.map((s) => ({ ...s, src: "/static/samples/" + s.file })); } catch (e) { window.AFRIEYE_SAMPLES = []; }
    try {
      const me = await api("GET", "/api/me");
      if (me.signed_in) { S.me = me; remember(me.role === "chw" ? me : null); if (me.role === "chw") await loadDirectory(); await render(); if (me.role === "chw" && me.approved) sync(); return; }
      remember(null);
    } catch (e) {
      const saved = remembered();
      if (saved) { S.me = saved; S.directory = cached("afrieye.directory"); S.offline = true; S.tab = "screen"; return render(); }
      say("warn", "You're offline. Connect once to sign in.");
    }
    render();
  }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => { /* needs localhost or HTTPS */ });
  start();
})();
