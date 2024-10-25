const CACHE_NAME = 'apollo-cache-static-v12';

const CACHED_URLS = [
  '/pwa/',
  '/pwa/static/css/main.css',
  '/pwa/static/img/icons-48.png',
  '/pwa/static/img/icons-72.png',
  '/pwa/static/img/icons-96.png',
  '/pwa/static/img/icons-192.png',
  '/pwa/static/js/app.js',
  '/pwa/static/js/client.js',
  '/pwa/static/vendor/animate.css/animate.min.css',
  '/pwa/static/vendor/autocomplete/autocomplete.min.js',
  '/pwa/static/vendor/autocomplete/style.css',
  '/pwa/static/vendor/bootstrap/css/bootstrap.min.css',
  '/pwa/static/vendor/bootstrap/css/bootstrap.rtl.min.css',
  '/pwa/static/vendor/bootstrap/js/bootstrap.min.js',
  '/pwa/static/vendor/dexie/dexie.min.js',
  '/pwa/static/vendor/fast-copy/fast-copy.min.js',
  '/pwa/static/vendor/image-blob-reduce/image-blob-reduce.min.js',
  '/pwa/static/vendor/luxon/luxon.min.js',
  '/pwa/static/vendor/notiflix/notiflix-2.7.0.min.css',
  '/pwa/static/vendor/notiflix/notiflix-2.7.0.min.js',
  '/pwa/static/vendor/popper.js/popper.min.js',
  '/pwa/static/vendor/tippy.js/tippy.umd.min.js',
  '/pwa/static/vendor/sentry/bundle.min.js',
  '/pwa/static/vendor/vue/vue.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CACHED_URLS)));
});

self.addEventListener('fetch', (event) => {
  const requestURL = new URL(event.request.url);
  if (requestURL.pathname === '/pwa/') {
    event.respondWith(
      caches.open(CACHE_NAME).then(
        cache => cache.match('/pwa/').then(
          cacheResponse => {
            let fetchPromise = fetch('/pwa/').then(
              networkResponse => {
                cache.put('/pwa/', networkResponse.clone());
                return networkResponse;
              }
            );

            return cacheResponse || fetchPromise;
          }
        )
      )
    )
  } else if (CACHED_URLS.includes(requestURL.href) || CACHED_URLS.includes(requestURL.pathname)) {
    event.respondWith(
      caches.open(CACHE_NAME).then(
        cache => cache.match(event.request)
          .then(response => response || fetch(event.request))
      )
    );
  }
});


self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(
      (cacheNames) => Promise.all(cacheNames.map((cacheName) => {
        if (CACHE_NAME !== cacheName && cacheName.startsWith('apollo-cache-static')) {
          return caches.delete(cacheName);
        }
      }))
    )
  );
});
