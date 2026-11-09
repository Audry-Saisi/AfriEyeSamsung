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
  const ROLE = { chw: "Community health worker", clinician: "Ophthalmologist / clinician", member: "Patient", county: "County health officer" };
  const THEME = { chw: "chw", clinician: "clinic", member: "patient", county: "county" };
  const PIE = { chw: ["#006B3F", "#BB0000", "#1C1C1C", "#D08A16", "#5FA777", "#8C8C8C"], clinic: ["#006B3F", "#BB0000", "#1C1C1C", "#D08A16", "#5FA777", "#8C8C8C"] };
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
    const helper = u.role === "chw" && !S.view;
    app.innerHTML = `<header class="band"><div class="band-in"><div class="brand"><img class="brand-mark" src="/static/logo_mark_96.png" alt="" width="40" height="40"><h1>AfriEye</h1><span>${esc(sub || ROLE[u.role])}</span></div><div class="who"><span class="who-name" data-notr>${esc(u.name)}</span>${langPicker("lang-head")}<button class="icon-btn" data-act="alerts" aria-label="Alerts" title="Alerts">${ICON.bell}<b id="bell-count" class="badge" hidden></b></button><button class="icon-btn" data-act="settings" aria-label="Settings" title="Settings">${ICON.gear}</button><button class="btn ghost signout" data-act="logout" type="button">Sign out</button></div></div></header>
    ${S.offline ? `<div class="strip">You're offline. Scanning still works, and screenings are saved on this phone until you reconnect.</div>` : ""}
    <main class="wrap">${m ? `<div class="note ${esc(m.kind)}" role="status">${esc(m.text)}</div>` : ""}${body}</main>
    <footer class="foot">The AI gives a first result, and an eye specialist makes the decision.</footer>
    ${helper ? `<button class="fab" data-act="assistant" type="button">${ICON.chat}<span>Ask AfriEye</span></button>` : ""}`;
    pollAlerts();
  }
  const tabs = (list) => `<nav class="tabs" aria-label="Sections">${list.map(([k, t]) => `<button class="tab" data-tab="${k}" aria-current="${S.tab === k ? "page" : "false"}">${t}</button>`).join("")}</nav>`;
  const tile = (n, label, cls) => `<div class="tile ${cls || ""}"><b>${esc(n)}</b><span>${esc(label)}</span></div>`;

  // A pie with a table beside it, so that colour is never the only way to read it.
  function pie(title, items, palette, unit) {
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
      <table class="legend"><thead><tr><th>Group</th><th>${unit || "Scans"}</th><th>Share</th></tr></thead><tbody>${items.map((it, i) => `<tr><td><span class="swatch" style="background:${palette[i % palette.length]}"></span>${esc(it.label)}</td><td>${it.count}</td><td>${Math.round((it.count / total) * 100)}%</td></tr>`).join("")}</tbody></table></div></figure>`;
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
    if (mode === "register") form = `<form id="register" class="stack"><fieldset class="roles"><legend>I am registering as</legend>${[["chw", "CHW"], ["clinician", "Ophthalmologist / Clinician"], ["patient", "Patient"], ["county", "County health officer"]].map(([k, t]) => `<label class="role-pick"><input type="radio" name="role" value="${k}" ${role === k ? "checked" : ""}><span>${t}</span></label>`).join("")}</fieldset>
      <label>Full name<input name="name" required autocomplete="name"></label><label>Phone number<input name="phone" inputmode="tel" required placeholder="0712 345 678"></label>
      <label>Email${role === "clinician" ? ", for new referral alerts" : ", optional"}<input name="email" type="email" autocomplete="email" ${role === "clinician" ? "required" : ""} placeholder="name@example.com"></label>
      ${role === "county" ? `<label>County<select name="county" required><option value="">Choose your county</option>${[...new Set(S.places.areas.map((a) => a.county))].sort().map((c) => `<option>${esc(c)}</option>`).join("")}</select></label><label>Staff number<input name="staff_id" required></label>` : role === "clinician" ? `<label>Hospital<select name="hospital" required><option value="">Choose your hospital</option>${S.places.hospitals.map((h) => `<option value="${h.id}">${esc(h.name)}, ${esc(h.county)}</option>`).join("")}</select></label><label>Licence number<input name="staff_id" required></label>` : `<label>Area<select name="area" required>${areaOptions("")}</select></label>`}
      ${role === "chw" ? `<label>CHW ID number<input name="staff_id" required></label>` : ""}
      ${role === "patient" ? `<label>Birth year<input name="birth_year" inputmode="numeric" maxlength="4"></label><label class="check"><input type="checkbox" name="sms_consent" checked> I agree to get my appointments and results by SMS and email.</label>` : ""}
      <button class="btn primary big">Continue</button></form>${role === "patient" ? "" : `<p class="small muted top">Staff accounts are checked before they can see patient records.</p>`}`;
    if (mode === "code") form = `<h2>${S.regPhone ? "Confirm your phone" : "Use an SMS code"}</h2><p class="muted">Enter the code we sent by SMS, then choose a password.</p>${S.testCode ? `<div class="note warn top">Your code is <b>${esc(S.testCode)}</b>.</div>` : ""}<form id="auth" class="stack top" data-mode="activate"><label>Phone number<input name="phone" inputmode="tel" required value="${esc(S.regPhone || "")}"></label><label>Code from SMS<input name="code" inputmode="numeric" autocomplete="one-time-code" required value="${esc(S.testCode || "")}"></label><label>New password, at least 8 characters<input name="password" type="password" autocomplete="new-password" required></label><button class="btn primary big">Save and continue</button></form><p class="top"><button class="link" data-act="forgot">Send me a new code</button></p><p><button class="link" data-act="auth-mode" data-mode="login">Back to log in</button></p>`;
    app.innerHTML = `<main class="auth"><section class="auth-pic"><img src="/static/community.jpg" alt="An AfriEye health worker greets smiling elders outside a village home."><div class="auth-copy"><p class="tagline" data-notr>Kuja Utuone</p><p>Eye checks that start at home, for older adults with diabetes.</p></div></section>
      <section class="auth-panel"><div class="auth-box"><div class="auth-top"><img class="auth-logo" src="/static/logo.png" alt="AfriEye, Kuja Utuone" width="491" height="600">${langPicker("lang-auth")}</div>${m ? `<div class="note ${esc(m.kind)}" role="status">${esc(m.text)}</div>` : ""}${tabs2}${form}</div></section></main>`;
  }

  // ---- Community health worker
  const newFlow = () => ({ consent: false, eyes: { right: { retakes: 0 }, left: { retakes: 0 } }, family: [], step: "scan", ref: "s" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8), focus: {}, form: {}, done: null, history: { items: [], diabetes_type: null, years: "", insulin: false, notes: "" }, choice: null, face: null, faceConsent: false });
  function worstLevel(f) {
    const rank = ["rescreen", "refer", "urgent"]; let w = null;
    for (const k of ["left", "right"]) { const r = f.eyes[k].res; if (r && rank.indexOf(r.tri.level) > (w === null ? -1 : rank.indexOf(w))) w = r.tri.level; }
    return w;
  }
  function eyeCard(f, k) {
    const e = f.eyes[k], r = e.res, lv = r && LEVEL[r.tri.level];
    const pick = `<div class="row">${navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? `<button class="btn ${r ? "" : "primary"}" data-live="${k}" type="button">Live camera with tips</button>` : ""}<label class="btn" tabindex="0"><input type="file" accept="image/*" capture="environment" data-photo="${k}" hidden>${r ? "Take again" : "Take photo"}</label><label class="btn" tabindex="0"><input type="file" accept="image/*" data-photo="${k}" hidden>Upload photo</label></div>`;
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
    return `<ol class="steps" aria-label="Steps"><li class="on">History and scan</li><li>Next step</li><li>Patient and hospital</li></ol>
      <section class="card"><h2>New screening</h2><label class="check top"><input type="checkbox" id="consent" ${f.consent ? "checked" : ""}> The patient agreed to have their eyes photographed and checked by AI.</label></section>
      ${f.consent ? historyCard(f) : ""}
      ${f.consent && !f.history.diabetes_type ? `<p class="muted">Choose the type of diabetes to start the eye photos.</p>` : ""}
      ${f.consent && f.history.diabetes_type ? `<div class="eyes">${eyeCard(f, "right")}${eyeCard(f, "left")}</div>${samplePicker(f)}` : ""}
      ${f.consent && f.history.diabetes_type && scanned.length ? `<div class="actions">${pending ? `<p class="muted">Retake the photo marked above to continue.</p>` : ""}${ready ? `<button class="btn primary big" data-act="to-result">${missing.length ? `Continue without the ${EYE[missing[0]].toLowerCase()}` : "See the next step"}</button>` : ""}</div>` : ""}`;
  }
  function hospitalPicker(f) {
    const areaId = f.form.area || S.me.area_id;
    if (!areaId) return `<fieldset class="hosp"><legend>Refer to</legend><p class="muted">Choose the patient's area above to see the nearest hospitals.</p></fieldset>`;
    if (!S.directory) return `<fieldset class="hosp"><legend>Refer to</legend><p class="bad">Connect to the internet once to download the hospital list.</p></fieldset>`;
    const near = nearest(areaId, S.directory.hospitals).slice(0, 4), first = near.find((h) => h.clinicians.length);
    if (!f.choice && first) f.choice = `${first.id}|${first.clinicians[0].id}`;
    return `<fieldset class="hosp"><legend>Refer to the nearest hospital</legend><p class="muted small">Nearest first, by straight-line distance from ${esc((areaById(areaId) || {}).name || "")}.</p>
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
      <div class="two"><label>Phone number<input name="phone" inputmode="tel" required placeholder="0712 345 678" value="${esc(form.phone)}"></label><label>Email, optional<input name="email" type="email" placeholder="name@example.com" value="${esc(form.email)}"></label></div>
      <label class="check"><input type="checkbox" name="sms_consent" ${form.sms_consent !== false ? "checked" : ""}> The patient agreed to get their appointment and result by SMS and email.</label>
      ${refer ? hospitalPicker(f) + `<fieldset><legend>Family members who also get the SMS and email</legend><p class="muted small">Only add people the patient chose. They'll see this person's eye result.</p>
        <div id="fam">${f.family.map((m, i) => `<div class="fam"><label>Name<input data-fam="${i}" data-k="name" value="${esc(m.name)}"></label><label>Relationship<input data-fam="${i}" data-k="relation" value="${esc(m.relation)}" placeholder="Daughter"></label><label>Phone<input data-fam="${i}" data-k="phone" inputmode="tel" value="${esc(m.phone)}"></label><label>Email, optional<input data-fam="${i}" data-k="email" type="email" value="${esc(m.email || "")}"></label><button type="button" class="link" data-act="fam-del" data-i="${i}">Remove</button></div>`).join("")}</div>
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
      <div class="stack small-gap end"><span class="pill ${s.status === "confirmed" ? "ok" : s.status === "waiting_review" ? "refer" : "watch"}">${esc(s.status_text)}</span><button class="btn mini" data-report="${s.patient.id}" type="button">Preview and send PDF report</button></div></article>`;
  }
  async function renderChwHome(head) {
    S.period = S.period || "today";
    frame(head + `<div id="dash" class="muted">Loading your dashboard…</div>`);
    let sum, rows = [];
    let visits = [], shaHelp = [], fu = { missed: [], recall: [] };
    try { [sum, rows, visits, shaHelp, fu] = await Promise.all([api("GET", "/api/chw/summary"), api("GET", "/api/chw/screenings"), api("GET", "/api/chw/visits"), api("GET", "/api/chw/cover-help"), api("GET", "/api/chw/followups")]); S.recall = fu.recall; S.chwCache = { sum, rows }; cached("afrieye.chwSummary", { sum }); }
    catch (e) { const c = S.chwCache || cached("afrieye.chwSummary"); if (!c) { const d = $("#dash"); if (d) d.textContent = "Connect to the internet to see your dashboard. You can still start a new screening."; return; } sum = c.sum; rows = c.rows || []; }
    const p = sum.periods, cur = p[S.period], d = $("#dash"), u = S.me, label = { today: "today", month: "this month", year: "this year" };
    if (!d) return;
    d.className = "stack";
    d.innerHTML = `<div class="dash-top"><section class="card profile"><span class="avatar big" aria-hidden="true">${esc(initials(u.name))}</span><div><h2>${esc(u.name)}</h2><p class="muted">Community health worker</p><dl class="facts"><div><dt>CHW ID</dt><dd>${esc(u.staff_id || "Not given")}</dd></div><div><dt>Area</dt><dd>${esc(u.area || "Not given")}${u.county ? ", " + esc(u.county) : ""}</dd></div><div><dt>Phone</dt><dd>${esc(u.phone)}</dd></div><div><dt>Status</dt><dd>${u.approved ? "Verified" : "Waiting for verification"}</dd></div><div><dt>AfriEye training</dt><dd>${u.trained ? `<span class="pill ok">${esc(t("Trained"))}</span> <span data-notr>${esc(u.trained.slice(0, 11))}</span>` : `<button class="link" data-tab="training">Not done yet. Start now</button>`}</dd></div><div><dt>Patient rating</dt><dd>${sum.ratings && sum.ratings.average ? `<span data-notr>★ ${sum.ratings.average} (${sum.ratings.count})</span>` : "No ratings yet"}</dd></div></dl></div></section>
      <section class="card cta"><img class="cta-pic" src="/static/community.jpg" alt="" loading="lazy"><h2>Ready for your next visit?</h2><p class="muted">Take or upload an eye photo. The AI scan works without internet.</p><button class="btn primary big" data-tab="screen">Start new screening</button></section></div>
      
      ${fu.missed.length ? `<section class="card stack alert-card"><h2>Missed eye appointments</h2><p class="small muted">${esc(t("These patients didn't go to their eye appointment. Visit or call them, find out why, and help them book again."))}</p>${fu.missed.map((m) => `<div class="hist row between"><div><b data-notr>${esc(m.name)}</b> <span class="muted" data-notr>${esc(m.village || "")}</span><p class="small muted"><span data-notr>${esc(m.appt)}, ${esc(m.location)}</span></p><p class="small"><a href="tel:${esc(m.phone)}" data-notr>${esc(m.phone)}</a></p></div><button class="btn mini primary" data-fudone="${m.id}" type="button">Followed up</button></div>`).join("")}</section>` : ""}
      ${fu.recall.length ? `<section class="card stack"><h2>Due for an eye check</h2><p class="small muted">${esc(t("People with diabetes need an eye check every year, or every 6 months if early changes were seen."))}</p>${fu.recall.map((r, i) => `<div class="hist row between"><div><b data-notr>${esc(r.name)}</b> <span class="muted" data-notr>${esc(age(r.birth_year))} · ${esc(r.village || "")}</span><p class="small ${r.overdue ? "bad" : "muted"}">${esc(r.overdue ? tr("Overdue since {d}").replace("{d}", r.due) : tr("Due {d}").replace("{d}", r.due))} · ${esc(r.reason === "early" ? t("6-month check") : t("Yearly check"))}</p></div><button class="btn mini primary" data-recall="${i}" type="button">Screen now</button></div>`).join("")}</section>` : ""}
      ${shaHelp.length ? `<section class="card stack"><h2>Help with SHA</h2><p class="small muted">${esc(t("These patients have no health cover. Help them apply: dial *147# with them or use afyayangu.go.ke. They need their national ID number."))}</p>${shaHelp.map((h) => `<div class="hist row between"><div><b data-notr>${esc(h.name)}</b> <span class="muted" data-notr>${esc(age(h.birth_year))} · ${esc(h.village || "")}</span><p class="small muted"><span data-notr>${esc(h.phone)} · ${esc(h.when)}</span> · ${esc(t(h.cover))}</p></div>${h.status === "requested" ? `<button class="btn mini primary" data-sha="${h.id}" data-shaact="start" type="button">I'll help</button>` : `<form class="row" data-shadone="${h.id}"><label class="grow"><span class="sr">SHA number</span><input name="ref" maxlength="40" placeholder="SHA number, optional"></label><button class="btn mini primary">Registered</button></form>`}</div>`).join("")}</section>` : ""}
      ${visits.length ? `<section class="card stack"><h2>Visit requests</h2>${visits.map((v) => `<div class="hist row between"><div>${v.kind === "village" ? `<span class="pill refer">${esc(t("Village eye check"))}</span> <b data-notr>${esc(v.place || v.village || "")}</b> <span class="muted">${esc(tr("About {n} people").replace("{n}", v.people || "?"))}${v.preferred ? ` · <span data-notr>${esc(v.preferred)}</span>` : ""}</span><p class="small">${esc(t("Asked by"))} <span data-notr>${esc(v.name)}</span></p>` : `<span class="pill watch">${esc(t("Home visit"))}</span> <b data-notr>${esc(v.name)}</b> <span class="muted" data-notr>${esc(age(v.birth_year))} · ${esc(v.village || "")}</span>`}<p class="small muted"><span data-notr>${esc(v.phone)} · ${esc(v.when)}</span>${v.source === "ussd" ? " · USSD" : ""}</p>${v.note ? `<p class="small" data-notr>${esc(v.note)}</p>` : ""}</div>${v.status === "open" ? `<button class="btn mini primary" data-visit="${v.id}" data-vact="accept" type="button">Accept visit</button>` : `<button class="btn mini" data-visit="${v.id}" data-vact="done" type="button">Mark visited</button>`}</div>`).join("")}</section>` : ""}
      <section><h2>Elders scanned</h2><div class="period" role="group" aria-label="Choose a period">${[["today", "Today"], ["month", "This month"], ["year", "This year"]].map(([k, t]) => `<button class="tile pick ${S.period === k ? "on" : ""}" data-period="${k}" aria-pressed="${S.period === k}"><b>${p[k].count}</b><span>${t}</span></button>`).join("")}</div></section>
      <section class="card"><div class="chart-head"><h2>Who was scanned ${label[S.period]}</h2><p class="small muted">${cur.outcomes.urgent} urgent · ${cur.outcomes.refer} referred · ${cur.outcomes.rescreen} no referral</p></div><div class="charts">${pie("By age", cur.ages, PIE.chw)}${pie("By area", cur.areas, PIE.chw)}</div></section>
      <section><div class="row between"><h2>Recent patients</h2><button class="link" data-tab="patients">See all</button></div><div class="stack top">${rows.slice(0, 4).map(patientLine).join("") || `<p class="muted">No patients yet.</p>`}</div></section>`;
  }
  async function renderChw() {
    S.tab = S.tab || "home";
    const box = await Store.all();
    const head = tabs([["home", "Dashboard"], ["screen", "New screening"], ["patients", "My patients"], ["training", S.me.trained ? "Training ✓" : "Training"]]) + (box.length ? `<div class="note warn row between"><span>${box.length} screening${box.length > 1 ? "s" : ""} not sent yet.${S.syncError ? " " + esc(S.syncError) : ""}</span><button class="btn mini" data-act="sync">Send now</button></div>` : "");
    if (S.tab === "home") return renderChwHome(head);
    if (S.tab === "training") return renderTraining(head);
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
    const fd = new FormData(form); S.flow.form = { name: fd.get("name"), birth_year: fd.get("birth_year"), area: fd.get("area"), phone: fd.get("phone"), email: fd.get("email"), sms_consent: fd.get("sms_consent") === "on" };
    const r = fd.get("referto"); if (r) S.flow.choice = r;
    const ok = $("#faceok"); if (ok) S.flow.faceConsent = ok.checked;
  }
  const emailOk = (e) => !e || /^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(String(e).trim());
  const phoneOk = (p) => /^(0[17]\d{8}|(\+?254)[17]\d{8})$/.test(String(p || "").replace(/[\s\-()]/g, ""));
  async function submitPatient() {
    keepForm();
    const f = S.flow, v = f.form, level = worstLevel(f), refer = level !== "rescreen";
    if (!phoneOk(v.phone)) { say("bad", "Enter a Kenyan mobile number, such as 0712 345 678."); return renderChw(); }
    if (!v.area) { say("bad", "Choose the patient's area."); return renderChw(); }
    if (!emailOk(v.email) || f.family.some((m) => !emailOk(m.email))) { say("bad", "Check the email addresses, such as name@example.com, or leave them empty."); return renderChw(); }
    if (f.face && !f.faceConsent) { say("bad", "Confirm the patient agreed to store their face photo, or remove it by starting again."); return renderChw(); }
    if (refer && !f.choice) { say("bad", "Choose a hospital and clinician for this referral."); return renderChw(); }
    for (const m of f.family) if (!m.name || !m.relation || !phoneOk(m.phone)) { say("bad", "Complete each family member's name, relationship and phone number, or remove them."); return renderChw(); }
    const [hospital, clinician] = refer ? f.choice.split("|") : [null, null];
    const eyes = ["right", "left"].filter((k) => f.eyes[k].res).map((k) => { const e = f.eyes[k], r = e.res; return { eye: k, level: r.tri.level, message: r.tri.msg, p_referable: r.p[1], p_urgent: r.p[2], p_quality: r.p[4], retakes: e.retakes, photo: r.photo }; });
    const body = { client_ref: f.ref, screening_consent: f.consent, screened_at: Date.now() / 1000, model: Model.name, eyes, history: f.history, hospital, clinician: clinician ? +clinician : null, face: f.face, face_consent: f.faceConsent, patient: { name: v.name.trim(), birth_year: v.birth_year.trim(), area: v.area, phone: v.phone.trim(), email: (v.email || "").trim(), sms_consent: v.sms_consent }, family: refer ? f.family.map((m) => ({ ...m, receive_sms: true })) : [] };
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
    let held = { rows: [] };
    try { [sum, q, held] = await Promise.all([api("GET", "/api/clinic/summary"), api("GET", "/api/clinic/referrals"), api("GET", "/api/clinic/chw-reports")]); S.queue = q; S.held = held.rows; }
    catch (e) { frame(`<div class="note bad">${esc(e.message)}</div>`, S.me.hospital); return; }
    const strip = `<section class="tiles">${tile(q.waiting.length, "Waiting for review")}${tile(sum.urgent, "Urgent waiting", sum.urgent ? "alert" : "")}${tile(sum.overdue, "Waiting over 3 days", sum.overdue ? "alert" : "")}${tile(sum.confirmed_month, "Confirmed this month")}${tile(q.consultations.length, "Consultation requests")}</section>`;
    const head = `<div class="hosp-title"><h2>${esc(sum.hospital)}</h2><p class="muted">${esc(sum.county)} County · ${esc(S.me.name)}${S.me.staff_id ? " · " + esc(S.me.staff_id) : ""}</p></div>` + strip + tabs([["referrals", "Referrals"], ["consults", `Consultations (${q.consultations.length})`], ["chwreports", `${t("Health worker reports")} (${held.rows.filter((r) => r.waiting).length})`], ["attendance", "Attendance"], ["reviewed", "Reviewed"], ["summary", "Summary"], ["sms", "SMS log"], ["email", "Email log"]]);
    frame(head + `<div id="pane"></div>`, ROLE.clinician);
    const pane = $("#pane");
    try {
      if (S.tab === "summary") {
        const rt = sum.ratings || {};
        pane.className = "stack"; pane.innerHTML = `<section class="card stack"><h2>Patient ratings</h2>${rt.average ? `<p class="big-text" data-notr>★ ${rt.average} · ${rt.count}</p>${rt.recent.map((r) => `<div class="hist" data-notr><p>${"★".repeat(r.stars)} ${esc(r.comment)}</p><p class="small muted">${esc(r.when)}</p></div>`).join("")}` : `<p class="muted">No ratings yet.</p>`}</section><section class="card"><h2>This month</h2><p class="muted small">${sum.referrals_month} referrals received.</p><div class="charts top">${pie("Your decisions", sum.results, PIE.clinic)}${pie("Referrals by area", sum.areas, PIE.clinic)}</div></section><div id="mcheck"></div>`;
        try { $("#mcheck").innerHTML = modelCheck(await api("GET", "/api/clinic/model-check")); } catch (e) { /* shown next time */ }
        return;
      }
      if (S.tab === "email") {
        const r = await api("GET", "/api/clinic/emails");
        pane.className = "stack"; pane.innerHTML = `<form id="myemail" class="card row"><label class="grow">Your email for referral alerts<input name="email" type="email" required value="${esc(S.me.email || "")}"></label><button class="btn">Save email</button></form>${r.rows.map((m) => `<article class="card line"><div><b>${esc(m.to_email)}</b>${m.name ? `<span class="muted"> · ${esc(m.name)}</span>` : ""}<p>${esc(m.subject)}</p><p class="small muted">${esc(m.when)}${m.error ? " · " + esc(m.error) : ""}</p></div><span class="pill ${m.status === "failed" ? "urgent" : "watch"}">${esc(m.status)}</span></article>`).join("") || `<p class="muted">No emails yet.</p>`}`;
        return;
      }
      if (S.tab === "sms") {
        const rows = await api("GET", "/api/clinic/sms");
        pane.className = "stack"; pane.innerHTML = `${rows.map((m) => `<article class="card line"><div><b>${esc(m.to_phone)}</b>${m.name ? `<span class="muted"> · ${esc(m.name)}</span>` : ""}<p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)} · ${m.parts} part${m.parts > 1 ? "s" : ""}</p></div><span class="pill ${m.status === "failed" ? "urgent" : "watch"}">${esc(m.status)}</span>${m.status === "failed" ? `<button class="btn mini" data-retry="${m.id}">Send again</button>` : ""}</article>`).join("") || `<p class="muted">No messages yet.</p>`}`;
        return;
      }
      if (S.tab === "attendance") {
        const rows = await api("GET", "/api/clinic/attendance"), open = rows.filter((a) => !a.attended);
        pane.className = "stack"; pane.innerHTML = `<p class="muted">Mark each past appointment. A missed visit goes to the patient's health worker to follow up.</p>` + (rows.map((a) => `<article class="card line person">${avatar(a.patient_id, false, a.name)}<div><b data-notr>${esc(a.name)}</b> <span class="muted" data-notr>${esc(age(a.birth_year))}${a.village ? " · " + esc(a.village) : ""}</span><p class="small muted" data-notr>${esc(a.when_text)} · ${esc(a.location)}</p></div><div class="stack small-gap end">${a.attended ? `<span class="pill ${a.attended === "attended" ? "ok" : "urgent"}">${esc(t(a.attended === "attended" ? "Attended" : "Missed"))}</span>${a.attended === "attended" ? `<button class="btn mini" data-treatfor="${a.patient_id}" data-tname="${esc(a.name)}" type="button">Record treatment</button>` : ""}` : `<div class="row tight"><button class="btn mini primary" data-attend="${a.id}" data-st="attended" type="button">Attended</button><button class="btn mini danger" data-attend="${a.id}" data-st="missed" type="button">Missed</button></div>`}</div></article>`).join("") || `<p class="muted">No past appointments in the last 4 months.</p>`) + (open.length ? "" : "");
        return;
      }
      if (S.tab === "chwreports") {
        pane.className = "stack"; pane.innerHTML = `<p class="muted">Reports that health workers made for your patients. Check each one, then send it to the patient. The patient doesn't see it before you send it.</p>` + (held.rows.map((r) => `<article class="card line"><div><b data-notr>${esc(r.patient)}</b><p class="small muted"><span data-notr>${esc(r.chw)}</span> · <span data-notr>${esc(r.when)}</span></p>${r.released ? `<p class="small muted">${esc(t("Sent to the patient"))} <span data-notr>${esc(r.released)}</span></p>` : ""}</div><div class="stack small-gap end"><span class="pill ${r.waiting ? "refer" : "ok"}">${esc(r.waiting ? t("Waiting for you") : t("Sent to patient"))}</span><button class="btn mini ${r.waiting ? "primary" : ""}" data-held="${r.id}" type="button">${r.waiting ? "Check and send" : "Open"}</button></div></article>`).join("") || `<p class="muted">No reports from health workers yet.</p>`);
        return;
      }
      if (S.tab === "reviewed") {
        pane.className = "stack"; pane.innerHTML = q.reviewed.map((s) => `<article class="card line person">${avatar(s.patient.id, s.patient.has_face, s.patient.name)}<div><b>${esc(s.patient.name)}</b><p class="small muted">${esc(s.reviewed)} · ${esc(s.specialist_result || "")}</p>${s.appointment ? `<p>${esc(s.appointment.when_text)}, ${esc(s.appointment.location)}</p>` : `<p>${esc(s.review_note)}</p>`}</div><div class="stack small-gap end"><span class="pill ${s.status === "confirmed" ? "ok" : "watch"}">${esc(s.status_text)}</span><button class="btn mini" data-report="${s.patient.id}" type="button">Preview and send PDF report</button><button class="btn mini" data-treatfor="${s.patient.id}" data-tname="${esc(s.patient.name)}" type="button">Record treatment</button>${s.patient.treatments && s.patient.treatments.length ? `<span class="small muted">${esc(t(s.patient.treatments[0].label))}, <span data-notr>${esc(s.patient.treatments[0].when)}</span></span>` : ""}</div></article>`).join("") || `<p class="muted">Nothing reviewed yet.</p>`;
        return;
      }
      if (S.tab === "consults") {
        pane.className = "stack"; pane.innerHTML = q.consultations.map((c) => `<article class="card stack"><div class="row between"><div><b>${esc(c.name)}</b> <span class="muted">${esc(age(c.birth_year))}${c.village ? " · " + esc(c.village) : ""}</span><p class="small muted">Requested ${esc(c.requested)} · prefers ${esc(c.preferred_date)}</p></div></div><p>${esc(c.reason)}</p>
          <div class="three"><label>Date<input type="date" id="c-date-${c.id}" value="${esc(c.preferred_date)}"></label><label>Time<input type="time" id="c-time-${c.id}" value="09:00"></label><label>Note, if you decline<input id="c-note-${c.id}"></label></div><div class="row between"><button class="btn" data-cdecline="${c.id}">Decline</button><button class="btn primary" data-cconfirm="${c.id}">Confirm and send SMS</button></div></article>`).join("") || `<p class="muted">No consultation requests.</p>`;
        return;
      }
      if (!q.waiting.find((s) => s.id === S.selected)) S.selected = q.waiting.length ? q.waiting[0].id : null;
      pane.className = "split";
      pane.innerHTML = `<aside class="queue"><h2>Waiting for review (${q.waiting.length})</h2>${q.waiting.map((s) => `<button class="qitem" data-pick="${s.id}" aria-current="${s.id === S.selected ? "true" : "false"}"><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span><b>${esc(s.patient.name)}</b><span class="small muted">${esc(s.patient.village || "")} ${esc(age(s.patient.birth_year))}</span><span class="small muted">${esc(s.screened)}</span>${s.deadline ? `<span class="small ${s.deadline.days_left <= 4 ? "bad" : "muted"}">${s.deadline.escalated ? esc(t("Reported to the hospital")) : esc(tr("Day {n} of 7").replace("{n}", s.deadline.day))}</span>` : ""}</button>`).join("") || `<p class="muted">No referrals are waiting. They'll appear here when a CHW sends one.</p>`}</aside><div id="review"></div>`;
      if (S.selected) renderReview();
    } catch (e) { pane.className = "note bad"; pane.textContent = e.message; }
  }
  function renderReview() {
    const s = S.queue.waiting.find((x) => x.id === S.selected), b = S.booking[s.id] || (S.booking[s.id] = { date: defaultDate(), time: "09:00", location: S.me.hospital || "Eye clinic", note: "", left: "", right: "" }), box = $("#review"), byEye = Object.fromEntries(s.eyes.map((e) => [e.eye, e]));
    box.innerHTML = `<section class="card"><div class="eye-head"><div class="person">${avatar(s.patient.id, s.patient.has_face, s.patient.name, true)}<div><h2>${esc(s.patient.name)}</h2><p class="muted">${esc(age(s.patient.birth_year))}${s.patient.village ? " · " + esc(s.patient.village) : ""}</p><p class="muted small">Referred by ${esc(s.chw.name)}, ${esc(s.chw.phone)}${s.referred_to ? ` · To ${esc(s.referred_to)}` : ""}</p></div></div><span class="pill ${LEVEL[s.outcome].cls}">${esc(LEVEL[s.outcome].label)}</span></div>
      <p class="top">${esc(s.ai_text)} <span class="muted">This is the AI's first result, from ${esc(s.model || "the phone")}. Please grade the photos yourself.</span></p>
      ${s.deadline ? `<p class="note ${s.deadline.days_left <= 4 ? "bad" : "warn"} top">${s.deadline.escalated ? esc(t("This referral passed 7 days without action, so your hospital was emailed.")) : esc(tr("Please respond by {due}. That's {n} days left.").replace("{due}", s.deadline.due).replace("{n}", s.deadline.days_left))}</p>` : ""}</section>
      <section class="card stack"><div class="row between"><h3>Health history</h3><button class="btn mini" data-report="${s.patient.id}" type="button">Preview and send PDF report</button></div>${historyFacts(s.patient.conditions)}${coverFacts(s.patient.cover)}<h4>Latest readings</h4>${vitalsFacts(s.patient.vitals)}${s.patient.treatments && s.patient.treatments.length ? `<h4>Past treatment</h4>${treatmentFacts(s.patient.treatments)}` : ""}</section>
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
  // ---- Languages. English and Kiswahili are complete; the team fills in the others in translations/translations.csv.
  const LANGS = [["en", "English", "en"], ["sw", "Kiswahili", "sw"], ["ki", "Gĩkũyũ", "ki"], ["luy", "Luluhya", "luy"], ["luo", "Dholuo", "luo"], ["kln", "Kalenjin", "kln"], ["kam", "Kĩkamba", "kam"], ["mas", "Maa", "mas"], ["guz", "Ekegusii", "guz"], ["mer", "Kĩmĩrũ", "mer"], ["tuv", "Ng'aturkana", "tuv"], ["so", "Soomaali", "so"], ["nyf", "Mijikenda", "nyf"]];
  const DICT = window.AFRIEYE_T || {};
  const patternCache = {};
  function patterns(lang) {
    if (patternCache[lang]) return patternCache[lang];
    const out = [];
    for (const [k, v] of Object.entries(DICT[lang] || {})) {
      if (!v || k.indexOf("{") < 0) continue;
      const names = []; const re = new RegExp("^" + k.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{(\w+)\}/g, (_, n) => { names.push(n); return "(.+?)"; }) + "$");
      out.push([re, names, v]);
    }
    return (patternCache[lang] = out);
  }
  function tr(text, lang) {
    lang = lang || S.lang;
    if (!text || lang === "en") return text;
    const d = DICT[lang] || {}, core = text.trim();
    if (!core || /^[\d\s.,:%·/+-]+$/.test(core)) return text;
    let hit = d[core];
    if (!hit) for (const [re, names, v] of patterns(lang)) { const m = core.match(re); if (m) { hit = names.reduce((s, n, i) => s.replace("{" + n + "}", tr(m[i + 1], lang).trim()), v); break; } }
    // Words not translated yet in a Kenyan language show in Kiswahili, which more people know than English.
    if (!hit) { if (window.__missing) window.__missing.add(core); return lang !== "sw" ? tr(text, "sw") : text; }
    return text.replace(core, hit);
  }
  const t = (s) => tr(s);
  function translateTree(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (n) => (n.parentElement && n.parentElement.closest("[data-notr],script,style,textarea") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
    let n;
    while ((n = w.nextNode())) {
      if (n.__en === undefined || (n.nodeValue !== n.__tr && n.nodeValue !== n.__en)) n.__en = n.nodeValue;
      const out = tr(n.__en); n.__tr = out; if (n.nodeValue !== out) n.nodeValue = out;
    }
    root.querySelectorAll && root.querySelectorAll("[placeholder],[aria-label],[title]").forEach((el) => {
      for (const a of ["placeholder", "aria-label", "title"]) {
        if (!el.hasAttribute(a) || el.closest("[data-notr]")) continue;
        const key = "__en_" + a; if (el[key] === undefined || (el.getAttribute(a) !== el["__tr_" + a] && el.getAttribute(a) !== el[key])) el[key] = el.getAttribute(a);
        const out = tr(el[key]); el["__tr_" + a] = out; if (el.getAttribute(a) !== out) el.setAttribute(a, out);
      }
    });
  }
  new MutationObserver(() => translateTree(app)).observe(app, { childList: true, subtree: true });
  function setLang(code, save) {
    S.lang = LANGS.some((l) => l[0] === code) ? code : "en";
    document.documentElement.lang = S.lang;
    try { localStorage.setItem("afrieye.lang", S.lang); } catch (e) { /* stays for this visit only */ }
    if (save && S.me) api("POST", "/api/me/lang", { lang: S.lang }).catch(() => { /* saved on this device anyway */ });
    translateTree(app);
  }
  const langPicker = (id) => `<label class="lang-pick"><span class="sr">Language</span><select id="${id}" data-notr aria-label="Language">${LANGS.map(([c, n]) => `<option value="${c}" ${S.lang === c ? "selected" : ""}>${n}</option>`).join("")}</select></label>`;

  // ---- Reading aloud, with the phone's own voices
  // Voices: a soft Kenyan woman's voice when the phone or browser has one, at the speed the user chooses.
  // Microsoft Edge has Kenyan neural voices (Asilia for English, Zuri for Kiswahili). Android has Kiswahili (Kenya) in Google's voices.
  const FEMALE = /asilia|zuri|female|woman|zira|aria|jenny|sonia|libby|hazel|susan|samantha|karen|moira|tessa|fiona|victoria|serena|catherine|natasha|emma|ava|allison|neerja|veena|rehema|imani|amani|wanjiku|achieng/i;
  const MALE = /chilemba|rafiki|\bmale\b|\bman\b|david|mark|george|james|daniel|guy|ryan|thomas|alex|fred|arthur|oliver|liam|william/i;
  function voiceScore(v, code) {
    const lang = v.lang.toLowerCase().replace("_", "-");
    if (!lang.startsWith(code)) return -1;
    let sc = 10;
    if (lang.endsWith("-ke")) sc += 40;                       // Kenyan accent first
    else if (code === "en" && /-(tz|ug|ng|za|gh)$/.test(lang)) sc += 15;  // then another African English accent
    if (FEMALE.test(v.name)) sc += 20; else if (MALE.test(v.name)) sc -= 20;
    if (/natural|neural|online|enhanced|premium/i.test(v.name)) sc += 8;  // smoother voices sound softer
    return sc;
  }
  function voiceFor(code) {
    const vs = window.speechSynthesis ? speechSynthesis.getVoices() : [];
    if (PREFS.voice) { const own = vs.find((v) => v.name === PREFS.voice && v.lang.toLowerCase().startsWith(code)); if (own) return own; }
    let best = null, top = -1;
    for (const v of vs) { const sc = voiceScore(v, code); if (sc > top) { top = sc; best = v; } }
    return best;
  }
  function speak(text) {
    if (!("speechSynthesis" in window)) return toast(t("This phone can't read aloud."));
    speechSynthesis.cancel();
    const name = (LANGS.find((l) => l[0] === S.lang) || LANGS[0])[1];
    let v = voiceFor(LANGS.find((l) => l[0] === S.lang)[2]);
    if (!v && S.lang !== "en") {
      const sw = voiceFor("sw");
      toast(sw ? tr("This phone has no {language} voice, so it reads with a Kiswahili voice.").replace("{language}", name) : tr("This phone has no {language} voice, so it reads with its default voice.").replace("{language}", name));
      v = sw;
    }
    const u = new SpeechSynthesisUtterance(text); if (v) { u.voice = v; u.lang = v.lang; }
    u.rate = +PREFS.rate || 0.85; u.pitch = +PREFS.pitch || 1.1; u.volume = 1;   // a little slower and lighter than normal, so it sounds gentle
    speechSynthesis.speak(u);
  }
  if (window.speechSynthesis) speechSynthesis.onvoiceschanged = () => { if (S.view === "settings") fillVoices(); };
  function fillVoices() {
    const sel = $("#voice-pick"); if (!sel) return;
    const vs = speechSynthesis.getVoices().filter((v) => /^(en|sw)/i.test(v.lang)).sort((a, b) => Math.max(voiceScore(b, "en"), voiceScore(b, "sw")) - Math.max(voiceScore(a, "en"), voiceScore(a, "sw")));
    const ke = vs.some((v) => /-ke$/i.test(v.lang.replace("_", "-")));
    sel.innerHTML = `<option value="">${esc(t("Automatic: a Kenyan woman's voice if this device has one"))}</option>` + vs.map((v) => `<option value="${esc(v.name)}" ${PREFS.voice === v.name ? "selected" : ""}>${esc(v.name)} (${esc(v.lang)})${/-ke$/i.test(v.lang.replace("_", "-")) ? " 🇰🇪" : ""}</option>`).join("");
    const n = $("#voice-note"); if (n) n.hidden = ke;
  }
  function speakEl(sel) {
    const el = $(sel); if (!el) return;
    const c = el.cloneNode(true); c.querySelectorAll("button,select,input,form,.no-read").forEach((x) => x.remove());
    speak(c.textContent.replace(/\s+/g, " ").trim());
  }
  const readBtn = (sel) => `<button class="btn mini read" data-speak="${sel}" type="button">${ICON.speaker}Read aloud</button>`;
  function toast(msg) {
    let el = $("#toast"); if (!el) { el = document.createElement("div"); el.id = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = msg; el.className = "show"; clearTimeout(toast.t); toast.t = setTimeout(() => { el.className = ""; }, 5000);
  }
  const ICON = {
    bell: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z M10 20a2 2 0 0 0 4 0" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
    gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    speaker: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor"/><path d="M16 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    chat: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  };

  // ---- Settings that stay on this device
  const PREFS = { mode: "light", size: "normal", font: "standard", rate: 0.85, pitch: 1.1, voice: "" };
  function loadPrefs() { try { Object.assign(PREFS, JSON.parse(localStorage.getItem("afrieye.prefs") || "{}")); } catch (e) { /* defaults */ } applyPrefs(); }
  function applyPrefs() { const r = document.documentElement; r.dataset.mode = PREFS.mode; r.dataset.size = PREFS.size; r.dataset.font = PREFS.font; try { localStorage.setItem("afrieye.prefs", JSON.stringify(PREFS)); } catch (e) { /* this visit only */ } }

  // ---- Alerts
  async function pollAlerts() {
    if (!S.me || S.offline) return;
    try {
      const r = await api("GET", "/api/notifications"); const b = $("#bell-count");
      if (b) { b.textContent = r.unread || ""; b.hidden = !r.unread; }
      const newest = r.items[0];
      if (newest && !newest.read && S.lastAlert !== newest.id && S.lastAlert !== undefined && "Notification" in window && Notification.permission === "granted") new Notification(tr(newest.title), { body: newest.body, icon: "/static/logo_mark_192.png" });
      S.lastAlert = newest ? newest.id : null;
    } catch (e) { /* try again later */ }
  }
  setInterval(pollAlerts, 60000);
  async function renderAlerts() {
    frame(`<div class="vstack"><div class="row between"><h2>Alerts</h2><button class="btn" data-act="close-view">Back</button></div><div id="alist" class="stack muted">Loading…</div></div>`);
    try {
      const r = await api("GET", "/api/notifications"); await api("POST", "/api/notifications/read", {});
      const b = $("#bell-count"); if (b) b.hidden = true;
      $("#alist").className = "stack"; $("#alist").innerHTML = r.items.map((n) => `<article class="card ${n.read ? "" : "unread"}"><b>${esc(n.title)}</b><p data-notr>${esc(n.body)}</p><p class="small muted">${esc(n.when)}</p></article>`).join("") || `<p class="muted">No alerts yet.</p>`;
    } catch (e) { $("#alist").textContent = e.message; }
  }

  // ---- The built-in assistant
  function kbAnswer(q) {
    const role = S.me.role, lang = S.lang === "en" ? "en" : "sw", norm = (x) => " " + x.toLowerCase().replace(/[^\p{L}\p{N}' ]+/gu, " ").replace(/\s+/g, " ") + " ";
    const text = norm(q); let best = null, score = 0;
    for (const e of window.AFRIEYE_KB || []) {
      if (!e.roles.includes(role)) continue;
      let s = 0; for (const k of e.keys) if (text.includes(norm(k).trimEnd())) s += k.length > 4 ? 2 : 1;
      if (e.urgent && s) s += 5;
      if (s > score) { score = s; best = e; }
    }
    if (!best) return lang === "sw" ? "Sina jibu la swali hilo bado. Kwa tatizo la macho, wasiliana na mhudumu wako wa afya au kliniki ya macho. Ukipoteza uwezo wa kuona ghafla, nenda hospitali iliyo karibu leo." : "I don't have an answer to that yet. For an eye problem, contact your health worker or eye clinic. If you suddenly lose sight, go to the nearest hospital today.";
    return best.a[lang];
  }
  function renderAssistant() {
    const lang = S.lang === "en" ? "en" : "sw", qs = (window.AFRIEYE_KB || []).filter((e) => e.roles.includes(S.me.role)).slice(0, 8);
    S.chat = S.chat || [];
    frame(`<div class="vstack"><div class="row between"><h2>Ask AfriEye</h2><button class="btn" data-act="close-view">Back</button></div>
      <p class="muted">Ask about eye health, diabetes or using AfriEye. Answers come from AfriEye's prepared guidance, and work without internet.</p>
      ${S.lang !== "en" && S.lang !== "sw" ? `<p class="small muted">Answers are in Kiswahili for now.</p>` : ""}
      <div class="chips" data-notr>${qs.map((e, i) => `<button class="chip" data-askq="${i}" type="button">${esc(e.q[lang])}</button>`).join("")}</div>
      <div class="chat" id="chat" data-notr>${S.chat.map((m, i) => m.me ? `<p class="bubble me">${esc(m.text)}</p>` : `<div class="bubble ai" id="ans-${i}"><p>${esc(m.text)}</p><button class="btn mini read" data-speak="#ans-${i}" type="button">${ICON.speaker}${lang === "sw" ? "Soma kwa sauti" : "Read aloud"}</button></div>`).join("")}</div>
      <form id="askform" class="row"><label class="grow"><span class="sr">Your question</span><input name="q" required autocomplete="off" placeholder="Type your question"></label><button class="btn primary">Ask</button></form></div>`);
    const c = $("#chat"); if (c && c.lastElementChild) c.lastElementChild.scrollIntoView({ block: "nearest" });
  }
  function ask(q) { S.chat = (S.chat || []).concat([{ me: true, text: q }, { text: kbAnswer(q) }]).slice(-20); renderAssistant(); }

  // ---- Settings page
  const HELP = {
    chw: ["Dashboard: your scans by day, month and year, home visit requests and your patients.", "New screening: get the patient's agreement, record their health history, then take or upload a photo of each eye.", "The AI scans the photo on the phone. Follow the next step it shows.", "For a referral, choose the nearest hospital and clinician, then send it. If there's no internet, it sends later.", "My patients: see each specialist decision, and send a PDF report to the patient."],
    clinician: ["Referrals: urgent cases come first. You have 7 days to respond, and get an alert after 3 days.", "Open a referral to see the photos, AI scores and health history. Grade each eye, then confirm the appointment or say no visit is needed.", "Consultations: requests that patients made in the app or by USSD.", "Reviewed: past decisions, with a button to send a PDF report to the patient.", "Summary: your decisions, referrals by area and your patient ratings."],
    member: ["Your result and next appointment are at the top. Press Read aloud to hear them.", "Set a reminder for an appointment by app alert, SMS or email.", "Ask a health worker to visit you, or book a consultation with an eye doctor.", "Reports: open, share or email the reports your health worker or doctor sends.", "Rate your care, and choose which family members can see your record.", "Without internet, dial the AfriEye USSD code on any phone."],
  };
  async function renderSettings() {
    const u = S.me, box = u.role === "chw" ? await Store.all() : [];
    frame(`<div class="vstack"><div class="row between"><h2>Settings</h2><button class="btn" data-act="close-view">Back</button></div>
      <section class="card stack"><h3>Language and voice</h3>${langPicker("lang-settings")}<p class="small muted">Words not translated yet into your language show in Kiswahili. Read aloud uses this phone's voices.</p><label>Voice<select id="voice-pick" data-notr></select></label>
        <div class="two"><label>Speed <output id="rate-out">${speedName(PREFS.rate)}</output><input type="range" id="pref-rate" min="0.6" max="1.3" step="0.05" value="${PREFS.rate}"></label>
        <label>Softness <output id="pitch-out">${PREFS.pitch > 1.15 ? t("Lighter") : PREFS.pitch < 0.95 ? t("Deeper") : t("Normal")}</output><input type="range" id="pref-pitch" min="0.8" max="1.4" step="0.05" value="${PREFS.pitch}"></label></div>
        <div id="voice-note" class="note warn small" hidden>This device has no Kenyan voice yet. On a laptop, open AfriEye in Microsoft Edge to get the Kenyan voices Asilia (English) and Zuri (Kiswahili). On Android, open Settings, then Text-to-speech, choose Speech Services by Google and install Kiswahili (Kenya).</div>
        <button class="btn" data-act="test-voice" type="button">${ICON.speaker}Test the voice</button></section>
      <section class="card stack"><h3>Appearance</h3>
        <fieldset><legend>Theme</legend><div class="row">${[["light", "Light"], ["dark", "Dark"], ["auto", "Follow this phone"]].map(([k, n]) => `<label class="check"><input type="radio" name="pref-mode" value="${k}" ${PREFS.mode === k ? "checked" : ""}> ${n}</label>`).join("")}</div></fieldset>
        <fieldset><legend>Text size</legend><div class="row">${[["normal", "Normal"], ["large", "Large"], ["xl", "Extra large"]].map(([k, n]) => `<label class="check"><input type="radio" name="pref-size" value="${k}" ${PREFS.size === k ? "checked" : ""}> ${n}</label>`).join("")}</div></fieldset>
        <fieldset><legend>Font</legend><div class="row">${[["standard", "Easy to read"], ["times", "Times New Roman"]].map(([k, n]) => `<label class="check"><input type="radio" name="pref-font" value="${k}" ${PREFS.font === k ? "checked" : ""}> ${n}</label>`).join("")}</div></fieldset></section>
      <section class="card stack"><h3>Alerts</h3><p class="muted">Show alerts on this device, even when you're on another page.</p><button class="btn" data-act="allow-alerts" type="button">Allow alerts on this device</button></section>
      <section class="card stack"><h3>Account</h3><p data-notr>${esc(u.name)} · ${esc(u.phone)}</p>
        <form id="myemail" class="row"><label class="grow">Email<input name="email" type="email" value="${esc(u.email || "")}" placeholder="name@example.com"></label><button class="btn">Save email</button></form>
        <form id="pwform" class="stack"><h4>Change password</h4><div class="two"><label>Current password<input name="current" type="password" autocomplete="current-password" required></label><label>New password, at least 8 characters<input name="new" type="password" autocomplete="new-password" minlength="8" required></label></div><button class="btn">Change password</button></form>
        <button class="btn" data-act="logout" type="button">Sign out</button></section>
      <section class="card stack"><h3>How to use AfriEye</h3><ol class="todo">${HELP[u.role].map((h) => `<li>${esc(h)}</li>`).join("")}</ol></section>
      <section class="card stack"><h3>Your data</h3>
        <div class="row"><button class="btn" data-act="export" type="button">Download a copy of my data</button></div>
        ${u.role === "chw" ? `<p class="muted">${box.length} screening${box.length === 1 ? "" : "s"} saved on this phone and not sent yet.</p><div class="row"><button class="btn" data-act="backup-device" type="button">Back up screenings on this phone</button><label class="btn" tabindex="0"><input type="file" accept="application/json,.json" id="restore-file" hidden>Restore from a backup</label><button class="btn danger" data-act="clear-device" type="button">Delete data saved on this phone</button></div>` : ""}
        ${u.role === "member" ? `<form id="delform" class="stack"><h4>Delete my account and records</h4><p class="muted">This permanently deletes your eye records, photos, reports and appointments. It can't be undone.</p><label>Type your password to confirm<input name="password" type="password" required></label><button class="btn danger">Delete everything</button></form>` : `<p class="small muted">All records are backed up automatically each day. To restore a backup, ask your administrator.</p>`}
      </section></div>`);
      fillVoices();
  }
  const speedName = (r) => (r <= 0.75 ? t("Slow") : r >= 1.05 ? t("Fast") : t("Normal"));
  function download(name, blob) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000); }

  // ---- Shared pieces for health history, reports and reviews
  const CONDS = ["High blood pressure", "Kidney disease", "Heart disease", "High cholesterol", "Cataract", "Glaucoma", "Previous eye surgery or laser", "Pregnancy", "HIV", "Smoking"];
  const DTYPES = ["Type 1", "Type 2", "Not sure"];
  const COVERS = [["sha", "SHA (Social Health Authority)"], ["ngo", "NGO or charity support"], ["private", "Private insurance"], ["none", "No cover"], ["unsure", "Not sure"]];
  const SHA_HOW = "To apply for SHA yourself, dial *147# or visit afyayangu.go.ke. You need your national ID number.";
  function coverFacts(cv) {
    if (!cv || !cv.cover) return `<p class="muted">Health cover not recorded.</p>`;
    const help = { requested: t("Asked a health worker for help to apply for SHA"), helping: cv.helper ? tr("{name} is helping them apply for SHA").replace("{name}", cv.helper.name) : t("A health worker is helping them apply for SHA"), registered: t("Registered for SHA with a health worker's help") }[cv.help];
    return `<dl class="facts one"><div><dt>Health cover</dt><dd>${esc(t(cv.label))}${cv.ref ? ` · <span data-notr>${esc(cv.ref)}</span>` : ""}</dd></div>${help ? `<div><dt>SHA application</dt><dd data-notr>${esc(help)}</dd></div>` : ""}</dl>`;
  }
  function historyCard(f) {
    const h = f.history;
    return `<section class="card stack"><h2>Health history</h2><p class="muted small">Record this before the eye photos. The eye specialist and the patient will see it.</p>
      <fieldset><legend>Type of diabetes</legend><div class="row">${DTYPES.map((d) => `<label class="check"><input type="radio" name="dtype" data-hist="diabetes_type" value="${d}" ${h.diabetes_type === d ? "checked" : ""}> ${d}</label>`).join("")}</div></fieldset>
      <div class="two"><label>Years with diabetes<input data-hist="years" inputmode="numeric" maxlength="2" value="${esc(h.years)}"></label><label class="check pad-top"><input type="checkbox" data-hist="insulin" ${h.insulin ? "checked" : ""}> Uses insulin</label></div>
      <fieldset><legend>Other conditions</legend><div class="conds">${CONDS.map((c) => `<label class="check"><input type="checkbox" data-cond="${c}" ${h.items.includes(c) ? "checked" : ""}> ${c}</label>`).join("")}</div></fieldset>
      <label>Other notes, such as medicines<textarea data-hist="notes" rows="2" maxlength="300">${esc(h.notes)}</textarea></label>
      <fieldset><legend>Today's readings, optional</legend><div class="two"><label>Blood sugar, mmol/L<input data-hist="glucose" inputmode="decimal" maxlength="4" value="${esc(h.glucose || "")}"></label><label>Taken<select data-hist="glucose_kind"><option value="random" ${h.glucose_kind !== "fasting" ? "selected" : ""}>Any time</option><option value="fasting" ${h.glucose_kind === "fasting" ? "selected" : ""}>Fasting</option></select></label></div>
        <div class="three"><label>Blood pressure, top<input data-hist="sys" inputmode="numeric" maxlength="3" value="${esc(h.sys || "")}" placeholder="130"></label><label>Bottom<input data-hist="dia" inputmode="numeric" maxlength="3" value="${esc(h.dia || "")}" placeholder="80"></label><label>HbA1c, %<input data-hist="hba1c" inputmode="decimal" maxlength="4" value="${esc(h.hba1c || "")}"></label></div></fieldset>
      <fieldset><legend>Health cover for eye care</legend><div class="conds">${COVERS.map(([k, n]) => `<label class="check"><input type="radio" name="cover" data-hist="cover" value="${k}" ${h.cover === k ? "checked" : ""}> ${n}</label>`).join("")}</div></fieldset>
      ${h.cover === "sha" || h.cover === "ngo" || h.cover === "private" ? `<label>${h.cover === "sha" ? "SHA number, if they have it" : h.cover === "ngo" ? "Name of the NGO or charity" : "Insurance company"}<input data-hist="cover_ref" maxlength="40" value="${esc(h.cover_ref || "")}"></label>` : ""}
      ${h.cover === "none" || h.cover === "unsure" ? `<div class="note warn stack small-gap"><label class="check"><input type="checkbox" data-hist="cover_help" ${h.cover_help ? "checked" : ""}> I will help them apply for SHA</label><p class="small">${SHA_HOW}</p></div>` : ""}</section>`;
  }
  function historyFacts(c) {
    if (!c) return `<p class="muted">Not recorded.</p>`;
    const dia = [t(c.diabetes_type), c.years !== null && c.years !== undefined ? tr("{n} years").replace("{n}", c.years) : null, c.insulin ? t("uses insulin") : null].filter(Boolean).join(", ");
    return `<dl class="facts one"><div><dt>Diabetes</dt><dd data-notr>${esc(dia)}</dd></div><div><dt>Other conditions</dt><dd data-notr>${esc(c.items.length ? c.items.map(t).join(", ") : t("None recorded"))}</dd></div>${c.notes ? `<div><dt>Notes</dt><dd data-notr>${esc(c.notes)}</dd></div>` : ""}</dl>`;
  }
  // A report is always shown to the CHW or clinician first. It's only sent from the preview.
  // A CHW's report goes to the patient's eye specialist, who checks it and sends it to the patient.
  function pdfModal(pdf, title, note, send) {
    const url = pdf.url;
    closePreview();
    const box = document.createElement("div"); box.className = "pv-back"; box.id = "pv";
    box.innerHTML = `<div class="pv" role="dialog" aria-modal="true" aria-labelledby="pv-title"><div class="pv-head"><h2 id="pv-title">${title}</h2><button class="icon-btn pv-x" data-pv="close" type="button" aria-label="Close">✕</button></div>
      <p class="muted small">${note}</p>
      <div class="pv-frame" id="pv-pages" tabindex="0" aria-label="Report preview"><p class="muted pv-wait">Loading the report…</p></div>
      <p class="small muted pv-hint"><a href="${url}" target="_blank" rel="noopener">Open the PDF</a> in a new tab to zoom in.</p>
      <div class="row between pv-actions"><button class="btn" data-pv="close" type="button">${send ? "Cancel" : "Close"}</button>${send || ""}</div></div>`;
    box.dataset.url = url; document.body.appendChild(box); translateTree(box); (box.querySelector(".pv-actions .primary") || box.querySelector("[data-pv=close]")).focus();
    drawPdf(pdf.bytes, $("#pv-pages"));
  }
  async function pdfUrl(path) {
    const r = await fetch(path, { credentials: "same-origin" });
    if (!r.ok) { let m = t("The report couldn't be made."); try { m = (await r.json()).error || m; } catch (e) { /* keep the plain message */ } throw new Error(m); }
    const bytes = new Uint8Array(await r.arrayBuffer());
    return { url: URL.createObjectURL(new Blob([bytes], { type: "application/pdf" })), bytes };
  }
  async function previewReport(pid, btn) {
    if (btn) btn.disabled = true;
    try {
      const url = await pdfUrl(`/api/patients/${pid}/reports/preview`), chw = S.me.role === "chw";
      pdfModal(url, "Check the report before sending",
        chw ? "This report goes to the patient's eye specialist first. They check it, then send it to the patient." : "This is exactly what the patient will get. Read it, then send it or close it to make changes.",
        `<button class="btn primary" data-pv="send" data-pid="${pid}" type="button">${chw ? "Send to the eye specialist" : "Send to patient"}</button>`);
    } catch (e) { toast(tr(e.message)); }
    if (btn) btn.disabled = false;
  }
  async function openHeldReport(r, btn) {
    if (btn) btn.disabled = true;
    try {
      const url = await pdfUrl(`/api/reports/${r.id}/pdf`);
      pdfModal(url, r.waiting ? "Check this report before it goes to the patient" : "Report from a health worker",
        `${esc(t("Prepared by {name}, health worker, for {patient}.").replace("{name}", r.chw).replace("{patient}", r.patient))}${r.waiting ? " " + esc(t("The patient only sees it after you send it.")) : ""}`,
        r.waiting ? `<button class="btn primary" data-pv="release" data-rid="${r.id}" type="button">Send to patient</button>` : "");
    } catch (e) { toast(tr(e.message)); }
    if (btn) btn.disabled = false;
  }
  // Draws each PDF page as a picture, so the preview works on Android phones too (their browsers can't show a PDF inside a page).
  async function drawPdf(bytes, box) {
    try {
      const pdfjs = await import("/static/vendor/pdfjs/pdf.min.mjs");
      pdfjs.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.mjs";
      const doc = await pdfjs.getDocument({ data: bytes.slice(), isEvalSupported: false }).promise;
      box.innerHTML = "";
      for (let i = 1; i <= doc.numPages; i++) {
        if (!box.isConnected) return;
        const page = await doc.getPage(i), w = Math.max(320, box.clientWidth - 16), base = page.getViewport({ scale: 1 }), scale = (w / base.width) * Math.min(2, window.devicePixelRatio || 1);
        const vp = page.getViewport({ scale }), c = document.createElement("canvas"); c.width = vp.width; c.height = vp.height; c.className = "pv-page"; c.setAttribute("role", "img"); c.setAttribute("aria-label", t("Report page {n} of {m}").replace("{n}", i).replace("{m}", doc.numPages));
        box.appendChild(c); await page.render({ canvasContext: c.getContext("2d"), viewport: vp }).promise;
      }
    } catch (e) { box.innerHTML = `<p class="muted pv-wait">${esc(t("The preview couldn't be shown here. Use Open the PDF below."))}</p>`; }
  }
  function closePreview() { const b = $("#pv"); if (b) { URL.revokeObjectURL(b.dataset.url); b.remove(); } }
  async function sendReport(pid, btn) {
    if (btn) btn.disabled = true;
    try { const r = await api("POST", `/api/patients/${pid}/reports`, {}); closePreview(); toast(r.to ? tr("Report sent to {name}. They'll check it and send it to the patient.").replace("{name}", r.to) : t("Report sent to the patient's dashboard.")); } catch (e) { toast(tr(e.message)); if (btn) btn.disabled = false; }
  }
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-pv]"); if (!b && ev.target.id === "pv") return closePreview(); if (!b) return;
    if (b.dataset.pv === "close") return closePreview();
    if (b.dataset.pv === "send") return sendReport(b.dataset.pid, b);
    if (b.dataset.pv === "release") { b.disabled = true; try { await api("POST", `/api/reports/${b.dataset.rid}/release`, {}); closePreview(); toast(t("Report sent to the patient's dashboard.")); if (S.me.role === "clinician") renderClinic(); } catch (e) { toast(tr(e.message)); b.disabled = false; } }
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closePreview(); });
  async function reportFile(id) {
    const r = await fetch(`/api/reports/${id}/pdf`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(t("The report couldn't be opened."));
    return new File([await r.blob()], `afrieye-report-${id}.pdf`, { type: "application/pdf" });
  }

  // ---- Patient and family: a vertical menu, and the chosen page beside it (or on its own on a phone)
  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const PICON = {
    home: svg("M3 11l9-7 9 7M5 10v10h5v-6h4v6h5V10"),
    profile: svg("M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0"),
    appointments: svg("M4 6h16v14H4zM4 10h16M8 3v5M16 3v5"),
    reminders: svg("M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM12 9v4l3 2M5 3L2 6M19 3l3 3"),
    emergency: svg("M12 3l10 18H2zM12 10v5M12 18v.5"),
    reports: svg("M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h7"),
    care: svg("M12 3l2.8 5.8 6.2.9-4.5 4.4 1 6.2L12 17.4 6.5 20.3l1-6.2L3 9.7l6.2-.9z"),
    family: svg("M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2 20a7 7 0 0 1 14 0M16 4.5a3.5 3.5 0 0 1 0 6.5M18 13.5a6 6 0 0 1 4 6.5"),
    messages: svg("M3 5h18v14H3zM3 6l9 7 9-7"),
    assistant: svg("M4 5h16v11H9l-5 4z"),
    settings: svg("M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"),
    menu: svg("M4 6h16M4 12h16M4 18h16"),
    visit: svg("M3 21h18M5 21V11l7-5 7 5v10M10 21v-5h4v5M12 2v2"),
    readings: svg("M3 12h4l3-8 4 16 3-8h4"),
    cover: svg("M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4"),
    logout: svg("M14 4h5v16h-5M10 8l-4 4 4 4M6 12h11"),
  };
  async function renderFamily(pid) {
    const list = S.me.patients || [];
    if (!list.length) { frame(`<section class="card narrow"><h2>No record yet</h2><p>Your record appears here after a health worker adds you, or a patient adds you as family.</p></section>`); return; }
    pid = pid || S.patient || list[0].id; S.patient = pid;
    frame(`<div id="rec" class="muted">Loading…</div>`, list.find((p) => p.id === pid).access === "owner" ? ROLE.member : "Family member");
    let d; try { d = await api("GET", `/api/patients/${pid}`); } catch (e) { $("#rec").className = "note bad"; $("#rec").textContent = e.message; return; }
    const st = d.status, next = d.next_appointment, owner = d.access === "owner", P = d.patient, future = d.appointments.filter((a) => a.upcoming).sort((a, b) => (a.when < b.when ? -1 : 1)), past = d.appointments.filter((a) => !a.upcoming);
    const cls = st ? (st.level === "urgent" ? "urgent" : st.level === "refer" ? "refer" : st.level === "ok" ? "ok" : "watch") : "watch";
    const hospitals = nearest(P.area_id, S.places.hospitals), hlist = hospitals.length ? hospitals : S.places.hospitals, waitingConsult = d.consultations.some((c) => c.status === "requested");
    const rems = next ? d.reminders[next.id] || [] : [];
    const km = (h) => (h.km !== undefined ? `, ${h.km < 10 ? h.km.toFixed(1) : Math.round(h.km)} km` : "");
    const chooser = list.length > 1 ? `<label>Whose record?<select id="who" data-notr>${list.map((p) => `<option value="${p.id}" ${p.id === pid ? "selected" : ""}>${esc(p.name)}${p.relation && p.relation !== "Self" ? ` (${esc(p.relation)})` : ""}</option>`).join("")}</select></label>` : "";

    const MENU = [
      ["home", "My eye health"], ["profile", "Profile"], ["appointments", "Appointments"], ["reminders", "Reminders"], ...(owner ? [["visit", "Request a CHW visit"]] : []), ["cover", "Health cover"], ["readings", "Health readings"], ["emergency", "Emergency"], ["reports", "Reports"],
      ...(owner ? [["care", "Rate your care"], ["family", "Family"]] : []), ["messages", "Messages"],
    ];
    const keys = MENU.map((m) => m[0]); if (S.ptab && !keys.includes(S.ptab)) S.ptab = null;
    const cur = S.ptab || "home";
    const count = { reports: d.reports.length, appointments: future.length, reminders: rems.length };

    const PAGES = {
      home: () => `<section class="result ${cls}" id="res-card"><p class="eyebrow">${st ? `From your eye specialist, <span data-notr>${esc(st.date)}</span>` : "Your eye health"}</p><h2>${esc(st ? st.label : "No specialist result yet")}</h2><p>${esc(st ? st.advice : "Your result appears here after an eye specialist reviews your photos.")}</p>${st && st.note ? `<p class="small" data-notr>${esc(st.note)}</p>` : ""}<div>${readBtn("#res-card")}</div></section>
        <section class="card stack" id="next-card"><h3>Next appointment</h3>${next ? `<p class="big-text" data-notr>${esc(next.when_text)}</p><p data-notr>${esc(next.location)}</p><p class="muted" data-notr>${esc(next.doctor)}</p><div class="row">${readBtn("#next-card")}<button class="btn mini" data-ptab="reminders" type="button">Set a reminder</button></div>` : `<p class="muted">None booked.</p><div><button class="btn mini" data-ptab="appointments" type="button">Book a consultation</button></div>`}</section>
        ${d.recall ? `<section class="card stack"><h3>Next eye check</h3><p class="big-text ${d.recall.overdue ? "bad" : ""}" data-notr>${esc(d.recall.due_text)}</p><p class="muted">${esc(d.recall.overdue ? t("Your eye check is overdue. Ask your health worker to visit you.") : d.recall.reason === "early" ? t("Early changes were seen, so check again after 6 months.") : t("People with diabetes need an eye check every year."))}</p>${d.recall.overdue && owner ? `<div><button class="btn mini" data-ptab="visit" type="button">Request a CHW visit</button></div>` : ""}</section>` : ""}
        ${d.treatments.length ? `<section class="card stack"><h3>Your treatment</h3>${treatmentFacts(d.treatments.slice(0, 2))}</section>` : ""}
        <section class="card stack tip" id="tip-card"><h3>Tip of the week</h3><p data-notr>${esc(S.lang === "en" ? d.tip.en : d.tip.sw)}</p><div class="row">${readBtn("#tip-card")}</div>${owner ? `<label class="check small no-read"><input type="checkbox" id="tipsms" ${d.tips_on ? "checked" : ""} ${P.sms_consent ? "" : "disabled"}> Send me a tip like this every week by SMS</label>` : ""}</section>
        ${d.screenings.length ? `<section class="card"><h3>Eye screenings</h3>${d.screenings.map((s) => `<div class="hist"><p>${esc(s.text)}</p><p class="small muted"><span data-notr>${esc(s.date)}</span> · ${esc(s.status)}</p></div>`).join("")}<p class="small muted top">The AI gives a first result. It isn't a diagnosis.</p></section>` : ""}`,
      profile: () => `${chooser}<section class="card profile">${avatar(P.id, P.has_face, P.name, true)}<div class="grow"><h2 data-notr>${esc(P.name)}</h2><p class="muted"><span>${esc(age(P.birth_year))}</span>${P.village ? ` · <span data-notr>${esc(P.village)}${P.county ? ", " + esc(P.county) : ""}</span>` : ""}</p>
          ${owner ? `<div class="row top"><label class="btn mini" tabindex="0"><input type="file" accept="image/*" capture="user" data-face="own" hidden>Take photo</label><label class="btn mini" tabindex="0"><input type="file" accept="image/*" data-face="own" hidden>Upload photo</label>${P.has_face ? `<button class="link" data-act="face-del">Remove</button>` : ""}</div>` : ""}</div></section>
        <section class="card stack"><h3>Your details</h3><dl class="facts one"><div><dt>Condition</dt><dd>${esc(d.condition ? d.condition : "Waiting for a specialist result")}</dd></div><div><dt>Eye doctor</dt><dd data-notr>${esc(d.doctor || t("Not assigned yet"))}</dd></div><div><dt>Community health worker</dt><dd data-notr>${d.chw ? `${esc(d.chw.name)}, ${esc(d.chw.phone)}` : esc(t("Not assigned yet"))}</dd></div><div><dt>Phone</dt><dd data-notr>${esc(P.phone)}</dd></div></dl>
          ${owner ? `<label>Your location<select id="myarea">${areaOptions(P.area_id)}</select></label><label class="check"><input type="checkbox" id="smsok" ${P.sms_consent ? "checked" : ""}> Send me appointment and result SMS and email</label>` : ""}</section>
        <section class="card stack"><h3>Health history</h3>${historyFacts(P.conditions)}${coverFacts(d.cover)}</section>`,
      appointments: () => `<section class="card stack" id="appt-card"><h3>Next appointment</h3>${next ? `<p class="big-text" data-notr>${esc(next.when_text)}</p><p data-notr>${esc(next.location)}</p><p class="muted" data-notr>${esc(next.doctor)}</p><div>${readBtn("#appt-card")}</div>` : `<p class="muted">None booked.</p>`}</section>
        ${future.length > 1 || past.length ? `<section class="card stack"><h3>All appointments</h3>${future.map((a) => `<p><b data-notr>${esc(a.when_text)}</b> · <span data-notr>${esc(a.location)}${a.doctor ? ", " + esc(a.doctor) : ""}</span></p>`).join("")}${past.length ? `<h4>Past</h4>${past.slice(0, 5).map((a) => `<p class="small muted" data-notr>${esc(a.when_text)}, ${esc(a.location)}${a.doctor ? ", " + esc(a.doctor) : ""}</p>`).join("")}` : ""}</section>` : ""}
        ${owner ? `<section class="card stack"><h3>Book a consultation</h3>${waitingConsult ? `<p class="muted">Your request is waiting for the clinic. They'll reply by SMS.</p>` : `<form id="consult" class="stack"><label>Hospital, nearest first<select name="hospital" required data-notr>${hlist.map((h) => `<option value="${h.id}">${esc(h.name)}${km(h)}</option>`).join("")}</select></label><label>Preferred date<input type="date" name="date" required value="${defaultDate()}"></label><label>What would you like to discuss?<textarea name="reason" rows="2" maxlength="300" required></textarea></label><button class="btn primary">Request consultation</button></form>`}
          ${d.consultations.map((c) => `<div class="hist"><div class="row between"><b data-notr>${esc(c.hospital)}</b><span class="pill ${c.status === "confirmed" ? "ok" : c.status === "declined" ? "urgent" : "watch"}">${esc({ requested: "Waiting", confirmed: "Confirmed", declined: "Declined" }[c.status])}</span></div><p class="small muted" data-notr>${esc(c.reason)}${c.note ? ` · ${esc(c.note)}` : ""}</p></div>`).join("")}</section>` : ""}`,
      reminders: () => `<section class="card stack"><h3>For your next appointment</h3>${next ? `<p><b data-notr>${esc(next.when_text)}</b> · <span data-notr>${esc(next.location)}</span></p>
          ${rems.map((r) => `<div class="row between"><span><span data-notr>${esc(r.at)}</span> · ${r.channels.map((c) => esc({ app: t("App alert"), sms: "SMS", email: t("Email") }[c])).join(", ")}</span><button class="link" data-remdel="${r.id}">Remove</button></div>`).join("") || `<p class="muted small">No reminders set.</p>`}
          <form id="remform" class="stack" data-appt="${next.id}"><div class="two"><label>When<select name="when" id="rem-when"><option value="day_before">The day before</option><option value="morning">On the morning, at 7:00</option><option value="two_hours">2 hours before</option><option value="custom">Choose a time</option></select></label><label>Time, if chosen<input type="datetime-local" name="at"></label></div>
          <fieldset><legend>Remind me by</legend><div class="row"><label class="check"><input type="checkbox" name="ch" value="app" checked> App alert</label><label class="check"><input type="checkbox" name="ch" value="sms" checked> SMS</label><label class="check"><input type="checkbox" name="ch" value="email" ${S.me.email ? "" : "disabled"}> Email</label></div>${S.me.email ? "" : `<p class="small muted">Add your email in Settings to get email reminders.</p>`}</fieldset><button class="btn primary">Set reminder</button></form>`
        : `<p class="muted">You have no appointment booked, so there's nothing to remind you about yet.</p><div><button class="btn" data-ptab="appointments" type="button">Book a consultation</button></div>`}</section>`,
      readings: () => `<section class="card stack"><h3>Blood sugar and blood pressure</h3><p class="muted">Keeping blood sugar and blood pressure under control protects your eyes. Your health worker records readings at each visit, and you can add your own.</p>${readingsCharts(d.vitals)}${vitalsFacts(d.vitals)}</section>
        ${owner ? `<section class="card stack"><h3>Add a reading</h3>${vitalsForm("myvitals")}</section>` : ""}
        ${d.vitals.length ? `<section class="card"><h3>All readings</h3><div class="table-wrap"><table class="legend wide"><thead><tr><th>Date</th><th>Blood sugar</th><th>Blood pressure</th><th>HbA1c</th><th>By</th></tr></thead><tbody>${d.vitals.map((v) => `<tr><td data-notr>${esc(v.date)}</td><td>${v.glucose ?? ""}</td><td>${v.sys ? `${v.sys}/${v.dia}` : ""}</td><td>${v.hba1c ? v.hba1c + "%" : ""}</td><td>${esc(t({ chw: "Health worker", clinician: "Eye doctor", member: "You" }[v.by] || ""))}</td></tr>`).join("")}</tbody></table></div></section>` : ""}`,
      cover: () => {
        const cv = d.cover || {}, c = cv.cover || "", st = cv.help, hp = cv.helper;
        const status = st === "requested" ? `<div class="note warn">${esc(t("Your request is waiting for a health worker in your area. They'll SMS you."))}</div>`
          : st === "helping" ? `<div class="note ok">${esc(t("A health worker is helping you apply for SHA:"))} <b data-notr>${esc(hp ? hp.name + ", " + hp.phone : "")}</b>. ${esc(t("Keep your national ID card ready."))}</div>`
          : st === "registered" ? `<div class="note ok">${esc(t("You are registered with SHA. Show your ID card at the hospital."))}</div>` : "";
        return `<section class="card stack"><h3>How is your eye care paid for?</h3><p class="muted">This helps your health worker and eye doctor plan your care. SHA covers many eye and diabetes services at government and approved hospitals.</p>${status}
          ${owner ? `<form id="coverform" class="stack"><fieldset><legend>Health cover</legend><div class="conds">${COVERS.map(([k, n]) => `<label class="check"><input type="radio" name="cover" value="${k}" ${c === k ? "checked" : ""} required> ${n}</label>`).join("")}</div></fieldset>
            <label>SHA number, NGO or insurance name, optional<input name="ref" maxlength="40" value="${esc(cv.ref || "")}"></label>
            ${st === "helping" || st === "requested" ? "" : `<label class="check"><input type="checkbox" name="help"> I have no cover. Ask a health worker to help me apply for SHA</label>`}
            <button class="btn primary">Save</button></form>` : coverFacts(d.cover)}</section>
          <section class="card stack"><h3>Apply for SHA yourself</h3><p>${esc(t(SHA_HOW))}</p><p class="small muted">${esc(t("You can also ask at your nearest health centre or Huduma Centre."))}</p></section>`;
      },
      visit: () => {
        const vv = d.village_visit, place = P.village || "";
        return `<section class="card stack"><h3>Visit me at home</h3><p class="muted">A community health worker from your area comes to your home to check your eyes.</p>${d.visit ? `<p>${d.visit.status === "accepted" ? `${t("A health worker will visit you:")} <b data-notr>${esc(d.visit.name)}, ${esc(d.visit.phone)}</b>` : t("Your visit request is waiting for a health worker in your area.")}</p>` : `<button class="btn primary big" data-act="ask-visit" type="button">Ask a health worker to visit me</button>`}</section>
        <section class="card stack"><h3>Visit my village</h3><p class="muted">Ask a health worker to come to your village to check the eyes of several people with diabetes on one day, such as at a church, school or chief's office.</p>
          ${vv ? `<p>${vv.status === "accepted" ? `${t("A health worker will come to the village:")} <b data-notr>${esc(vv.name)}, ${esc(vv.phone)}</b>` : t("Your village visit request is waiting for a health worker in your area.")}</p><p class="small muted" data-notr>${esc(vv.place || "")} · ${esc(tr("About {n} people").replace("{n}", vv.people))}${vv.preferred ? " · " + esc(vv.preferred) : ""}</p>`
          : `<form id="villageform" class="stack"><label>Meeting place<input name="place" maxlength="80" required value="${esc(place)}" placeholder="Church, school or chief's office"></label><div class="two"><label>How many people need an eye check?<input name="people" type="number" min="2" max="200" required value="10"></label><label>Preferred date<input type="date" name="date" value="${defaultDate()}"></label></div><label>Anything the health worker should know?<textarea name="note" rows="2" maxlength="200"></textarea></label><button class="btn primary">Request a village visit</button></form>`}</section>
        <p class="small muted">No internet? Dial the AfriEye USSD code on any phone and choose 2.</p>`;
      },
      emergency: () => `<section class="result urgent" id="sos-card"><h2>Go to a hospital today if you have:</h2><ul class="todo"><li>Sudden loss of sight, in one or both eyes</li><li>A dark curtain or shadow across your vision</li><li>Many new floaters or flashes of light</li><li>Strong eye pain or a red, painful eye</li></ul><p><b>Emergency numbers: 999 or 112</b></p><div>${readBtn("#sos-card")}</div></section>
        <section class="card stack"><h3>Nearest hospitals with eye care</h3>${hlist.slice(0, 4).map((h) => `<p><b data-notr>${esc(h.name)}</b><span class="muted" data-notr>${km(h)}</span></p>`).join("")}</section>
        ${owner ? `<section class="card stack"><h3>Get help at home</h3>${d.visit ? `<p>${d.visit.status === "accepted" ? `${t("A health worker will visit you:")} <b data-notr>${esc(d.visit.name)}, ${esc(d.visit.phone)}</b>` : t("Your visit request is waiting for a health worker in your area.")}</p>` : `<button class="btn primary big" data-act="ask-visit" type="button">Ask a health worker to visit me</button>`}
          ${d.chw ? `<p>Your health worker: <b data-notr>${esc(d.chw.name)}, <a href="tel:${esc(d.chw.phone)}">${esc(d.chw.phone)}</a></b></p>` : ""}<p class="small muted">No internet? Dial the AfriEye USSD code on any phone.</p></section>` : ""}`,
      reports: () => `<section class="card stack"><h3>Reports</h3>${d.reports.map((r) => `<div class="hist stack small-gap"><b data-notr>${esc(r.title)}</b><p class="small muted"><span data-notr>${esc(r.by)}</span> · <span data-notr>${esc(r.when)}</span></p>
          <div class="row"><button class="btn mini" data-ropen="${r.id}" type="button">Open</button><button class="btn mini" data-rshare="${r.id}" type="button">Share (WhatsApp, Bluetooth and more)</button><button class="btn mini" data-rdown="${r.id}" type="button">Download</button></div>
          <form class="row" data-remail="${r.id}"><label class="grow"><span class="sr">Email address</span><input type="email" name="email" required placeholder="Email address" value="${esc(S.me.email || "")}"></label><button class="btn mini">Email</button></form></div>`).join("") || `<p class="muted">No reports yet. Your health worker or eye doctor can send one.</p>`}</section>`,
      care: () => `<section class="card stack"><h3>Rate your care</h3>${d.people.map((p) => `<form class="hist stack small-gap" data-review="${p.id}"><b><span data-notr>${esc(p.name)}</span> · ${p.role === "chw" ? "Community health worker" : "Eye doctor"}</b>
          <fieldset class="stars"><legend class="sr">Stars</legend>${[1, 2, 3, 4, 5].map((n) => `<label><input type="radio" name="stars" value="${n}" ${p.stars === n ? "checked" : ""} required><span aria-hidden="true">★</span><span class="sr">${n}</span></label>`).join("")}</fieldset>
          <label><span class="sr">Comment</span><input name="comment" maxlength="400" placeholder="Comment, optional" value="${esc(p.comment || "")}"></label><button class="btn mini">${p.stars ? "Update rating" : "Send rating"}</button></form>`).join("") || `<p class="muted">You can rate your health worker and eye doctor after they care for you.</p>`}</section>`,
      family: () => `<section class="card stack"><h3>Family who can see this record</h3>${(d.family || []).filter((f) => f.access !== "owner").map((f) => `<div class="fam-row"><div><b data-notr>${esc(f.name)}</b> <span class="muted" data-notr>${esc(f.relation)}</span><p class="small muted" data-notr>${esc(f.phone)}${f.email ? " · " + esc(f.email) : ""}</p></div><label class="check small"><input type="checkbox" data-famsms="${f.id}" ${f.receive_sms ? "checked" : ""}> Gets SMS and email</label><button class="link" data-famdel="${f.id}">Remove</button></div>`).join("") || `<p class="muted">No family members yet.</p>`}
          <form id="addfam" class="stack"><label>Name<input name="name" required></label><label>Relationship<input name="relation" required placeholder="Son"></label><label>Phone<input name="phone" inputmode="tel" required></label><label>Email, optional<input name="email" type="email" placeholder="name@example.com"></label><label class="check"><input type="checkbox" name="receive_sms" checked> Also send them appointment SMS and email</label><button class="btn">Add family member</button></form></section>`,
      messages: () => `${d.emails.length ? `<section class="card"><h3>Emails sent</h3>${d.emails.map((m) => `<div class="hist" data-notr><p><b>${esc(m.subject)}</b></p><p class="small muted">${esc(m.to)} · ${esc(m.when)}</p></div>`).join("")}</section>` : ""}
        ${d.messages.length ? `<section class="card"><h3>SMS sent</h3>${d.messages.map((m) => `<div class="hist" data-notr><p class="sms">${esc(m.body)}</p><p class="small muted">${esc(m.when)}</p></div>`).join("")}</section>` : ""}
        ${d.emails.length || d.messages.length ? "" : `<section class="card"><h3>Messages</h3><p class="muted">No messages yet.</p></section>`}`,
    };
    const item = (k, label) => `<button class="pm-item" type="button" title="${esc(t(label))}" data-ptab="${k}" aria-current="${cur === k ? "page" : "false"}">${PICON[k]}<span class="grow">${label}</span>${count[k] ? `<b class="pm-count">${count[k]}</b>` : ""}</button>`;
    const act = (a, k, label, extra) => `<button class="pm-item${extra || ""}" type="button" title="${esc(t(label))}" data-act="${a}">${PICON[k]}<span class="grow">${label}</span></button>`;
    const title = MENU.find((m) => m[0] === cur)[1];
    const rec = $("#rec"); rec.className = "";
    rec.innerHTML = `<div class="pdash">
      <nav class="pmenu" aria-label="Menu"><button class="pm-item pm-toggle" type="button" data-act="pm-toggle" aria-expanded="false" title="${esc(t("Menu"))}">${PICON.menu}<span class="grow">Menu</span></button><div class="pm-who">${avatar(P.id, P.has_face, P.name)}<div><b data-notr>${esc(P.name)}</b><span class="pill ${cls}">${esc(st ? st.label : "Waiting for result")}</span></div></div>
        ${MENU.map(([k, l]) => item(k, l)).join("")}<hr>${act("assistant", "assistant", "Ask AfriEye")}${act("settings", "settings", "Settings")}${act("logout", "logout", "Sign out", " pm-out")}</nav>
      <div class="pcontent"><div class="pc-head"><h2>${title}</h2></div><div class="vstack">${PAGES[cur]()}</div></div></div>`;
  }

  // ---- Health readings: a small line chart that always comes with the numbers
  function lineChart(title, series, unit, band) {
    const pts = series.flatMap((s) => s.values.filter((v) => v.y !== null && v.y !== undefined));
    if (!pts.length) return "";
    const W = 320, H = 150, L = 36, B = 22, n = Math.max(...series.map((s) => s.values.length));
    let lo = Math.min(...pts.map((p) => p.y), band ? band[0] : Infinity), hi = Math.max(...pts.map((p) => p.y), band ? band[1] : -Infinity); const pad = (hi - lo) * 0.15 || 1; lo -= pad; hi += pad;
    const x = (i) => L + (n === 1 ? (W - L - 10) / 2 : (i * (W - L - 10)) / (n - 1)), y = (v) => 8 + (H - B - 8) * (1 - (v - lo) / (hi - lo));
    const ticks = [lo + pad, (lo + hi) / 2, hi - pad].map((v) => `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end" class="ax">${Math.round(v * 10) / 10}</text><line x1="${L}" x2="${W - 6}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`).join("");
    const bandEl = band ? `<rect x="${L}" width="${W - L - 6}" y="${y(band[1])}" height="${Math.max(1, y(band[0]) - y(band[1]))}" class="band-ok"/>` : "";
    const lines = series.map((s) => { const v = s.values.map((p, i) => [p, i]).filter(([p]) => p.y !== null && p.y !== undefined); return `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${v.map(([p, i]) => `${x(i)},${y(p.y)}`).join(" ")}"/>${v.map(([p, i]) => `<circle cx="${x(i)}" cy="${y(p.y)}" r="4" fill="${s.color}"><title>${esc(p.label)}: ${p.y} ${unit}</title></circle>`).join("")}`; }).join("");
    const labels = series[0].values.map((p, i) => `<text x="${x(i)}" y="${H - 5}" text-anchor="middle" class="ax">${esc(p.label)}</text>`).join("");
    return `<figure class="chart line-chart"><figcaption>${esc(title)}</figcaption><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${bandEl}${ticks}${lines}${labels}</svg>${series.length > 1 ? `<p class="small muted">${series.map((s) => `<span class="swatch" style="background:${s.color}"></span>${esc(t(s.name))}`).join(" ")}</p>` : ""}</figure>`;
  }
  function vitalsFacts(v) {
    if (!v || !v.length) return `<p class="muted small">No readings yet.</p>`;
    const g = v.find((x) => x.glucose !== null), b = v.find((x) => x.sys !== null), a = v.find((x) => x.hba1c !== null);
    return `<dl class="facts one">${g ? `<div><dt>Blood sugar</dt><dd><span data-notr>${g.glucose} mmol/L · ${esc(g.date)}</span> ${g.glucose >= (g.glucose_kind === "fasting" ? 7 : 11.1) ? `<span class="pill refer">${esc(t("High"))}</span>` : ""}</dd></div>` : ""}${b ? `<div><dt>Blood pressure</dt><dd><span data-notr>${b.sys}/${b.dia} · ${esc(b.date)}</span> ${b.sys >= 140 || b.dia >= 90 ? `<span class="pill refer">${esc(t("High"))}</span>` : ""}</dd></div>` : ""}${a ? `<div><dt>HbA1c</dt><dd><span data-notr>${a.hba1c}% · ${esc(a.date)}</span> ${a.hba1c >= 7 ? `<span class="pill refer">${esc(t("Above target"))}</span>` : ""}</dd></div>` : ""}</dl>`;
  }
  function treatmentFacts(list) {
    if (!list || !list.length) return "";
    return `<div class="stack small-gap">${list.map((x) => `<div class="hist"><b>${esc(t(x.label))}</b>${x.eye ? ` · ${esc(t(x.eye === "both" ? "Both eyes" : EYE[x.eye]))}` : ""}<p class="small muted"><span data-notr>${esc(x.when)}${x.doctor ? " · " + esc(x.doctor) : ""}</span>${x.va_right || x.va_left ? ` · ${esc(t("Vision"))} <span data-notr>${x.va_right ? "R " + esc(x.va_right) : ""} ${x.va_left ? "L " + esc(x.va_left) : ""}</span>` : ""}</p>${x.note ? `<p class="small" data-notr>${esc(x.note)}</p>` : ""}</div>`).join("")}</div>`;
  }
  function readingsCharts(v) {
    const r = [...v].reverse(), lab = (x) => x.date;
    const g = r.filter((x) => x.glucose !== null), b = r.filter((x) => x.sys !== null);
    return `<div class="charts">${g.length ? lineChart(t("Blood sugar, mmol/L"), [{ name: "Blood sugar", color: "#006B3F", values: g.map((x) => ({ y: x.glucose, label: lab(x) })) }], "mmol/L", [4, 10]) : ""}${b.length ? lineChart(t("Blood pressure"), [{ name: "Top number", color: "#BB0000", values: b.map((x) => ({ y: x.sys, label: lab(x) })) }, { name: "Bottom number", color: "#1C1C1C", values: b.map((x) => ({ y: x.dia, label: lab(x) })) }], "mmHg", [70, 140]) : ""}</div>`;
  }
  const vitalsForm = (id) => `<form id="${id}" class="stack"><div class="two"><label>Blood sugar, mmol/L<input name="glucose" inputmode="decimal" maxlength="4"></label><label>Taken<select name="glucose_kind"><option value="random">Any time</option><option value="fasting">Fasting</option></select></label></div>
    <div class="three"><label>Blood pressure, top<input name="sys" inputmode="numeric" maxlength="3" placeholder="130"></label><label>Bottom<input name="dia" inputmode="numeric" maxlength="3" placeholder="80"></label><label>HbA1c, %<input name="hba1c" inputmode="decimal" maxlength="4"></label></div><button class="btn primary">Save reading</button></form>`;

  // ---- CHW training: short cards, then a 5-question quiz. Passing gives a certificate.
  const TRAIN = [
    ["Why we screen", "Diabetes can damage the back of the eye with no pain and no early signs. Found early, laser or injections can save sight. Everyone with diabetes needs an eye check every year."],
    ["Consent first", "Explain what you will do: a photo of the back of each eye, checked by AI on the phone. Ask if they agree, and tick the consent box. They can say no and still get care."],
    ["Taking a good photo", "Sit the person in a dim place, so the pupil opens. Fix the adapter on the phone, hold it close to the eye, and keep still. Use Live camera with tips. Take each eye. If the app asks for a retake, try again."],
    ["Reading the result", "Green means no referable damage: check again in a year. Amber means refer to an eye clinic. Red means urgent: refer now and tell them to go soon. The AI is a first check; the eye specialist decides."],
    ["After the referral", "Choose the nearest hospital and send. The clinician must respond within 7 days. If the patient misses the appointment, AfriEye asks you to follow up. Help people with no cover apply for SHA."],
    ["Privacy", "Only record what is needed. Never share photos or results on WhatsApp groups. Lock your phone. Patients can see and delete their own record."],
  ];
  const QUIZ_Q = [
    ["How often should a person with diabetes have an eye check?", ["Only when they can't see well", "Every year", "Every 5 years"]],
    ["The AI result is red: possible severe damage. What do you do?", ["Wait and check again next year", "Tell them to buy glasses", "Send an urgent referral now"]],
    ["What must you do before taking the eye photo?", ["Get the person's agreement", "Ask for payment", "Take their ID card"]],
    ["The photo can't be read. What is the best next step?", ["Guess the result", "Send it on WhatsApp", "Move to a dim place, hold still and retake"]],
    ["A patient missed their eye appointment. What should you do?", ["Remove them from the list", "Visit or call them and help them book again", "Nothing, it is the clinic's job"]],
  ];
  async function renderTraining(head) {
    const tr2 = S.train || (S.train = { step: 0, answers: [], result: null }), u = S.me;
    let body = "";
    if (u.trained && !tr2.redo) body = `<section class="card stack cert"><span class="pill ok">${esc(t("Trained"))}</span><h2>You completed AfriEye training</h2><p class="muted" data-notr>${esc(u.trained)}</p><div class="row"><button class="btn primary" data-act="cert" type="button">Open my certificate</button><button class="btn" data-act="train-redo" type="button">Go through it again</button></div></section>`;
    else if (tr2.step < TRAIN.length) { const [h, p] = TRAIN[tr2.step]; body = `<section class="card stack" id="train-card"><p class="eyebrow">${esc(tr("Lesson {n} of {m}").replace("{n}", tr2.step + 1).replace("{m}", TRAIN.length))}</p><h2>${esc(h)}</h2><p>${esc(p)}</p><div>${readBtn("#train-card")}</div><div class="row between"><button class="btn" data-act="train-back" type="button" ${tr2.step ? "" : "disabled"}>Back</button><button class="btn primary" data-act="train-next" type="button">${tr2.step === TRAIN.length - 1 ? "Start the quiz" : "Next"}</button></div></section>`; }
    else if (!tr2.result) body = `<form id="quizform" class="card stack"><h2>Quiz</h2><p class="muted">Answer 4 of 5 correctly to pass.</p>${QUIZ_Q.map(([q, opts], i) => `<fieldset><legend>${i + 1}. ${esc(q)}</legend>${opts.map((o, j) => `<label class="check"><input type="radio" name="q${i}" value="${j}" required> ${esc(o)}</label>`).join("")}</fieldset>`).join("")}<button class="btn primary big">Check my answers</button></form>`;
    else body = `<section class="card stack"><h2>${tr2.result.passed ? "Well done. You passed." : "Not yet. Read the lessons again and retry."}</h2><p class="big-text" data-notr>${tr2.result.score} / ${tr2.result.total}</p>${tr2.result.wrong.length ? `<p class="muted">${esc(t("Questions to review:"))} <span data-notr>${tr2.result.wrong.map((i) => i + 1).join(", ")}</span></p>` : ""}<div class="row">${tr2.result.passed ? `<button class="btn primary" data-act="cert" type="button">Open my certificate</button>` : `<button class="btn primary" data-act="train-redo" type="button">Try again</button>`}</div></section>`;
    frame(head + `<div class="vstack">${body}</div>`);
  }

  // ---- Live camera with tips while taking the eye photo
  async function liveCamera(eye) {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } }, audio: false }); }
    catch (e) { return toast(t("The camera couldn't be opened. Use Take photo instead.")); }
    const box = document.createElement("div"); box.className = "pv-back cam-back"; box.id = "cam";
    box.innerHTML = `<div class="cam" role="dialog" aria-modal="true" aria-labelledby="cam-title"><div class="pv-head"><h2 id="cam-title">${esc(t(EYE[eye]))}</h2><button class="icon-btn pv-x" data-cam="close" type="button" aria-label="Close">✕</button></div>
      <div class="cam-view"><video playsinline muted autoplay></video><div class="cam-ring" aria-hidden="true"></div></div>
      <p class="cam-tip" id="cam-tip" role="status" aria-live="polite">${esc(t("Looking…"))}</p><ul class="cam-checks" id="cam-checks"></ul>
      <div class="row between"><button class="btn" data-cam="close" type="button">Cancel</button><button class="btn primary big" data-cam="shoot" type="button" disabled>Take photo</button></div></div>`;
    document.body.appendChild(box); translateTree(box);
    const v = box.querySelector("video"); v.srcObject = stream; await v.play().catch(() => {});
    const c = document.createElement("canvas"), x = c.getContext("2d", { willReadFrequently: true }); c.width = 96; c.height = 96; let prev = null, steady = 0, timer;
    const shoot = box.querySelector("[data-cam=shoot]"), tip = box.querySelector("#cam-tip"), checks = box.querySelector("#cam-checks");
    const stop = () => { clearInterval(timer); stream.getTracks().forEach((tk) => tk.stop()); box.remove(); };
    function look() {
      if (!v.videoWidth) return;
      const side = Math.min(v.videoWidth, v.videoHeight); x.drawImage(v, (v.videoWidth - side) / 2, (v.videoHeight - side) / 2, side, side, 0, 0, 96, 96);
      const d = x.getImageData(0, 0, 96, 96).data, g = new Float32Array(96 * 96); let sum = 0, lit = 0, red = 0;
      for (let i = 0; i < g.length; i++) { const r = d[i * 4], gg = d[i * 4 + 1], b = d[i * 4 + 2]; g[i] = 0.3 * r + 0.59 * gg + 0.11 * b; sum += g[i]; if (g[i] > 40) { lit++; red += r / (gg + b + 1); } }
      const mean = sum / g.length, cover = lit / g.length, reddish = lit ? red / lit : 0;
      let edge = 0; for (let yy = 1; yy < 95; yy++) for (let xx = 1; xx < 95; xx++) { const i = yy * 96 + xx; edge += Math.abs(4 * g[i] - g[i - 1] - g[i + 1] - g[i - 96] - g[i + 96]); }
      const sharp = edge / (94 * 94); let move = 0; if (prev) { for (let i = 0; i < g.length; i += 7) move += Math.abs(g[i] - prev[i]); move /= g.length / 7; } prev = g;
      steady = move < 6 ? steady + 1 : 0;
      const items = [[mean > 35, mean < 35 ? "Too dark: turn on the adapter light" : "Light is good"], [mean < 200, mean >= 200 ? "Too bright: move away from the window" : "Not too bright"], [cover > 0.3, cover <= 0.3 ? "Move closer: fill the circle with the eye" : "Eye fills the circle"], [steady >= 3, steady < 3 ? "Hold still" : "Steady"], [sharp > 3, sharp <= 3 ? "Blurry: hold the adapter firmly against the eye" : "Sharp"]];
      const first = items.find(([ok]) => !ok), ok = !first;
      tip.textContent = t(ok ? "Good. Take the photo now." : first[1]); tip.className = "cam-tip " + (ok ? "ok" : "warn");
      checks.innerHTML = items.map(([good, msg]) => `<li class="${good ? "ok" : ""}">${good ? "✓" : "•"} ${esc(t(msg))}</li>`).join("");
      shoot.disabled = !(mean > 20 && cover > 0.15); box.dataset.reddish = reddish.toFixed(2);
    }
    timer = setInterval(look, 250);
    box.addEventListener("click", (ev) => {
      const b = ev.target.closest("[data-cam]"); if (!b) return;
      if (b.dataset.cam === "close") return stop();
      const cv = document.createElement("canvas"); cv.width = v.videoWidth; cv.height = v.videoHeight; cv.getContext("2d").drawImage(v, 0, 0);
      const url = cv.toDataURL("image/jpeg", 0.92); stop(); takePhoto(eye, null, url);
    });
  }

  // ---- County health officer: totals only, never names
  async function renderCounty() {
    S.period = S.period && S.period !== "today" ? S.period : "year"; S.tab = S.tab === "staff" ? "staff" : "dash";
    let staff = []; try { staff = await api("GET", "/api/county/staff"); } catch (e) { /* shown below */ }
    const waitingN = staff.filter((x) => x.status === "waiting").length;
    const head = tabs([["dash", "Dashboard"], ["staff", `${t("Verify clinicians")} (${waitingN})`]]);
    if (S.tab === "staff") {
      const card = (x) => `<article class="card stack"><div class="row between"><div><b data-notr>${esc(x.name)}</b><p class="small muted" data-notr>${esc(x.hospital)}</p></div><span class="pill ${x.status === "approved" ? "ok" : x.status === "rejected" ? "urgent" : "watch"}">${esc(t({ approved: "Verified", rejected: "Not approved", waiting: "Waiting for you" }[x.status]))}</span></div>
        <dl class="facts"><div><dt>Licence number</dt><dd data-notr>${esc(x.licence || "")}</dd></div><div><dt>Phone</dt><dd data-notr>${esc(x.phone)}</dd></div><div><dt>Email</dt><dd data-notr>${esc(x.email || "")}</dd></div><div><dt>Registered</dt><dd data-notr>${esc(x.registered)}</dd></div></dl>
        ${x.status === "waiting" ? `<p class="small muted">${esc(t("Check that the name and licence number match the official register, and call the hospital if unsure."))} <a href="https://kmpdc.go.ke" target="_blank" rel="noopener">KMPDC</a> · <a href="https://clinicalofficerscouncil.org" target="_blank" rel="noopener">Clinical Officers Council</a></p>
          <form class="row" data-staffreject="${x.id}"><label class="grow"><span class="sr">Reason</span><input name="reason" maxlength="200" required placeholder="Reason, if you reject"></label><button class="btn danger">Reject</button></form><div><button class="btn primary" data-staffok="${x.id}" type="button">Approve</button></div>`
          : `<p class="small muted">${x.reviewed_by ? `<span data-notr>${esc(x.reviewed_by)} · ${esc(x.reviewed)}</span>` : ""}${x.reason ? ` · <span data-notr>${esc(x.reason)}</span>` : ""}</p>${x.status === "rejected" ? `<div><button class="btn mini" data-staffok="${x.id}" type="button">Approve after all</button></div>` : ""}`}</article>`;
      frame(head + `<div class="vstack"><p class="muted">${esc(t("New eye clinicians at hospitals in your county wait here until you check their licence. This stops people pretending to be specialists."))}</p>${staff.map(card).join("") || `<p class="muted">${esc(t("No clinicians have registered yet."))}</p>`}</div>`, ROLE.county);
      return;
    }
    frame(head + `<div id="county" class="muted">Loading the county dashboard…</div>`, ROLE.county);
    let d; try { d = await api("GET", `/api/county/summary?period=${S.period}`); } catch (e) { const c = $("#county"); if (c) { c.className = "note bad"; c.textContent = e.message; } return; }
    const T = d.totals, pct = (v) => (v === null || v === undefined ? "–" : Math.round(v * 100) + "%"), m = d.model.all;
    const pts = d.areas.concat(d.hospitals), lats = pts.map((p) => p.lat), lngs = pts.map((p) => p.lng);
    const la0 = Math.min(...lats), la1 = Math.max(...lats), ln0 = Math.min(...lngs), ln1 = Math.max(...lngs), span = Math.max(la1 - la0, ln1 - ln0, 0.04) * 1.35, cla = (la0 + la1) / 2, cln = (ln0 + ln1) / 2;
    const px = (p) => [20 + ((p.lng - cln) / span + 0.5) * 360, 20 + ((cla - p.lat) / span + 0.5) * 260];
    const maxS = Math.max(1, ...d.areas.map((a) => a.screenings));
    const map = `<svg viewBox="0 0 400 300" class="county-map" role="img" aria-label="${esc(t("Map of screenings by area"))}"><rect x="0" y="0" width="400" height="300" rx="12" class="map-bg"/>
      ${d.hospitals.map((h) => { const [x, y] = px(h); return `<g><rect x="${x - 7}" y="${y - 7}" width="14" height="14" rx="3" class="map-h"/><path d="M${x - 4} ${y}h8M${x} ${y - 4}v8" stroke="#fff" stroke-width="2"/><title>${esc(h.name)}: ${h.waiting} ${esc(t("waiting"))}</title></g>`; }).join("")}
      ${d.areas.map((a) => { const [x, y] = px(a), r = 8 + 22 * Math.sqrt(a.screenings / maxS), share = a.screenings ? a.ai_referred / a.screenings : 0; return `<g><circle cx="${x}" cy="${y}" r="${r}" class="map-a" style="fill-opacity:${0.25 + 0.6 * share}"/><text x="${x}" y="${y + r + 13}" text-anchor="middle" class="map-t">${esc(a.name)}</text><text x="${x}" y="${y + 4}" text-anchor="middle" class="map-n">${a.screenings}</text><title>${esc(a.name)}: ${a.screenings} ${esc(t("screenings"))}, ${a.ai_referred} ${esc(t("referred"))}</title></g>`; }).join("")}</svg>
      <p class="small muted">${esc(t("Circles: screenings in each area. Darker means a bigger share referred. Squares: hospitals."))}</p>`;
    const el = $("#county"); el.className = "stack";
    el.innerHTML = `<div class="hosp-title"><h2>${esc(tr("{c} County").replace("{c}", d.county))}</h2><p class="muted">${esc(t("Diabetic eye screening. Totals only: no patient names are shown here."))}</p></div>
      <div class="period" role="group" aria-label="Choose a period">${[["month", "This month"], ["year", "This year"], ["all", "All time"]].map(([k, n]) => `<button class="tile pick ${S.period === k ? "on" : ""}" data-cperiod="${k}" aria-pressed="${S.period === k}"><span>${n}</span></button>`).join("")}</div>
      <section class="tiles">${tile(T.screenings, "Screenings")}${tile(T.people, "People screened")}${tile(T.ai_referred, "Referred by AI")}${tile(T.dr_confirmed, "DR confirmed by specialists")}${tile(T.sight_threatening, "Sight-threatening", T.sight_threatening ? "alert" : "")}</section>
      ${T.sample ? `<p class="small muted">${esc(tr("{n} of these screenings are sample data.").replace("{n}", T.sample))}</p>` : ""}
      <div class="dash-top"><section class="card"><h3>Where screening happens</h3>${map}</section>
        <section class="card stack"><h3>Is the system working?</h3><dl class="facts one"><div><dt>Referrals reviewed</dt><dd>${d.wait.reviewed}</dd></div><div><dt>Median days to specialist review</dt><dd>${d.wait.median_days ?? "–"}</dd></div><div><dt>Reviewed within 7 days</dt><dd>${pct(d.wait.within_7)}</dd></div>
          <div><dt>Attended eye appointments</dt><dd>${pct(d.attendance.rate)} <span class="small muted" data-notr>(${d.attendance.attended} / ${d.attendance.attended + d.attendance.missed})</span></dd></div><div><dt>Missed appointments</dt><dd>${d.attendance.missed}</dd></div><div><dt>Overdue for yearly check</dt><dd>${d.recall_overdue}</dd></div><div><dt>CHWs trained</dt><dd data-notr>${d.chws_trained} / ${d.chws}</dd></div></dl></section></div>
      <section class="card"><h3>By area</h3><div class="table-wrap"><table class="legend wide"><thead><tr><th>Area</th><th>Screenings</th><th>People</th><th>Referred by AI</th><th>Urgent</th><th>DR confirmed</th></tr></thead><tbody>${d.areas.map((a) => `<tr><td data-notr>${esc(a.name)}</td><td>${a.screenings}</td><td>${a.people}</td><td>${a.ai_referred}</td><td>${a.urgent}</td><td>${a.dr_confirmed}</td></tr>`).join("")}</tbody></table></div></section>
      <section class="card"><h3>Hospitals</h3><div class="table-wrap"><table class="legend wide"><thead><tr><th>Hospital</th><th>Waiting for review</th><th>Over 7 days</th></tr></thead><tbody>${d.hospitals.map((h) => `<tr><td data-notr>${esc(h.name)}</td><td>${h.waiting}</td><td class="${h.overdue ? "bad" : ""}">${h.overdue}</td></tr>`).join("") || `<tr><td colspan="3">${esc(t("No hospitals in this county yet."))}</td></tr>`}</tbody></table></div></section>
      <section class="card"><div class="charts">${pie(t("AI first results"), d.outcomes, PIE.clinic)}${pie(t("Health cover"), d.cover.filter((c) => c.count), PIE.clinic, "People")}${d.treatments.length ? pie(t("Treatments given"), d.treatments, PIE.clinic, "Patients") : ""}</div></section>
      ${modelCheck(d.model)}`;
  }
  function modelCheck(mc) {
    const m = mc.all, pct = (v) => (v === null || v === undefined ? "–" : Math.round(v * 100) + "%");
    const G = ["none", "mild", "moderate", "severe", "proliferative"], GN = { none: "No DR", mild: "Mild", moderate: "Moderate", severe: "Severe", proliferative: "Proliferative" }, ON = { rescreen: "No referral", refer: "Refer", urgent: "Urgent" };
    return `<section class="card stack"><h3>How well the AI agrees with the eye specialists</h3>
      <div class="tiles trio">${tile(m.n, "Referrals graded by a specialist")}${tile(pct(m.ppv), "AI referrals confirmed")}${tile(m.urgent ? `${m.urgent_confirmed}/${m.urgent}` : "–", "AI urgent, specialist severe")}</div>
      <div class="table-wrap"><table class="legend wide"><thead><tr><th>${esc(t("AI said"))} \\ ${esc(t("Specialist graded"))}</th>${G.map((g) => `<th>${esc(t(GN[g]))}</th>`).join("")}</tr></thead><tbody>${["urgent", "refer", "rescreen"].map((o) => `<tr><th>${esc(t(ON[o]))}</th>${G.map((g) => `<td>${m.grid[o][g] || ""}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <p class="small muted">${esc(t("Only referrals are graded by a specialist, so this shows how often referrals were right. To measure missed cases, a specialist should also grade a random 1 in 10 of the photos the AI didn't refer."))}${mc.sample ? " " + esc(tr("{n} of these are sample data.").replace("{n}", mc.sample)) : ""}</p></section>`;
  }


  // ---- Handlers for follow-ups, recall, training, camera, attendance, treatment, readings and the county dashboard
  const TREAT_OPTS = [["laser", "Laser treatment"], ["injection", "Eye injection (anti-VEGF)"], ["surgery", "Eye surgery (vitrectomy)"], ["cataract", "Cataract surgery"], ["glasses", "Glasses"], ["monitor", "Watch and check again"], ["other", "Other"]];
  const VA = ["", "6/6", "6/9", "6/12", "6/18", "6/24", "6/36", "6/60", "3/60", "CF", "HM", "PL", "NPL"];
  function openTreatForm(pid, name) {
    closePreview();
    const box = document.createElement("div"); box.className = "pv-back"; box.id = "pv";
    box.innerHTML = `<form class="pv stack" id="treatform" data-pid="${pid}" role="dialog" aria-modal="true" aria-labelledby="tr-title"><div class="pv-head"><h2 id="tr-title">Record treatment</h2><button class="icon-btn pv-x" data-pv="close" type="button" aria-label="Close">✕</button></div><p class="muted" data-notr>${esc(name)}</p>
      <label>Treatment given<select name="treatment" required>${TREAT_OPTS.map(([k, n]) => `<option value="${k}">${n}</option>`).join("")}</select></label>
      <label>Eye<select name="eye"><option value="right">Right eye</option><option value="left">Left eye</option><option value="both">Both eyes</option></select></label>
      <div class="two"><label>Vision after, right eye<select name="va_right" data-notr>${VA.map((v) => `<option>${v}</option>`).join("")}</select></label><label>Vision after, left eye<select name="va_left" data-notr>${VA.map((v) => `<option>${v}</option>`).join("")}</select></label></div>
      <label>Note, optional<textarea name="note" rows="2" maxlength="300"></textarea></label>
      <div class="row between"><button class="btn" data-pv="close" type="button">Cancel</button><button class="btn primary">Save treatment</button></div></form>`;
    document.body.appendChild(box); translateTree(box); box.querySelector("select").focus();
  }
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-fudone],[data-recall],[data-live],[data-attend],[data-treatfor],[data-cperiod],[data-staffok],[data-act=train-next],[data-act=train-back],[data-act=train-redo],[data-act=cert]"); if (!b) return;
    const A = b.dataset.act;
    if (b.dataset.fudone) { try { await api("POST", `/api/chw/followups/${b.dataset.fudone}/done`, {}); toast(t("Marked as followed up.")); } catch (e) { toast(tr(e.message)); } return renderChw(); }
    if (b.dataset.recall !== undefined) {
      const r = (S.recall || [])[+b.dataset.recall]; if (!r) return;
      S.flow = newFlow(); S.flow.form = { name: r.name, phone: "0" + String(r.phone).replace(/^\+?254/, ""), birth_year: r.birth_year || "", area: r.area, email: "", sms_consent: true }; S.tab = "screen"; window.scrollTo(0, 0); return renderChw();
    }
    if (b.dataset.live) return liveCamera(b.dataset.live);
    if (b.dataset.attend) { b.disabled = true; try { await api("POST", `/api/clinic/appointments/${b.dataset.attend}/attendance`, { status: b.dataset.st }); toast(b.dataset.st === "missed" ? t("Marked as missed. The health worker will follow up.") : t("Marked as attended.")); } catch (e) { toast(tr(e.message)); } return renderClinic(); }
    if (b.dataset.treatfor) return openTreatForm(b.dataset.treatfor, b.dataset.tname || "");
    if (b.dataset.cperiod) { S.period = b.dataset.cperiod; return renderCounty(); }
    if (b.dataset.staffok) { b.disabled = true; try { await api("POST", `/api/county/staff/${b.dataset.staffok}/approve`, {}); toast(t("Approved. The clinician was told by SMS.")); } catch (e) { toast(tr(e.message)); } return renderCounty(); }
    const tr2 = S.train || (S.train = { step: 0, answers: [], result: null });
    if (A === "train-next") { tr2.step += 1; window.scrollTo(0, 0); return renderChw(); }
    if (A === "train-back") { tr2.step = Math.max(0, tr2.step - 1); return renderChw(); }
    if (A === "train-redo") { S.train = { step: 0, answers: [], result: null, redo: true }; return renderChw(); }
    if (A === "cert") { try { const r = await fetch("/api/chw/certificate", { credentials: "same-origin" }); if (!r.ok) throw new Error(t("Pass the training quiz first.")); const bytes = new Uint8Array(await r.arrayBuffer()); pdfModal({ url: URL.createObjectURL(new Blob([bytes], { type: "application/pdf" })), bytes }, t("Your AfriEye training certificate"), t("Download it or print it from Open the PDF."), ""); } catch (e) { toast(tr(e.message)); } }
  });
  document.addEventListener("change", async (ev) => {
    const el = ev.target;
    if (el.id === "tipsms") { try { await api("POST", `/api/patients/${S.patient}/tips`, { on: el.checked }); toast(el.checked ? t("You'll get a tip by SMS every week.") : t("Weekly tips are off.")); } catch (e) { toast(tr(e.message)); el.checked = !el.checked; } }
  });
  document.addEventListener("submit", async (ev) => {
    const f = ev.target;
    if (f.dataset.staffreject) { ev.preventDefault(); try { await api("POST", `/api/county/staff/${f.dataset.staffreject}/reject`, { reason: new FormData(f).get("reason") }); toast(t("Rejected. The person was told by SMS.")); } catch (e) { toast(tr(e.message)); } return renderCounty(); }
    if (!["quizform", "myvitals", "treatform"].includes(f.id)) return;
    ev.preventDefault(); const fd = Object.fromEntries(new FormData(f));
    if (f.id === "quizform") {
      const answers = [0, 1, 2, 3, 4].map((i) => +fd["q" + i]);
      try { const r = await api("POST", "/api/chw/training", { answers }); S.train.result = r; if (r.passed) { S.me = await api("GET", "/api/me"); S.train.redo = false; } } catch (e) { toast(tr(e.message)); }
      window.scrollTo(0, 0); return renderChw();
    }
    if (f.id === "myvitals") { try { await api("POST", `/api/patients/${S.patient}/vitals`, fd); toast(t("Reading saved.")); } catch (e) { toast(tr(e.message)); return; } return renderFamily(); }
    if (f.id === "treatform") { try { await api("POST", `/api/clinic/patients/${f.dataset.pid}/treatments`, fd); closePreview(); toast(t("Treatment saved. The patient was told.")); } catch (e) { toast(tr(e.message)); return; } return renderClinic(); }
  });
  // ---- Routing
  function renderPending() {
    const u = S.me;
    if (u.rejected) return frame(`<section class="card narrow stack"><span class="pill urgent">${esc(t("Not approved"))}</span><h2>Your account was not approved</h2><p>${esc(tr("The {c} County health office gave this reason:").replace("{c}", u.county || ""))}</p><p class="note warn" data-notr>${esc(u.rejected)}</p><p class="muted">If this is a mistake, contact the county health office with your licence certificate. They can approve your account later.</p></section>`);
    const who = u.role === "clinician" ? tr("The {c} County health office checks your licence on the KMPDC or Clinical Officers Council register.").replace("{c}", u.county || "") : u.role === "county" ? t("The AfriEye system administrator checks your staff number with the county.") : t("Your CHW ID needs to be checked.");
    frame(`<section class="card narrow stack"><span class="pill watch">${esc(t("Waiting for verification"))}</span><h2>Your account is waiting for verification</h2><p>${esc(who)}</p><p class="muted">This stops people pretending to be health workers. You'll get an SMS when it's done. Until then you can't see patient records.</p>${u.hospital ? `<dl class="facts one"><div><dt>Hospital</dt><dd data-notr>${esc(u.hospital)}</dd></div><div><dt>Licence number</dt><dd data-notr>${esc(u.staff_id || "")}</dd></div></dl>` : ""}</section>`);
  }
  async function render() {
    if (!S.me) return renderAuth();
    if (S.view === "settings") return renderSettings();
    if (S.view === "alerts") return renderAlerts();
    if (S.view === "assistant") return renderAssistant();
    if (S.me.role !== "member" && !S.me.approved) return renderPending();
    if (S.me.role === "chw") return renderChw();
    if (S.me.role === "clinician") return renderClinic();
    if (S.me.role === "county") return renderCounty();
    return renderFamily();
  }
  async function afterSignIn(me) {
    S.me = me; S.tab = null; S.ptab = null; S.flow = null; S.selected = null; S.patient = null; S.view = null; if (me.lang && me.lang !== "en") setLang(me.lang); S.regPhone = null; S.testCode = null; S.regRole = null; S.authMode = "login"; remember(me.role === "chw" ? me : null); say();
    if (me.role === "chw") await loadDirectory();
    await render(); if (me.role === "chw" && me.approved) sync();
  }

  // ---- Events
  document.addEventListener("click", async (ev) => {
    const t = ev.target.closest("[data-act],[data-tab],[data-ptab],[data-pick],[data-sample],[data-retry],[data-famdel],[data-period],[data-cconfirm],[data-cdecline]"); if (!t) return;
    if (t.dataset.act === "pm-toggle") { const d = t.closest(".pdash"), open = !d.classList.contains("open"); d.classList.toggle("open", open); t.setAttribute("aria-expanded", open); return; }
    if (t.dataset.ptab !== undefined) { S.ptab = t.dataset.ptab || null; say(); window.scrollTo(0, 0); return renderFamily(); }
    if (t.dataset.tab) { keepForm(); S.tab = t.dataset.tab; say(); if (t.dataset.tab === "screen" && S.flow && S.flow.step === "done") S.flow = null; return render(); }
    if (t.dataset.period) { S.period = t.dataset.period; return renderChw(); }
    if (t.dataset.pick) { S.selected = +t.dataset.pick; say(); return renderClinic(); }
    if (t.dataset.sample) { const s = window.AFRIEYE_SAMPLES[+t.dataset.sample]; S.flow.samples = true; return takePhoto(t.dataset.eye, null, s.src); }
    if (t.dataset.retry) { try { await api("POST", `/api/clinic/sms/${t.dataset.retry}/retry`, {}); } catch (e) { say("bad", e.message); } return renderClinic(); }
    if (t.dataset.famdel) { try { await api("DELETE", `/api/patients/${S.patient}/family/${t.dataset.famdel}`); say("ok", "Removed."); } catch (e) { say("bad", e.message); } return renderFamily(); }
    if (t.dataset.cconfirm || t.dataset.cdecline) {
      const id = t.dataset.cconfirm || t.dataset.cdecline; t.disabled = true;
      try {
        if (t.dataset.cconfirm) { const r = await api("POST", `/api/clinic/consultations/${id}/confirm`, { date: $(`#c-date-${id}`).value, time: $(`#c-time-${id}`).value }); say("ok", r.consent ? "Consultation confirmed. SMS sent." : "Consultation confirmed. The patient didn't agree to SMS, so please call them."); }
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
    if (act === "fam-add") { keepForm(); f.family.push({ name: "", relation: "", phone: "", email: "" }); return renderChw(); }
    if (act === "fam-del") { keepForm(); f.family.splice(+t.dataset.i, 1); return renderChw(); }
    if (act === "sync") return sync();
    if (act === "face-del") { try { await api("DELETE", `/api/patients/${S.patient}/face`); S.faceV = Date.now(); say("ok", "Photo removed."); } catch (e) { say("bad", e.message); } return renderFamily(); }
    if (act === "confirm" || act === "no-visit") {
      const s = S.queue.waiting.find((x) => x.id === S.selected), b = S.booking[s.id]; t.disabled = true;
      try {
        const r = await api("POST", `/api/clinic/referrals/${s.id}/${act === "confirm" ? "confirm" : "no-visit"}`, b);
        if (act === "confirm") {
          const n = (r.sms || []).length, failed = (r.sms || []).filter((x) => !x.ok).length;
          say(failed ? "bad" : "ok", !r.consent ? `Appointment confirmed for ${s.patient.name}. They didn't agree to SMS, so the CHW will tell them.` : failed ? `Appointment saved, but ${failed} of ${n} SMS failed. Open the SMS log to send again.` : `Appointment confirmed for ${s.patient.name}. SMS sent to ${n} ${n > 1 ? "people" : "person"}.`);
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
    } else if (form.id === "coverform") {
      const fd = new FormData(form);
      try { await api("POST", `/api/patients/${S.patient}/cover`, { cover: fd.get("cover"), ref: fd.get("ref"), help: fd.get("help") === "on" }); toast(fd.get("help") === "on" ? t("Request sent. A health worker in your area will SMS you.") : t("Saved.")); } catch (e) { toast(tr(e.message)); }
      renderFamily();
    } else if (form.id === "villageform") {
      const fd = Object.fromEntries(new FormData(form)); fd.kind = "village"; form.querySelector(".btn.primary").disabled = true;
      try { await api("POST", `/api/patients/${S.patient}/visit`, fd); toast(t("Request sent. A health worker in your area will SMS you.")); } catch (e) { toast(tr(e.message)); }
      renderFamily();
    } else if (form.id === "consult") {
      const fd = Object.fromEntries(new FormData(form));
      try { await api("POST", `/api/patients/${S.patient}/consultations`, fd); say("ok", "Request sent. The clinic will reply by SMS."); } catch (e) { say("bad", e.message); }
      window.scrollTo(0, 0); renderFamily();
    } else if (form.id === "myemail") {
      try { const r = await api("POST", "/api/me/email", { email: new FormData(form).get("email") }); S.me.email = r.email; say("ok", r.email ? "Email saved." : "Email removed."); } catch (e) { say("bad", e.message); }
      window.scrollTo(0, 0); render();
    } else if (form.id === "addfam") {
      const fd = new FormData(form);
      try { await api("POST", `/api/patients/${S.patient}/family`, { name: fd.get("name"), relation: fd.get("relation"), phone: fd.get("phone"), email: fd.get("email"), receive_sms: fd.get("receive_sms") === "on" }); say("ok", "Added. We sent them an SMS with how to sign in."); } catch (e) { say("bad", e.message); }
      renderFamily();
    }
  });
  function setOffline() { S.offline = !navigator.onLine; if (S.me && S.me.role === "chw") { if (!S.offline) sync(); else render(); } }
  window.addEventListener("online", setOffline); window.addEventListener("offline", setOffline);

  // ---- Handlers for settings, voice, assistant, reports, reminders, reviews and visits
  document.addEventListener("click", async (ev) => {
    const t2 = ev.target.closest("[data-speak],[data-askq],[data-remdel],[data-ropen],[data-rshare],[data-rdown],[data-report],[data-held],[data-visit],[data-sha],[data-act=settings],[data-act=alerts],[data-act=assistant],[data-act=close-view],[data-act=test-voice],[data-act=allow-alerts],[data-act=export],[data-act=backup-device],[data-act=clear-device],[data-act=ask-visit]");
    if (!t2) return;
    const a = t2.dataset.act;
    if (t2.dataset.speak) return speakEl(t2.dataset.speak);
    if (t2.dataset.askq) { const e = (window.AFRIEYE_KB || []).filter((x) => x.roles.includes(S.me.role))[+t2.dataset.askq]; return ask(e.q[S.lang === "sw" ? "sw" : "en"]); }
    if (t2.dataset.report) return previewReport(t2.dataset.report, t2);
    if (t2.dataset.held) return openHeldReport((S.held || []).find((r) => r.id === +t2.dataset.held), t2);
    if (t2.dataset.remdel) { try { await api("DELETE", `/api/reminders/${t2.dataset.remdel}`); toast(t("Reminder removed.")); } catch (e) { toast(tr(e.message)); } return renderFamily(); }
    if (t2.dataset.ropen || t2.dataset.rdown || t2.dataset.rshare) {
      const id = t2.dataset.ropen || t2.dataset.rdown || t2.dataset.rshare;
      try {
        const file = await reportFile(id);
        if (t2.dataset.ropen) { window.open(URL.createObjectURL(file), "_blank"); return; }
        if (t2.dataset.rshare && navigator.canShare && navigator.canShare({ files: [file] })) { await navigator.share({ files: [file], title: t("AfriEye eye care report") }); return; }
        download(file.name, file); if (t2.dataset.rshare) toast(t("This device can't share files directly, so the report was downloaded. Send it from your files."));
      } catch (e) { if (e.name !== "AbortError") toast(tr(e.message)); }
      return;
    }
    if (t2.dataset.sha) { try { await api("POST", `/api/chw/cover-help/${t2.dataset.sha}/start`, {}); toast(t("The patient was sent an SMS.")); } catch (e) { toast(tr(e.message)); } return renderChw(); }
    if (t2.dataset.visit) { try { await api("POST", `/api/chw/visits/${t2.dataset.visit}/${t2.dataset.vact}`, {}); toast(t2.dataset.vact === "accept" ? t("Visit accepted. The patient was sent an SMS.") : t("Marked as visited.")); } catch (e) { toast(tr(e.message)); } return renderChw(); }
    if (a === "settings" || a === "alerts" || a === "assistant") { keepForm(); S.view = a; say(); window.scrollTo(0, 0); return render(); }
    if (a === "close-view") { S.view = null; say(); return render(); }
    if (a === "test-voice") return speak(t("Hello. This is how AfriEye reads aloud."));
    if (a === "allow-alerts") { if (!("Notification" in window)) return toast(t("This browser can't show alerts.")); const p = await Notification.requestPermission(); return toast(p === "granted" ? t("Alerts are on for this device.") : t("Alerts are off. You can still see them under the bell.")); }
    if (a === "export") { try { const r = await fetch("/api/me/export", { credentials: "same-origin" }); download("afrieye-my-data.json", await r.blob()); } catch (e) { toast(t("Connect to the internet to download your data.")); } return; }
    if (a === "backup-device") { const box = await Store.all(); return download(`afrieye-phone-backup-${new Date().toISOString().slice(0, 10)}.json`, new Blob([JSON.stringify({ afrieye_backup: 1, screenings: box })], { type: "application/json" })); }
    if (a === "clear-device") {
      if (!t2.dataset.sure) { t2.dataset.sure = "1"; t2.textContent = t("Press again to delete. Unsent screenings will be lost."); return; }
      for (const b of await Store.all()) await Store.del(b.client_ref);
      try { ["afrieye.directory", "afrieye.chwSummary", "afrieye.places"].forEach((k) => localStorage.removeItem(k)); } catch (e) { /* nothing stored */ }
      toast(t("Data on this phone was deleted.")); return renderSettings();
    }
    if (a === "ask-visit") { try { await api("POST", `/api/patients/${S.patient}/visit`, {}); toast(t("Request sent. A health worker in your area will SMS you.")); } catch (e) { toast(tr(e.message)); } return renderFamily(); }
  });
  document.addEventListener("change", async (ev) => {
    const el = ev.target;
    if (el.id && el.id.startsWith("lang-")) { setLang(el.value, true); $$("select[id^=lang-]").forEach((s) => { s.value = S.lang; }); if (S.view === "assistant") renderAssistant(); return; }
    if (el.name && el.name.startsWith("pref-")) { PREFS[el.name.slice(5)] = el.value; return applyPrefs(); }
    if (el.id === "voice-pick") { PREFS.voice = el.value; applyPrefs(); return speak(t("Hello. This is how AfriEye reads aloud.")); }
    if (el.id === "pref-rate" || el.id === "pref-pitch") { PREFS[el.id.slice(5)] = +el.value; applyPrefs(); return speak(t("Hello. This is how AfriEye reads aloud.")); }
    if (el.dataset.hist && S.flow) { S.flow.history[el.dataset.hist] = el.type === "checkbox" ? el.checked : el.value; if (el.dataset.hist === "diabetes_type" || el.dataset.hist === "cover") renderChw(); return; }
    if (el.dataset.cond && S.flow) { const h = S.flow.history; h.items = el.checked ? h.items.concat(el.dataset.cond) : h.items.filter((x) => x !== el.dataset.cond); return; }
    if (el.id === "restore-file" && el.files[0]) {
      try {
        const data = JSON.parse(await el.files[0].text()); if (!data.afrieye_backup || !Array.isArray(data.screenings)) throw new Error();
        for (const b of data.screenings) await Store.put(b);
        toast(tr("{n} screenings restored. They'll send when you're online.").replace("{n}", data.screenings.length)); sync();
      } catch (e) { toast(t("That isn't an AfriEye phone backup.")); }
      return renderSettings();
    }
  });
  document.addEventListener("input", (ev) => { const el = ev.target;
    if (el.id === "pref-rate") $("#rate-out").textContent = speedName(+el.value);
    if (el.id === "pref-pitch") $("#pitch-out").textContent = +el.value > 1.15 ? t("Lighter") : +el.value < 0.95 ? t("Deeper") : t("Normal");
    if (el.dataset.hist && S.flow && el.type !== "radio" && el.type !== "checkbox") S.flow.history[el.dataset.hist] = el.value; });
  document.addEventListener("submit", async (ev) => {
    const f = ev.target, fd = new FormData(f);
    const done = (msg) => { toast(msg); };
    if (f.id === "askform") { ev.preventDefault(); return ask(fd.get("q")); }
    if (f.id === "pwform") { ev.preventDefault(); try { await api("POST", "/api/me/password", { current: fd.get("current"), new: fd.get("new") }); f.reset(); done(t("Password changed.")); } catch (e) { done(tr(e.message)); } return; }
    if (f.id === "delform") { ev.preventDefault(); try { await api("POST", "/api/me/delete", { password: fd.get("password") }); S.me = null; S.view = null; remember(null); say("ok", "Your account and records were deleted."); render(); } catch (e) { done(tr(e.message)); } return; }
    if (f.id === "remform") {
      ev.preventDefault(); const ch = fd.getAll("ch");
      try { await api("POST", `/api/appointments/${f.dataset.appt}/reminders`, { when: fd.get("when"), at: fd.get("at"), channels: ch }); done(t("Reminder set.")); if (ch.includes("app") && "Notification" in window && Notification.permission === "default") Notification.requestPermission(); } catch (e) { done(tr(e.message)); }
      return renderFamily();
    }
    if (f.dataset.shadone) { ev.preventDefault(); try { await api("POST", `/api/chw/cover-help/${f.dataset.shadone}/done`, { ref: fd.get("ref") }); done(t("Saved. The patient was told by SMS.")); } catch (e) { done(tr(e.message)); } return renderChw(); }
    if (f.dataset.review) { ev.preventDefault(); try { await api("POST", `/api/patients/${S.patient}/reviews`, { target_id: +f.dataset.review, stars: +fd.get("stars"), comment: fd.get("comment") }); done(t("Thank you. Your rating was saved.")); } catch (e) { done(tr(e.message)); } return renderFamily(); }
    if (f.dataset.remail) { ev.preventDefault(); try { await api("POST", `/api/reports/${f.dataset.remail}/email`, { email: fd.get("email") }); done(t("Report emailed.")); } catch (e) { done(tr(e.message)); } }
  }, true);

  // ---- Start
  async function start() {
    S.offline = !navigator.onLine; loadPrefs();
    let saved = null; try { saved = localStorage.getItem("afrieye.lang"); } catch (e) { /* default */ }
    setLang(saved || "en");
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
