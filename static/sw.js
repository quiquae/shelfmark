/* shelfmark service worker.
 *
 * Scope is deliberately narrow. The shell is cached so the app opens
 * instantly from a home screen, and NOTHING else is: a cached /job/ or
 * /collection/ page would show a librarian a record they have already
 * reviewed, or a queue that has already drained, and they would review it
 * again. A catalogue is not a document you want a stale copy of.
 *
 * Offline sync is out of scope on purpose. Uploading a photograph and
 * resolving it both need the network; pretending otherwise would queue work
 * that silently never happens.
 */
const CACHE = "shelfmark-shell-v1";
const SHELL = [
  "/static/app.css",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/favicon.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const cacheable =
    e.request.method === "GET" &&
    url.origin === self.location.origin &&
    url.pathname.startsWith("/static/") &&
    // Spine crops are per-record and there are thousands of them; caching
    // them would fill the device for no benefit.
    !url.pathname.includes("/spine/");

  if (!cacheable) return;   // everything else goes to the network, always

  e.respondWith(
    caches.match(e.request).then(
      (hit) =>
        hit ||
        fetch(e.request).then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        })
    )
  );
});
