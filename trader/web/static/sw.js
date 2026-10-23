/* AI Trader service worker: makes the app installable and the shell fast.
 *
 * - /static/*    cache-first from a versioned cache (refreshed in the background so a
 *                deploy reaches the phone on the next launch even if VERSION was not bumped)
 * - /api/*       network only, never cached (live trading data, authenticated)
 * - navigations  network first; offline -> a short offline page
 *
 * Bump VERSION whenever a file in /static changes: the old cache is dropped on activate.
 */
'use strict';

const VERSION = 'v4';
const CACHE = `trader-shell-${VERSION}`;
const SHELL = [
  '/static/styles.css',
  '/static/app.js',
  '/static/login.js',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/manifest.webmanifest',
];

const OFFLINE_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Offline · AI Trader</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body><main class="login"><div class="card login-card">
<div class="login-brand"><img src="/static/icon.svg" alt="" width="48" height="48"><h1>You are offline</h1></div>
<p>AI Trader needs a connection to show live data. The bot keeps running on the server; only this device is offline.</p>
<a class="btn btn-primary btn-block" href="/">Try again</a>
</div></main></body></html>`;

// Only same-origin, successful, non-redirected answers go in the cache (never a proxy's
// login page or an error).
const cacheable = (res) => res && res.ok && res.type === 'basic' && !res.redirected;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(SHELL.map(async (path) => {
      try {
        const res = await fetch(new Request(path, { cache: 'reload', credentials: 'same-origin' }));
        if (cacheable(res)) await cache.put(path, res);
      } catch {
        /* cached on first use instead */
      }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith('trader-shell-') && name !== CACHE)
      .map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

const cacheFirst = async (event) => {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(event.request);
  const network = fetch(event.request).then(async (res) => {
    if (cacheable(res)) await cache.put(event.request, res.clone());
    return res;
  });
  if (cached) {
    event.waitUntil(network.catch(() => undefined));
    return cached;
  }
  return network;
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
    event.respondWith(cacheFirst(event));
  }
});
