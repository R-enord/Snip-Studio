// SnipRiot Studio service worker: makes the app installable, keeps the shell working offline, shows push alerts.
const SHELL = "snipriot-shell-v1";
const FILES = ["/app", "/manifest.webmanifest", "/icons/icon-192.png", "/fonts/Anton-Regular.ttf"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/api/") || u.pathname.startsWith("/media/")) return;   // live data never from cache
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(s => s.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request).then(r => r || caches.match("/app"))));
});
self.addEventListener("push", e => {
  let d = {}; try { d = e.data.json(); } catch (_) { d = { title: "SnipRiot", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "SnipRiot", { body: d.body || "", icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", data: { url: d.url || "/app#home" } }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window" }).then(ws => { for (const w of ws) if ("focus" in w) { w.navigate(e.notification.data.url); return w.focus(); } return clients.openWindow(e.notification.data.url); }));
});
