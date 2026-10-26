/* AI Trader service worker: makes the app installable and the shell fast.
 *
 * - JS/CSS       network first, revalidated; exact-release cache only while offline
 * - other static cache first, refreshed in the background
 * - /api/*       network only, never cached (live trading data, authenticated)
 * - navigations  network first; offline -> a short offline page
 * - push         phone alerts from the server (trader/webpush.py), shown even when no
 *                window is open; tapping one opens or focuses the app
 *
 * Bump VERSION and the HTML's JS/CSS query version together on each shell release.
 * Query versions also bypass caches of an older worker that still controls the first
 * navigation during an upgrade. Never ignore the query when looking up cached assets.
 */
'use strict';

const VERSION = 'v16';
const ASSET_VERSION = VERSION.slice(1);
const CACHE = `trader-shell-${VERSION}`;
const SHELL = [
  `/static/styles.css?v=${ASSET_VERSION}`,
  `/static/app.js?v=${ASSET_VERSION}`,
  `/static/login.js?v=${ASSET_VERSION}`,
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/badge-96.png',
  '/static/manifest.webmanifest',
];

const OFFLINE_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Offline · AI Trader</title>
<link rel="stylesheet" href="/static/styles.css?v=${ASSET_VERSION}"></head>
<body><main class="login"><div class="card login-card">
<div class="login-brand"><img src="/static/icon.svg" alt="" width="48" height="48"><h1>You are offline</h1></div>
<p>AI Trader needs a connection to show live data. The bot keeps running on the server; only this device is offline.</p>
<a class="btn btn-primary btn-block" href="/">Try again</a>
</div></main></body></html>`;

// Only same-origin, successful, non-redirected answers go in the cache (never a proxy's
// login page or an error).
const cacheable = (res, request) => {
  if (!res || !res.ok || res.type !== 'basic' || res.redirected) return false;
  const mime = (res.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
  if (mime === 'text/html') return false; // a login page can also arrive without a redirect
  const path = new URL(typeof request === 'string' ? request : request.url, self.location.origin).pathname;
  if (path.endsWith('.css')) return mime === 'text/css';
  if (path.endsWith('.js')) return /^(text|application)\/(x-)?(javascript|ecmascript)$/.test(mime);
  return true;
};

const openCache = async () => {
  try { return await caches.open(CACHE); } catch { return null; }
};

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await openCache();
    if (cache) await Promise.all(SHELL.map(async (path) => {
      try {
        const res = await fetch(new Request(path, { cache: 'reload', credentials: 'same-origin' }));
        if (cacheable(res, path)) await cache.put(path, res);
      } catch {
        /* cached on first use instead */
      }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    try {
      const names = await caches.keys();
      await Promise.all(names
        .filter((name) => name.startsWith('trader-shell-') && name !== CACHE)
        .map((name) => caches.delete(name)));
    } catch { /* optional storage must not prevent the updated worker taking control */ }
    await self.clients.claim();
  })());
});

const cacheFirst = async (event) => {
  const cache = await openCache();
  let cached;
  try { if (cache) cached = await cache.match(event.request); } catch { /* network still works */ }
  const network = fetch(event.request).then(async (res) => {
    if (cache && cacheable(res, event.request)) {
      try { await cache.put(event.request, res.clone()); } catch { /* caching is optional */ }
    }
    return res;
  });
  if (cached) {
    event.waitUntil(network.catch(() => undefined));
    return cached;
  }
  return network;
};

const networkFirstCode = async (request) => {
  let res;
  try {
    // Revalidate the browser's HTTP cache as well as bypassing the worker cache.
    res = await fetch(new Request(request, { cache: 'no-cache' }));
  } catch (err) {
    const cache = await openCache();
    try {
      const cached = cache && await cache.match(request); // exact release query only
      if (cached) return cached;
    } catch { /* preserve the original network error when offline storage also fails */ }
    throw err;
  }
  if (cacheable(res, request)) {
    const cache = await openCache();
    try { if (cache) await cache.put(request, res.clone()); } catch { /* caching is optional */ }
  }
  return res; // authentication/server failures must not silently execute cached code
};

const networkFirstPage = async (request) => {
  try {
    return await fetch(request);
  } catch {
    return new Response(OFFLINE_HTML, {
      status: 503,
      headers: {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'none'; frame-ancestors 'none'; base-uri 'none'",
      },
    });
  }
};

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return; // network only
  if (request.mode === 'navigate') {
    event.respondWith(networkFirstPage(request));
  } else if (url.pathname.startsWith('/static/')) {
    event.respondWith(/\.(js|css)$/.test(url.pathname)
      ? networkFirstCode(request)
      : cacheFirst(event));
  }
});

// ------------------------------------------------------------------ phone alerts
const alertFrom = (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    try { data = { body: event.data.text() }; } catch { data = {}; }
  }
  if (!data || typeof data !== 'object') data = {};
  const text = (value, limit) => (typeof value === 'string' ? value.slice(0, limit) : '');
  const ts = Date.parse(data.ts);
  return {
    title: text(data.title, 120) || 'AI Trader',
    options: {
      body: text(data.body, 1500),
      icon: '/static/icon-192.png',
      badge: '/static/badge-96.png', // Android status bar: white glyph on transparent
      timestamp: Number.isFinite(ts) ? ts : Date.now(),
      requireInteraction: data.priority === 'urgent' || data.priority === 'max',
      data: { url: '/' },
    },
  };
};

self.addEventListener('push', (event) => {
  const { title, options } = alertFrom(event);
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (open && 'focus' in open) return open.focus();
    return self.clients.openWindow('/');
  })());
});
