const CACHE = "carenote-shell-v4";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/api.js",
  "/static/i18n.js",
  "/static/dom.js",
  "/static/agent-panel.js",
  "/static/knowledge-panel.js",
  "/static/demo.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("activate", (event) => {
  // Drop shells cached by earlier versions, then take over open pages.
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || event.request.url.includes("/api/")) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
