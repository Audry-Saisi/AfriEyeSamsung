// AfriEye on-device engine: preprocessing, CNN inference, CAM and triage rules.
// Mirrors afrieye/preprocess.py, afrieye/nn.py and afrieye/triage.py. No dependencies.
(function (root) {
  const b64 = (s) => { const b = atob(s), n = b.length, u = new Uint8Array(n); for (let i = 0; i < n; i++) u[i] = b.charCodeAt(i); return new Float32Array(u.buffer); };

  function loadModel(j) {
    const m = { size: j.size, thresholds: j.thresholds, layers: j.layers.map((l) => ({ w: b64(l.w), b: b64(l.b), g: b64(l.g), bt: b64(l.bt), mean: b64(l.mean), var: b64(l.var), cin: l.cin, cout: l.cout })), hw: b64(j.hw), hb: b64(j.hb), nOut: j.hb_n };
    return m;
  }

  function conv(x, H, W, L) {
    const { cin, cout, w, b } = L, out = new Float32Array(cout * H * W);
    for (let k = 0; k < cout; k++) {
      const ko = k * H * W; out.fill(b[k], ko, ko + H * W);
      for (let c = 0; c < cin; c++) {
        const ci = c * H * W;
        for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
          const wt = w[(c * 9 + i * 3 + j) * cout + k]; if (wt === 0) continue;
          const dy = i - 1, dx = j - 1;
          const y0 = Math.max(0, -dy), y1 = Math.min(H, H - dy), x0 = Math.max(0, -dx), x1 = Math.min(W, W - dx);
          for (let y = y0; y < y1; y++) {
            let o = ko + y * W + x0, s = ci + (y + dy) * W + x0 + dx;
            for (let e = o + (x1 - x0); o < e; o++, s++) out[o] += wt * x[s];
          }
        }
      }
    }
    return out;
  }

  function bnRelu(x, H, W, L) {
    const n = H * W;
    for (let k = 0; k < L.cout; k++) {
      const s = 1 / Math.sqrt(L.var[k] + 1e-5), mu = L.mean[k], g = L.g[k], bt = L.bt[k];
      for (let o = k * n, e = o + n; o < e; o++) { const v = (x[o] - mu) * s * g + bt; x[o] = v > 0 ? v : 0; }
    }
    return x;
  }

  function pool(x, C, H, W) {
    const h = H >> 1, w = W >> 1, out = new Float32Array(C * h * w);
    for (let c = 0; c < C; c++) for (let y = 0; y < h; y++) for (let t = 0; t < w; t++) {
      const a = c * H * W + y * 2 * W + t * 2;
      let v = x[a]; if (x[a + 1] > v) v = x[a + 1]; if (x[a + W] > v) v = x[a + W]; if (x[a + W + 1] > v) v = x[a + W + 1];
      out[c * h * w + y * w + t] = v;
    }
    return out;
  }

  // input: Float32Array CHW, already scaled with toInput.
  function forward(m, input) {
    let x = input, H = m.size, W = m.size;
    m.layers.forEach((L, i) => {
      x = bnRelu(conv(x, H, W, L), H, W, L);
      if (i < m.layers.length - 1) { x = pool(x, L.cout, H, W); H >>= 1; W >>= 1; }
    });
    const C = m.layers[m.layers.length - 1].cout, n = H * W, z = new Float64Array(C);
    for (let c = 0; c < C; c++) { let s = 0; for (let o = c * n, e = o + n; o < e; o++) s += x[o]; z[c] = s / n; }
    const logit = Array.from(m.hb, (b, h) => { let s = b; for (let c = 0; c < C; c++) s += z[c] * m.hw[c * m.hb.length + h]; return s; });
    const p = logit.map((v) => 1 / (1 + Math.exp(-v)));
    const cam = new Float32Array(n);
    for (let c = 0; c < C; c++) { const wt = m.hw[c * m.hb.length + 1]; for (let i = 0; i < n; i++) cam[i] += wt * x[c * n + i]; }
    return { p, cam, camSize: H };
  }

  function boxBlur(a, s, r) { // a: Float32Array of s*s, three channels interleaved handled by caller
    const t = new Float32Array(s * s), d = 2 * r + 1;
    for (let y = 0; y < s; y++) {
      let sum = 0; for (let i = -r; i <= r; i++) sum += a[y * s + Math.min(s - 1, Math.max(0, i))];
      for (let x = 0; x < s; x++) { t[y * s + x] = sum / d; sum += a[y * s + Math.min(s - 1, x + r + 1)] - a[y * s + Math.max(0, x - r)]; }
    }
    const o = new Float32Array(s * s);
    for (let x = 0; x < s; x++) {
      let sum = 0; for (let i = -r; i <= r; i++) sum += t[Math.min(s - 1, Math.max(0, i)) * s + x];
      for (let y = 0; y < s; y++) { o[y * s + x] = sum / d; sum += t[Math.min(s - 1, y + r + 1) * s + x] - t[Math.max(0, y - r) * s + x]; }
    }
    return o;
  }

  // rgb: Uint8 HWC (s*s*3) -> Uint8 HWC with background removed and circular mask.
  function localContrast(rgb, s) {
    const r = Math.max(1, Math.round(s * 0.06)), out = new Uint8ClampedArray(s * s * 3), c0 = (s - 1) / 2, lim = Math.pow(0.97 * s / 2, 2);
    for (let c = 0; c < 3; c++) {
      let a = new Float32Array(s * s); for (let i = 0; i < s * s; i++) a[i] = rgb[i * 3 + c];
      let b = a; for (let k = 0; k < 3; k++) b = boxBlur(b, s, r);
      for (let y = 0; y < s; y++) for (let x = 0; x < s; x++) {
        const i = y * s + x, ins = (x - c0) * (x - c0) + (y - c0) * (y - c0) <= lim;
        out[i * 3 + c] = ins ? Math.round(Math.min(255, Math.max(0, (a[i] - b[i]) * 4 + 128))) : 128;
      }
    }
    return out;
  }

  function toInput(u8, s) {
    const x = new Float32Array(3 * s * s);
    for (let c = 0; c < 3; c++) for (let i = 0; i < s * s; i++) x[c * s * s + i] = (u8[i * 3 + c] - 128) / 64;
    return x;
  }

  function photoProblem(c) {
    if (c.centre_brightness < 25) return "Too dark. Check the light and retake.";
    if (c.centre_brightness > 235) return "Overexposed. Reduce the light and retake.";
    if (c.red_dominance < 1.25 || c.corner_brightness > 60) return "This does not look like a retina. Centre the pupil and retake.";
    // Browser downscaling keeps more detail than PIL, so this differs from Python (1.5). Set from 100 validation photos in Chromium.
    if (c.sharpness < 2.5) return "Blurred. Hold still, refocus and retake.";
    return null;
  }

  // rgba: canvas pixels at 128x128.
  function photoChecks(rgba) {
    const n = 128, lum = new Float32Array(n * n); let cs = 0, cn = 0, rs = 0, bs = 0, ks = 0, kn = 0;
    for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) {
      const i = (y * n + x) * 4, R = rgba[i], G = rgba[i + 1], B = rgba[i + 2], l = (R + G + B) / 3, rr = Math.hypot(x - 63.5, y - 63.5) / 64;
      lum[y * n + x] = l;
      if (rr < 0.6) { cs += l; cn++; rs += R; bs += B; } else if (rr > 1.05) { ks += l; kn++; }
    }
    let m = 0, m2 = 0, c = 0;
    for (let y = 1; y < n - 1; y++) for (let x = 1; x < n - 1; x++) {
      if (Math.hypot(x - 63.5, y - 63.5) / 64 >= 0.8) continue;
      const v = 4 * lum[y * n + x] - lum[(y - 1) * n + x] - lum[(y + 1) * n + x] - lum[y * n + x - 1] - lum[y * n + x + 1];
      m += v; m2 += v * v; c++;
    }
    m /= c;
    return { centre_brightness: cs / cn, corner_brightness: ks / Math.max(1, kn), red_dominance: (rs / cn) / Math.max(1, bs / cn), sharpness: Math.sqrt(Math.max(0, m2 / c - m * m)) };
  }

  function triage(p, t, retakes, maxRetakes) {
    maxRetakes = maxRetakes === undefined ? 2 : maxRetakes;
    // Positive scores act first, so a retake never delays a referral.
    if (p[2] >= t.urgent) return { cat: "Urgent referral", msg: "Signs of possible severe or proliferative DR. Refer urgently.", level: "urgent" };
    if (p[1] >= t.referable) return { cat: "Refer", msg: "Signs of possible referable DR. Refer to an eye clinic.", level: "refer" };
    if (p[4] < t.quality) {
      if (retakes < maxRetakes) return { cat: "Retake photo", msg: "Image quality is too low for a reliable result. Retake the photo.", level: "retake" };
      return { cat: "Refer", msg: "Still ungradable after retakes. Refer for an in-person eye exam.", level: "refer" };
    }
    return { cat: "Rescreen in 12 months", msg: "No referable DR found. This is not a full eye exam, and oedema was not checked.", level: "rescreen" };
  }

  const api = { loadModel, forward, localContrast, toInput, photoChecks, photoProblem, triage };
  if (typeof module !== "undefined") module.exports = api; else root.AfriEngine = api;
})(typeof window !== "undefined" ? window : globalThis);
