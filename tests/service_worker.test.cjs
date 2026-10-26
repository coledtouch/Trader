/* Run with: node --test tests/service_worker.test.cjs
 * Execute the actual worker against browser Cache/Fetch contracts. No browser package.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const STATIC = path.join(__dirname, '..', 'trader', 'web', 'static');
const SOURCE = fs.readFileSync(path.join(STATIC, 'sw.js'), 'utf8');
const VERSION = /const VERSION = '(v\d+)'/.exec(SOURCE)[1];
const RELEASE = VERSION.slice(1);
const ORIGIN = 'https://trader.example';

const urlKey = (value) => new URL(typeof value === 'string' ? value : value.url, ORIGIN).href;

class WorkerRequest {
  constructor(input, init = {}) {
    Object.assign(this, typeof input === 'object' ? input : {}, init);
    this.url = urlKey(input);
    this.method ||= 'GET';
    this.mode ||= 'same-origin';
  }
}

function response(body, { status = 200, mime = 'text/javascript', redirected = false,
  type = 'basic' } = {}) {
  return {
    body, status, ok: status >= 200 && status < 300, redirected, type,
    headers: new Headers({ 'Content-Type': mime }),
    clone() { return response(body, { status, mime, redirected, type }); },
  };
}

function worker(network = async () => response('fresh')) {
  const listeners = new Map();
  const stores = new Map();
  const requests = [];
  const lifecycleCalls = [];
  const shown = [];
  const opened = [];
  const windows = [];
  let quotaFailure = false;
  let storageFailure = false;
  let readFailure = false;
  const cache = (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const values = stores.get(name);
    return {
      async match(request, options) {
        if (readFailure) throw new Error('cache read unavailable');
        assert.notEqual(options?.ignoreSearch, true, 'asset release query must remain part of key');
        return values.get(urlKey(request))?.clone();
      },
      async put(request, value) {
        if (quotaFailure) throw new Error('quota exceeded');
        values.set(urlKey(request), value.clone());
      },
    };
  };
  vm.runInNewContext(SOURCE, {
    URL, Headers, Request: WorkerRequest, Response,
    self: {
      location: { origin: ORIGIN },
      addEventListener(name, fn) { listeners.set(name, fn); },
      async skipWaiting() { lifecycleCalls.push('skipWaiting'); },
      clients: {
        async claim() { lifecycleCalls.push('claim'); },
        async matchAll(options) {
          assert.equal(options.type, 'window'); // objects from the worker's own realm
          assert.equal(options.includeUncontrolled, true);
          return windows;
        },
        async openWindow(url) { opened.push(url); },
      },
      registration: {
        async showNotification(title, options) { shown.push({ title, options }); },
      },
    },
    caches: {
      async open(name) {
        if (storageFailure) throw new Error('CacheStorage unavailable');
        return cache(name);
      },
      async keys() {
        if (storageFailure) throw new Error('CacheStorage unavailable');
        return [...stores.keys()];
      },
      async delete(name) { return stores.delete(name); },
    },
    async fetch(request) {
      requests.push(request);
      return network(request);
    },
  });
  return {
    requests, stores, lifecycleCalls, shown, opened, windows,
    async dispatch(name, event) {
      const pending = [];
      listeners.get(name)({ ...event, waitUntil(task) { pending.push(task); } });
      await Promise.all(pending);
    },
    setQuotaFailure() { quotaFailure = true; },
    setStorageFailure() { storageFailure = true; },
    setReadFailure() { readFailure = true; },
    async seed(request, value, name = `trader-shell-${VERSION}`) {
      await cache(name).put(request, value);
    },
    async cached(request) { return cache(`trader-shell-${VERSION}`).match(request); },
    async lifecycle(name) {
      const pending = [];
      listeners.get(name)({ waitUntil(task) { pending.push(task); } });
      await Promise.all(pending);
    },
    async request(input, init = {}) {
      let pending;
      const background = [];
      listeners.get('fetch')({
        request: new WorkerRequest(input, init),
        respondWith(task) { pending = task; },
        waitUntil(task) { background.push(task); },
      });
      if (pending === undefined) return undefined; // browser performs ordinary network fetch
      const result = await pending;
      await Promise.all(background);
      return result;
    },
  };
}

test('HTML and install precache agree on query-versioned code and styles', async () => {
  const w = worker(async (request) => response('asset', {
    mime: new URL(request.url).pathname.endsWith('.css') ? 'text/css' : 'text/javascript',
  }));
  await w.lifecycle('install');
  for (const file of ['index.html', 'login.html']) {
    const html = fs.readFileSync(path.join(STATIC, file), 'utf8');
    const assets = [...html.matchAll(/(?:src|href)="([^"\s]+\.(?:js|css)(?:\?[^"\s]*)?)"/g)];
    assert.equal(assets.length, 2);
    for (const [, asset] of assets) {
      assert.equal(new URL(asset, ORIGIN).searchParams.get('v'), RELEASE);
      assert.ok(await w.cached(asset), `${file} asset is precached with its exact release key`);
    }
  }
});

for (const [asset, mime] of [['app.js', 'text/javascript'], ['styles.css', 'text/css']]) {
  test(`${asset} serves revalidated network content even with a cached response`, async () => {
    const w = worker(async () => response('current release', { mime }));
    const assetUrl = `/static/${asset}?v=${RELEASE}`;
    await w.seed(assetUrl, response('stale cached content', { mime }));
    assert.equal((await w.request(assetUrl)).body, 'current release');
    assert.equal(w.requests[0].cache, 'no-cache');
    assert.equal((await w.cached(assetUrl)).body, 'current release');
  });
}

test('offline upgrade cannot match an unversioned or different-release script', async () => {
  const w = worker(async () => { throw new Error('offline'); });
  await w.seed('/static/app.js', response('old script using removed DOM ids'));
  await w.seed('/static/app.js?v=old', response('another release'));
  const assetUrl = `/static/app.js?v=${RELEASE}`;
  await assert.rejects(w.request(assetUrl), /offline/);
  await w.seed(assetUrl, response('exact release already downloaded'));
  assert.equal((await w.request(assetUrl)).body, 'exact release already downloaded');
});

test('auth redirects, login HTML, and failed responses never replace cached code', async () => {
  for (const options of [
    { redirected: true },
    { mime: 'text/html' },
    { mime: 'text/plain' },
    { status: 403 },
    { status: 500 },
    { status: 0, type: 'opaqueredirect' },
  ]) {
    const networkResponse = response('authentication or server response', options);
    const w = worker(async () => networkResponse);
    const assetUrl = `/static/app.js?v=${RELEASE}`;
    await w.seed(assetUrl, response('known release'));
    assert.equal((await w.request(assetUrl)).body, 'authentication or server response');
    assert.equal((await w.cached(assetUrl)).body, 'known release');
  }
});

test('fresh script still runs when updating optional offline cache fails', async () => {
  const w = worker(async () => response('fresh script'));
  const assetUrl = `/static/app.js?v=${RELEASE}`;
  await w.seed(assetUrl, response('stale script'));
  w.setQuotaFailure();
  assert.equal((await w.request(assetUrl)).body, 'fresh script');
});

test('healthy network serves scripts, styles and icons when CacheStorage cannot open', async () => {
  const w = worker(async (request) => response('fresh asset', {
    mime: request.url.includes('.css') ? 'text/css' : 'text/javascript',
  }));
  w.setStorageFailure();
  for (const asset of ['app.js', 'styles.css', 'icon.svg']) {
    assert.equal((await w.request(`/static/${asset}?v=${RELEASE}`)).body, 'fresh asset');
  }
  assert.equal(w.requests.length, 3, 'cache failure must not prevent network attempts');
});

test('install and activation proceed when optional CacheStorage is unavailable', async () => {
  const w = worker();
  w.setStorageFailure();
  await w.lifecycle('install');
  await w.lifecycle('activate');
  assert.deepEqual(w.lifecycleCalls, ['skipWaiting', 'claim']);
});

test('offline with inaccessible storage fails safely instead of returning another release', async () => {
  for (const fault of ['setStorageFailure', 'setReadFailure']) {
    const w = worker(async () => { throw new Error('network offline'); });
    const assetUrl = `/static/app.js?v=${RELEASE}`;
    await w.seed('/static/app.js', response('incompatible old release'));
    await w.seed(assetUrl, response('cached release cannot be read'));
    w[fault]();
    await assert.rejects(w.request(assetUrl), /network offline/);
    assert.equal(w.requests.length, 1);
    const offline = await w.request('/', { mode: 'navigate' });
    assert.equal(offline.status, 503);
    assert.match(await offline.text(), /You are offline/);
  }
});

test('API, mutations, and cross-origin requests bypass all worker caches', async () => {
  const w = worker();
  for (const [url, init] of [
    ['/api', {}], ['/api/status', {}], ['/api/performance?days=30', {}],
    ['/static/app.js', { method: 'POST' }], ['https://other.example/static/app.js', {}],
  ]) assert.equal(await w.request(url, init), undefined);
  assert.equal(w.requests.length, 0);
  assert.equal(w.stores.size, 0);
});

test('activation removes older shell caches while preserving unrelated caches', async () => {
  const w = worker();
  await w.seed('/static/app.js', response('old'), 'trader-shell-v-old');
  await w.seed('/other', response('unrelated'), 'another-app');
  await w.seed(`/static/app.js?v=${RELEASE}`, response('current'));
  await w.lifecycle('activate');
  assert.ok(!w.stores.has('trader-shell-v-old'));
  assert.ok(w.stores.has('another-app'));
  assert.ok(w.stores.has(`trader-shell-${VERSION}`));
});

test('offline navigation stays an offline page with the current stylesheet version', async () => {
  const w = worker(async () => { throw new Error('offline'); });
  const offline = await w.request('/', { mode: 'navigate' });
  assert.equal(offline.status, 503);
  const html = await offline.text();
  assert.match(html, /You are offline/);
  assert.ok(html.includes(`/static/styles.css?v=${RELEASE}`));
  assert.ok(!html.includes('/static/app.js'));
});

const pushData = (value) => ({
  json() { return typeof value === 'string' ? JSON.parse(value) : value; },
  text() { return typeof value === 'string' ? value : JSON.stringify(value); },
});

test('a push message is shown as a notification, even with no window open', async () => {
  const w = worker();
  await w.dispatch('push', {
    data: pushData({ title: '⚠️ Halted', body: 'Daily loss limit reached', priority: 'urgent', ts: '2026-10-26T14:00:00Z' }),
  });
  assert.equal(w.shown.length, 1);
  const { title, options } = w.shown[0];
  assert.equal(title, '⚠️ Halted');
  assert.equal(options.body, 'Daily loss limit reached');
  assert.equal(options.icon, '/static/icon-192.png');
  assert.equal(options.badge, '/static/badge-96.png');
  assert.equal(options.requireInteraction, true);
  assert.equal(options.timestamp, Date.parse('2026-10-26T14:00:00Z'));
});

test('an odd push message still shows something sensible', async () => {
  const w = worker();
  await w.dispatch('push', { data: pushData('plain text, not JSON') });
  await w.dispatch('push', { data: null });
  await w.dispatch('push', { data: pushData({ title: 42, body: { x: 1 } }) });
  assert.deepEqual(w.shown.map((n) => [n.title, n.options.body, n.options.requireInteraction]), [
    ['AI Trader', 'plain text, not JSON', false],
    ['AI Trader', '', false],
    ['AI Trader', '', false],
  ]);
});

test('tapping a notification focuses the open app, or opens it', async () => {
  const w = worker();
  let closed = 0;
  const notification = { close() { closed += 1; } };
  let focused = 0;
  w.windows.push({ url: 'https://elsewhere.example/', focus() { throw new Error('wrong origin'); } });
  w.windows.push({ url: `${ORIGIN}/#activity`, focus() { focused += 1; } });
  await w.dispatch('notificationclick', { notification });
  assert.equal(focused, 1);
  assert.deepEqual(w.opened, []);
  w.windows.length = 0;
  await w.dispatch('notificationclick', { notification });
  assert.deepEqual(w.opened, ['/']);
  assert.equal(closed, 2);
});
