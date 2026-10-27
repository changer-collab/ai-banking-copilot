/* 静态壳 v11：金融 API 始终在线读取，绝不进入 Cache Storage。 */
const CACHE_NAME = "aib-static-shell-v11";
const STATIC_ASSETS = [
  "/",
  "/assets/manifest.json?v=1",
  "/assets/icon.svg?v=1",
  "/assets/tokens.css?v=2",
  "/assets/app.css?v=6",
  "/assets/workspace.css?v=5",
  "/assets/app.js?v=17",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || request.method !== "GET") return;
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request));
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then((response) => response).catch(() => caches.match("/"))
    );
    return;
  }
  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      if (url.pathname.startsWith("/assets/")) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      }
      return response;
    }))
  );
});
