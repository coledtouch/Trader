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

const VERSION = 'v17';
const ASSET_VERSION = VERSION.slice(1);
const CACHE = `trader-shell-${VERSION}`;
const SHELL = [
  `/static/styles.css?v=${ASSET_VERSION}`,
  `/static/app.js?v=${ASSET_VERSION}`,
  `/static/login.js?v=${ASSET_VERSION}`,
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
// The browser fetches a notification's images itself, outside this worker and its cache. Behind
// Cloudflare Access such a fetch fails once the Access session has expired, so they are inline.
const ALERT_ICON = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAE70lEQVR42u3cz4tVZRjA8fs/FBFIKgxNPxShoEFksgH7xYhlo004Mca0KAQXKS1CF7MQcacLMWjTxtpkLQJr5aKSNkkbG2gntBL6K3riDpdhHGfuPfecc8/7vh/4LsaZucg58/Ge89z7OL0n9h6SKtdzCgSQABJAAkgCSAAJIAEkASSABJAAkgASQAJIAEkACSABJIAEkASQABJAAkgCSAAJIAEkASSABJAAkgASQAJIAAkgCSABJIAEkASQABJAAkgCSAAJIAEkASSABJAAkgASQFm3+7X3nj3+URQfAKRh2/fx2Te/u/nhP3+e/ndtUPwxPhlfAkiPLZ5sjt35caObR4tviG8DSJt7+fML29PZWHwzQKqopyOGAOrQlWtUPf0mey0DqCvteN+zzf0QQGaus9X09JvgXAZQJ4rhfBxA8XCAim7T6z2jFg8HqOjXmsfR029Sr1MDlPD81YVZDCCAAHIJcwkrudlrV8fU4ya63A5evjL+048xvtBeubg6vh4vJBbaS+e+qEWPtzJK7MCnn9Wix5upJfbi6TN16bHOUVzTJ1eWH97PQw9AbTc1v3Tqwb1a7nustBbXnrmFxbW7Y77eY6m+0HbNHF34404FNAcvX/Hfekrv6f1H3vnldgU9s9eudvzQAGq8J6dmj/78QwU9c1992f2jA6jx3vr+2wp6jtz8OomjA6jZwkEFPWEulQMEqMHiGlRBT1zv4qoHkCWNKksaca8dd9wJHSZAHVrSiDk/pv20jhSgrixpLK7d3TO3kNzB5g/o+aVPDt+4Pn/7VhQfxB87uKRx6sG9qfmlFE9vzoB2v3o80Dz604pPxpe6s6Sx/PD+9MmVRE9ytoD2vr74/l+/Pe5n9sHfv0+fqPlnVnlJIx6Y7nnuFahn0IEz5ye+pBFPWkmf6l6xevrNrF6a4JJG3DClfrZ7JevpF3fWE1nSiGEtgxPeK1zP+lsHt7556rm5lpc08jjnPXr6vfvrT6OOZhkvaRQHaEw9FUazvJc0ygJUi55RR7O8lzQKAlSvniFHs+yXNEoB1ISeHUezEpY0igDUnJ5tRrNCljTyB9S0ni1Hs3KWNDIH1I6eTaNZUUsaOQNqU8/G9xyKWtLIFlBlPfGomM/bNJf0kkaegMbRE4/9/z3zEytxSWoHUNJLGhkCGl/PYMssbo2b1pP6kkZugOrS0y+G8xjRG71hKmcBvFeankGHb1xvQk8eSxr5AGpIT7+Z1Uv16slmSSMTQI3qWV+Dr280y2lJIwdALehZX2euYzTLbEkjeUCt6allNMtvSSNtQC3rGXM0y3JJI2FAE9FTeTTLdUkjVUCT1TPqaJbxkkaSgLqgZ/jRLO8ljfQAdUfPMKNZ/KV5L2kkBqhregajWcu/mwGgfPQMavm3wwA08r/yLutR1wFteY2gB6Bhrw70ANTeq3b0ADTW9YsegKoDogeg6pcwegCqfhNND0DVr2L0AFT9hUR6APKWE0Bt3Q95ywkgASQBJIAEkACSABJAAkgASQApY0C7XnjjmX1va5viFAG0dftnlw8dO68dixMF0BbPPWQMX3eehwACyCXMJQwgN9FuomWMlwASQAJIAEkACSABJIAkgASQABJAEkACSAAJIAEkASSABJAAkgASQAJIAEkACSABJIAkgASQABJAAkgCSAAJIAEkASSAlET/AaYMBVkiwk4NAAAAAElFTkSuQmCC';
// Android status bar: a white glyph on transparent.
const ALERT_BADGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAAFA0lEQVR4nOybS4gdRRiFz2hAjYJxJygaUFB8C47xhYiS+BgQXMzGpcGFCDLGqCBqVAiiQ5i4MK7iIuDCjQshL0UFH0FFiFE3Im5EUCM+svGdjOen+8JlvF3d1VXV/zWcD35qmFvd1X1OVfXf1d2rIFxZBeGKDHBGBjgjA5yRAc7IAGdkgDMywBkZ4IwMcEYGOCMDnJEBzsgAZ2SAMzLAGRngjAxwRgY4IwOckQHOyABnZIAzMsAZGRDJ8vLyJSxuZ6xjXFP/+8M69s3MzHyBCGYgOkHhT2bxFGMz48SGakcZzzO20Ii/0QEZ0AGKfzmLVxkXdNzkIGMjTTjYVlEGtEDxT2VxiHEe4viacRFN+CtU6QSINrYiXnzU2zzTVkkjIAB7/3Us3kP/jnqMcSNHwQdNFTQCwjyANI1s23tDFZSGhrkQ6QT3oSkoAKegX1mcjjSOcApa0/SjpqAwfyCd4D5kQJjPkc6noR91DWiA049Nz+cjHRnQkxcZa5GG3YTtDFWQARNg719gcR/SsTWhr0IVlAWtgOLPsXgd6ddHWweapQFHQ5VkwBgU/1IWHzFOQRom+myXxbipyoJMAMYuxmHGEcYBxgYMANs5m8UbSBPfhH/O9tFFfGNqRgAF2IJqvX0SO3hC96Nc27bieYBxGeL4CVWebw9jbOTs/V8+kKEASywWWqrtZdzFE/wTeds2DfYz1iOOBR7LC0jEfQrqKL5hjwHfZ/0zkBdLN2PFfymH+IbrCIgQfxx70HEzBfgGidTp5lLcVniTcSvbX0YG3AzoKf6IHxnrKcIh9KRnuvkZ41q2+xsy4WJAovgjTIQ7KcZbiKRnuvk94wq29wMyMvg1IJP4xmrGfu7v7piNWP8sxKebvzM25BbfGNQAnvwi8og/wl4PeYX7faxLZdY7jcUexpmIY57i51gZ/Q+DGVD3/M0ow1buf2edUja1b7+9hvhc39LN3SjEIAZknHZC3MPYzbZOavjdNd1sovhFmILYm2IPYzjsrnSOwv08dgz2cD1WyD3cxxwKU3QE1D2/j/jbGVeiSjdjsfc1P2bb59bHYCLG5vqWbs5jAIqNgISev8ie90i9j7Us9qH7K4HjHGY8imrqWR2x3XeMq3kM32IAihiQMOdv54k/uGJftvRgaeNVKI+lm+tKZTyTyD4F5RTf4P9+YXEDqsW4kthbbPNDim9kNSC3+CPqFVCby19GOTaVTDebyGZAwk3WYkj8Ebb4xdjIPx9HfnaUTjebyHINKNXzA+3Z8sMuNH8oEUPW1c1Ykg0YWvyxdm9BtZoZk+GsJPvqZixJU1CdahabdkLUq6DXo0o3+2Dp5pyn+EbvEeDV8yccxzks3kbcRxSDp5tN9BoB0yK+UT8Zm2V80nETl3SziWgDpkn8EZH3Ci7pZhNRBpRONVOwewXGHaiWHprY5JVuNtH5GlCL32c9fxtPutRzgInwWG9jYe8ZXYxqynmHscTjeBdTRicDpnHaOV5onYIo/tPoJ/42id9OcARQ/JtQDd9Y1PM70vZ9wBOIR+JH0DYCLL1bg+5I/EjaRsAxdEfi96DtItzpHXdI/N60GfAs2pH4CQQNqFccHwpUeVLip9H1RszeSjAj7HOhf1CtPlqe/yVEEvpIzxl9J+yMDHBGBjgjA5yRAc7IAGdkgDMywBkZ4IwMcEYGOCMDnJEBzsgAZ2SAMzLAGRngjAxw5l8AAAD//9N38GsAAAAGSURBVAMAcqCZ3m/EkJkAAAAASUVORK5CYII=';

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
      icon: ALERT_ICON,
      badge: ALERT_BADGE,
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
