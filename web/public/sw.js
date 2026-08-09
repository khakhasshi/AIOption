const CACHE_NAME = 'ai-option-pwa-v8';
const APP_SHELL = ['/', '/manifest.webmanifest', '/logo.png', '/favicon.ico', '/favicon-16x16.png', '/favicon-32x32.png', '/apple-touch-icon.png', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      // Use individual puts so one missing optional asset can't abort the whole
      // install (addAll rejects atomically if any request 404s).
      .then((cache) => Promise.all(APP_SHELL.map((url) => cache.add(url).catch(() => undefined))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

// Allow the page to ask a freshly-installed worker to take over immediately.
self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting' || event.data?.type === 'skip-waiting') {
    self.skipWaiting();
  }
});

function offlineResponse(message) {
  return new Response(message || 'offline', {
    status: 503,
    statusText: 'Service Unavailable',
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  // Only handle same-origin GETs; never intercept API calls or cross-origin
  // requests (fonts, third-party scripts) so they fail/route normally.
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
    return;
  }

  // Navigations: always try the network first so a new deploy's index.html
  // (with fresh chunk hashes) is fetched; fall back to the cached shell offline.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put('/', copy)).catch(() => {});
          }
          return response;
        })
        .catch(async () => (await caches.match('/')) || (await caches.match(request)) || offlineResponse('网络不可用，请稍后重试')),
    );
    return;
  }

  // Hashed build assets under /assets/ are immutable: serve from cache first to
  // cut requests, but fall back to the network on a miss (and never resolve to
  // undefined, which would throw "Failed to convert value to 'Response'").
  const isHashedAsset = url.pathname.startsWith('/assets/');
  if (isHashedAsset) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request)
          .then((response) => {
            if (response && response.ok) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
            }
            return response;
          })
          .catch(async () => (await caches.match(request)) || offlineResponse('资源加载失败'));
      }),
    );
    return;
  }

  // Everything else (icons, manifest, etc.): network-first, cache fallback.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(async () => (await caches.match(request)) || offlineResponse('资源加载失败')),
  );
});
