// Offline app shell: the page works without a network once opened (Bluetooth needs none).
// The cache is named after the release (release-please updates it), so each release starts clean.

const CACHE = 'guoran-nixie-0.2.0'; // x-release-please-version
const SHELL = ['./', 'index.html', 'styles.css', 'app.js', 'ble.js', 'protocol.js', 'icon.svg', 'manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

// Network first, so an update shows on the next load; the cache only when offline. "no-cache"
// makes the browser check with the server too: its HTTP cache could otherwise serve a stale file.
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    fetch(new Request(event.request.url, { cache: 'no-cache' }))
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
