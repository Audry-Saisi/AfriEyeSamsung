// Keeps the app and the AI model on the phone, so that scanning works with no internet.
const CACHE = "afrieye-v3";
const FILES = ["/", "/static/style.css", "/static/engine.js", "/static/model.js", "/static/app.js", "/static/manifest.webmanifest", "/static/icon.svg", "/static/samples.json", "/static/login.jpg", "/api/places"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys().then((k) => Promise.all(k.filter((n) => n !== CACHE).map((n) => caches.delete(n)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || (u.pathname.startsWith("/api/") && u.pathname !== "/api/places")) return;
  e.respondWith(fetch(e.request).then((r) => { if (r.ok) { const k = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, k)); } return r; }).catch(() => caches.match(e.request).then((m) => m || Response.error())));
});
