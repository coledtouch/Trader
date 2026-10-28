/* AI Trader web app (SPEC 5.12, 5.13, 7.2).
 *
 * Vanilla JS, no build step, strict CSP: no inline scripts or styles, no innerHTML. Every
 * node is built with createElement/createElementNS and text goes in via textContent, so
 * server data can never become markup.
 */
(() => {
  'use strict';

  const POLL_MS = 5000;
  const READ_TIMEOUT_MS = 10000;
  const ACTION_TIMEOUT_MS = 30000; // kill/flatten and approvals talk to the broker first
  const APPROVAL_TTL_MS = 10 * 60 * 1000;
  const VIEWS = ['dashboard', 'activity', 'signals', 'settings'];
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const DASH = '—';

  const state = {
    csrf: null,
    data: null, // last /api/status payload
    updatedAt: null,
    settings: null, // last /api/settings payload
    view: null,
    pollTimer: null,
    pollBusy: false,
    pollAgain: false,
    expired: false,
    redirecting: false,
    settingsDirty: false,
    equity: null,
    chartWidth: 0,
    setups: new Set(),
    performanceBusy: false,
    performanceLoadedAt: 0,
    dashChartsBusy: false,
    dashChartsLoadedAt: 0,
    dashChartsWidth: 0,
    dashEquity: null,
    dashDays: null,
    dashPeriod: 30,
    researchRunning: false,
    researchTimer: null,
    researchRequest: 0,
    researchFrames: null,
    push: null, // last /api/push payload
  };

  // ------------------------------------------------------------------ DOM helpers
  const $ = (id) => document.getElementById(id);

  function applyProps(node, props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.setAttribute('class', value);
      else if (key === 'text') node.textContent = String(value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
  }

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    applyProps(node, props);
    for (const child of [].concat(children)) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  function svg(tag, props = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    applyProps(node, props);
    return node;
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function setTone(node, tone) {
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  const empty = (text) => el('p', { class: 'empty', text });

  /** True when `data` differs from what was last rendered into `key` (avoids redrawing
   * tables every poll, which would disturb screen readers and text selection). */
  const rendered = new Map();
  function changed(key, data) {
    const json = JSON.stringify(data);
    if (rendered.get(key) === json) return false;
    rendered.set(key, json);
    return true;
  }
  const errorLine = (text) => el('p', { class: 'inline-error', text });
  const asList = (value) => (Array.isArray(value) ? value : []);

  // ------------------------------------------------------------------ formatting
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
  const usdSigned = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    signDisplay: 'exceptZero',
  });
  const usdFine = new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
  const fmtTimeSec = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
  const fmtDateTime = new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
  const fmtDate = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
  // For calendar days stored as noon UTC (dayDate): formatting them in UTC keeps the date
  // right in every time zone, including UTC+12 and later.
  const fmtDayAxis = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
  const fmtDay = new Intl.DateTimeFormat(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });

  const money = (v) => (isNum(v) ? usd.format(v) : DASH);
  const signedMoney = (v) => (isNum(v) ? usdSigned.format(v) : DASH);
  const fineMoney = (v) => (isNum(v) ? usdFine.format(v) : DASH);
  const toneOf = (v) => (!isNum(v) || Math.abs(v) < 0.005 ? null : v > 0 ? 'pos' : 'neg');

  function price(v) {
    if (!isNum(v)) return DASH;
    const digits = Math.abs(v) >= 1 ? 2 : 4;
    return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  const qty = (v) => (isNum(v) ? v.toLocaleString('en-US', { maximumFractionDigits: 6 }) : DASH);
  const pct = (v, digits = 1) => (isNum(v) ? `${v.toFixed(digits)}%` : DASH);

  function signedPct(v) {
    if (!isNum(v)) return DASH;
    return `${v > 0 ? '+' : ''}${v.toFixed(1)}%`;
  }

  function rMult(v) {
    if (!isNum(v)) return DASH;
    if (Math.abs(v) < 0.005) return '0.00R';
    return `${v > 0 ? '+' : ''}${v.toFixed(2)}R`;
  }

  function parseTs(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function when(value) {
    const date = value instanceof Date ? value : parseTs(value);
    if (!date) return DASH;
    return date.toDateString() === new Date().toDateString()
      ? fmtTime.format(date)
      : fmtDateTime.format(date);
  }

  function dayLabel(day) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || '');
    if (!match) return day || DASH;
    return fmtDay.format(new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  }

  function sentence(text) {
    const s = String(text || '').trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  const humanize = (key) => sentence(String(key).replace(/[_.]+/g, ' '));

  const SETUP_LABELS = {
    orb_breakout: 'ORB breakout',
    vwap_reclaim: 'VWAP reclaim',
    trend_pullback: 'Trend pullback',
    rsi_reversion: 'RSI reversion',
    volume_spike: 'Volume spike',
  };
  const setupLabel = (name) => (name ? SETUP_LABELS[name] || humanize(name) : DASH);

  function pnlSpan(v, text) {
    return el('span', { class: toneOf(v), text: text === undefined ? signedMoney(v) : text });
  }

  function symbolCell(instrument, sub) {
    const inst = instrument || {};
    const crypto = inst.asset_class === 'CRYPTO';
    return [
      el('span', { class: 'cell-title', text: `${inst.symbol || '?'}${crypto ? ' (crypto)' : ''}` }),
      sub ? el('span', { class: 'cell-sub', text: sub }) : null,
    ];
  }

  const ORDER_STATUS = {
    pending_approval: ['Awaiting approval', 'warn'],
    submitted: ['Working', 'info'],
    partially_filled: ['Partly filled', 'info'],
    filled: ['Filled', 'ok'],
    cancelled: ['Cancelled', null],
    rejected: ['Rejected', 'bad'],
    blocked: ['Blocked', 'bad'],
  };

  function chipNode(label, tone) {
    const node = el('span', { class: 'status', text: label });
    setTone(node, tone);
    return node;
  }

  function statusChip(status) {
    const [label, tone] = ORDER_STATUS[status] || [humanize(status || 'unknown'), null];
    return chipNode(label, tone);
  }

  function orderType(req) {
    if (req.order_type === 'LMT') return isNum(req.limit_price) ? `Limit ${price(req.limit_price)}` : 'Limit';
    return 'Market';
  }

  /** A table that turns into label/value cards on phones (see .table in styles.css). */
  function table(columns, rows) {
    const head = el('tr', { role: 'row' }, columns.map((c) =>
      el('th', { scope: 'col', role: 'columnheader', class: c.num ? 'num' : null, text: c.label })));
    const body = rows.map((row) => el('tr', { role: 'row' }, columns.map((c) => {
      const value = c.value(row);
      const cls = [c.num ? 'num' : '', c.head ? 'cell-head' : '', c.note ? 'cell-note' : '']
        .filter(Boolean).join(' ');
      // On phones a cell is a flex row (label | value): a value made of several nodes stays
      // in one box, or its parts spread across the row and a chip stretches to the note.
      const content = Array.isArray(value) ? el('div', { class: 'cell-value' }, value) : value;
      return el('td', { role: 'cell', 'data-label': c.label, class: cls || null },
        content === null || content === undefined || content === '' ? [] : content);
    })));
    return el('div', { class: 'table-wrap' }, el('table', { class: 'table', role: 'table' }, [
      el('thead', { role: 'rowgroup' }, head),
      el('tbody', { role: 'rowgroup' }, body),
    ]));
  }

  // ------------------------------------------------------------------ API
  const FRIENDLY = {
    step_up_required: 'A valid 2FA code is required.',
    'invalid CSRF token': 'The security token expired. Reload the page.',
    'origin not allowed': 'Request blocked: it did not come from this site.',
    'too many failed attempts': 'Too many failed attempts. Wait a few minutes and try again.',
    'position_close failed': 'The broker did not take the sell.',
  };

  function locPath(loc) {
    if (!Array.isArray(loc)) return '';
    return loc.filter((p) => typeof p === 'string' && p !== 'body' && p !== 'settings').join('.');
  }

  function formatDetail(detail) {
    if (typeof detail === 'string') return FRIENDLY[detail] || sentence(detail);
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const path = locPath(d && d.loc);
          const msg = (d && d.msg) || 'invalid value';
          return path ? `${path}: ${msg}` : msg;
        })
        .join('; ');
    }
    return '';
  }

  class ApiError extends Error {
    constructor(status, detail, fatal = false) {
      super(formatDetail(detail) || (status ? `Request failed (HTTP ${status}).` : 'Cannot reach the server.'));
      this.status = status;
      this.detail = detail;
      this.fatal = fatal; // the page is redirecting or the session is gone: nothing to show
    }
  }

  const isStepUp = (err) => err instanceof ApiError && err.status === 403 && err.detail === 'step_up_required';

  function toLogin() {
    if (!state.redirecting) {
      state.redirecting = true;
      state.settingsDirty = false; // the session is gone; do not block the redirect
      window.location.assign('/login');
    }
    return new ApiError(401, 'Signed out.', true);
  }

  /** A proxy in front of the app (Cloudflare Access) refused or redirected the request. */
  function sessionExpired() {
    if (!state.expired) {
      state.expired = true;
      clearTimeout(state.pollTimer);
      $('session-banner').hidden = false;
      $('stale-banner').hidden = true;
    }
    return new ApiError(403, 'Session expired - reload to sign in again', true);
  }

  async function send(method, path, body, background = false) {
    const headers = { Accept: 'application/json' };
    // Automatic refreshes are not owner activity: the server then does not reset the idle
    // timer, so an unattended open tab still signs out after the idle timeout.
    if (background && method === 'GET') headers['X-Background'] = '1';
    const init = {
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'manual', // the API never redirects; a redirect means a proxy wants a login
    };
    if (method !== 'GET') {
      headers['X-CSRF-Token'] = state.csrf || '';
      if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), method === 'GET' ? READ_TIMEOUT_MS : ACTION_TIMEOUT_MS);
    init.signal = controller.signal;
    try {
      return await fetch(path, init);
    } catch (err) {
      const timedOut = err && err.name === 'AbortError';
      throw new ApiError(0, timedOut ? 'The server did not answer in time.' : 'Cannot reach the server.');
    } finally {
      clearTimeout(timer);
    }
  }

  async function readJson(res) {
    if (!(res.headers.get('content-type') || '').includes('application/json')) return { json: false };
    try {
      return { json: true, value: await res.json() };
    } catch {
      return { json: false };
    }
  }

  async function api(method, path, body, { background = false } = {}) {
    if (state.expired) throw new ApiError(403, 'Session expired - reload to sign in again', true);
    if (state.redirecting) throw new ApiError(401, 'Signed out.', true);
    if (method !== 'GET' && !state.csrf) await loadSession();
    for (let attempt = 0; ; attempt += 1) {
      const res = await send(method, path, body, background);
      if (res.type === 'opaqueredirect') throw sessionExpired();
      if (res.status === 401) throw toLogin();
      const { json, value } = await readJson(res);
      if (res.ok) {
        if (!json) throw new ApiError(res.status, 'Unexpected response from the server.');
        return value;
      }
      const detail = json && value && typeof value === 'object' ? value.detail : undefined;
      // Our API always answers JSON; an HTML 403 (or the Access middleware's "forbidden")
      // means the Cloudflare Access session ran out.
      if (res.status === 403 && (!json || detail === 'forbidden')) throw sessionExpired();
      if (res.status === 403 && detail === 'invalid CSRF token' && method !== 'GET' && attempt === 0) {
        await loadSession(); // the server refused before acting, so one retry is safe
        continue;
      }
      throw new ApiError(res.status, detail);
    }
  }

  async function loadSession() {
    const data = await api('GET', '/api/session');
    if (!data || !data.authenticated || typeof data.csrf !== 'string') throw toLogin();
    state.csrf = data.csrf;
  }

  // ------------------------------------------------------------------ feedback
  let toastTimer = null;

  function toast(message, tone = 'info') {
    const node = $('toast');
    clearTimeout(toastTimer);
    node.textContent = '';
    setTone(node, tone === 'error' ? 'error' : null);
    // Set on the next frame so a repeated message is announced again.
    requestAnimationFrame(() => {
      node.textContent = message;
    });
    toastTimer = setTimeout(() => {
      node.textContent = '';
    }, tone === 'error' ? 9000 : 5000);
  }

  function report(err, prefix = '') {
    if (err && err.fatal) return;
    toast(`${prefix}${err && err.message ? err.message : 'Something went wrong.'}`, 'error');
  }

  const isBusy = (button) => button.getAttribute('aria-busy') === 'true';

  async function busy(button, fn) {
    if (isBusy(button)) return undefined;
    button.setAttribute('aria-busy', 'true');
    button.disabled = true;
    try {
      return await fn();
    } finally {
      button.removeAttribute('aria-busy');
      button.disabled = false;
      syncButtons();
    }
  }

  function setStale(message) {
    const banner = $('stale-banner');
    const stale = Boolean(message) && !state.expired;
    $('main').classList.toggle('is-stale', stale);
    banner.hidden = !stale;
    if (!stale) return;
    const last = state.updatedAt ? ` Last update ${fmtTimeSec.format(state.updatedAt)}.` : '';
    setText($('stale-text'), `Live data paused: ${message}${last} Retrying every 5 seconds.`);
  }

  // ------------------------------------------------------------------ 2FA + kill dialogs
  /**
   * Ask for a TOTP code and run `submit(code)` while the dialog stays open. A rejected code
   * keeps the dialog open with an error; any other failure closes it and rejects.
   * Resolves to submit's result, or null when the owner cancels.
   */
  function stepUp({ title, message, confirm, danger = false }, submit) {
    const dialog = $('totp-dialog');
    const form = $('totp-form');
    const input = $('totp-input');
    const error = $('totp-error');
    const ok = $('totp-submit');
    const cancel = $('totp-cancel');
    if (dialog.open) return Promise.resolve(null);
    setText($('totp-title'), title);
    setText($('totp-message'), message);
    ok.textContent = confirm;
    ok.classList.toggle('btn-danger', danger);
    ok.classList.toggle('btn-primary', !danger);
    input.value = '';
    input.removeAttribute('aria-invalid');
    error.textContent = '';

    return new Promise((resolve, reject) => {
      const listeners = new AbortController();
      const opts = { signal: listeners.signal };
      let working = false;
      let settled = false;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        listeners.abort();
        if (dialog.open) dialog.close();
        fn(value);
      };
      const showError = (text) => {
        error.textContent = text;
        input.setAttribute('aria-invalid', 'true');
        input.focus();
      };

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (working) return;
        const code = input.value.replace(/\D/g, '');
        if (code.length !== 6) {
          showError('Enter the 6-digit code from your authenticator app.');
          return;
        }
        working = true;
        ok.setAttribute('aria-busy', 'true');
        ok.disabled = true;
        cancel.disabled = true;
        error.textContent = '';
        try {
          finish(resolve, await submit(code));
        } catch (err) {
          if (isStepUp(err)) {
            input.value = '';
            showError('That code was not accepted. Each code works once: wait for the next one.');
          } else if (err instanceof ApiError && err.status === 429) {
            showError(FRIENDLY['too many failed attempts']);
          } else {
            finish(reject, err);
          }
        } finally {
          working = false;
          ok.removeAttribute('aria-busy');
          ok.disabled = false;
          cancel.disabled = false;
        }
      }, opts);
      input.addEventListener('input', () => {
        const digits = input.value.replace(/\D/g, '').slice(0, 6);
        if (digits !== input.value) input.value = digits;
      }, opts);
      cancel.addEventListener('click', () => {
        if (!working) finish(resolve, null);
      }, opts);
      dialog.addEventListener('cancel', (event) => {
        event.preventDefault();
        if (!working) finish(resolve, null);
      }, opts);
      dialog.addEventListener('close', () => finish(resolve, null), opts);

      dialog.showModal();
      input.focus();
    });
  }

  function openKill() {
    const dialog = $('kill-dialog');
    if (dialog.open) return;
    $('kill-error').textContent = '';
    dialog.showModal();
    $('kill-cancel').focus();
  }

  async function confirmKill(event) {
    event.preventDefault();
    const button = $('kill-confirm');
    await busy(button, async () => {
      $('kill-error').textContent = '';
      try {
        const status = await api('POST', '/api/bot/kill');
        mergeStatus(status);
        const failure = status && typeof status.last_error === 'string' && status.last_error.includes('kill switch failed');
        if (failure) {
          $('kill-error').textContent = `${sentence(status.last_error)}. Check your IBKR account now.`;
          return;
        }
        $('kill-dialog').close();
        toast('Kill switch done: trading halted, orders cancelled, positions closing at market.');
      } catch (err) {
        // Shown even for a lost session: the owner must know the kill was not sent.
        let message = `Kill switch failed: ${err.message} Try again, or act in IBKR directly.`;
        if (err.fatal) message = `Not sent: ${err.message}.`;
        else if (err.status === 0) {
          message = `${err.message} The kill switch may still be running: check the positions and press again if any remain.`;
        }
        $('kill-error').textContent = message;
      } finally {
        refreshStatus();
      }
    });
  }

  // ------------------------------------------------------------------ status polling
  async function refreshStatus() {
    if (state.pollBusy) {
      state.pollAgain = true;
      return;
    }
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (document.hidden || state.expired || state.redirecting) return;
    state.pollBusy = true;
    try {
      const data = await api('GET', '/api/status', undefined, { background: true });
      if (!data || typeof data !== 'object' || !data.status) {
        throw new ApiError(200, 'Unexpected status response.');
      }
      state.data = data;
      state.updatedAt = new Date();
      setStale(null);
      renderStatus();
      if (state.view === 'dashboard' && Date.now() - state.performanceLoadedAt > 30000) loadPerformance({ background: true });
      if (state.view === 'dashboard' && Date.now() - state.dashChartsLoadedAt > 60000) loadDashboardCharts({ background: true });
    } catch (err) {
      if (!err.fatal) setStale(err.message);
    } finally {
      state.pollBusy = false;
      if (state.pollAgain) {
        state.pollAgain = false;
        refreshStatus();
      } else if (!document.hidden && !state.expired && !state.redirecting) {
        state.pollTimer = setTimeout(refreshStatus, POLL_MS);
      }
    }
  }

  function mergeStatus(status) {
    if (!status || typeof status !== 'object' || !('mode' in status)) return;
    if (state.data) {
      state.data.status = status;
      renderStatus();
    }
  }

  // ------------------------------------------------------------------ dashboard
  function modeInfo(s) {
    if (s.mode === 'live') {
      return s.shadow
        ? { key: 'live-shadow', label: 'LIVE-SHADOW', desc: 'live account in shadow mode: new entries are blocked; the bot still closes positions it opened' }
        : { key: 'live-armed', label: 'LIVE-ARMED', desc: 'live account, armed: real orders are sent' };
    }
    if (s.mode === 'paper') return { key: 'paper', label: 'PAPER', desc: 'IBKR paper account' };
    if (s.mode === 'sim') return { key: 'sim', label: 'SIM', desc: 'offline simulator with fake money' };
    return { key: 'unknown', label: String(s.mode || '…').toUpperCase(), desc: 'unknown mode' };
  }

  function limits() {
    const payload = state.settings;
    if (!payload || !payload.settings) return null;
    const risk = payload.settings.risk || {};
    const caps = payload.hard_caps || {};
    const capped = (name) => {
      const value = risk[name];
      if (!isNum(value)) return null;
      return isNum(caps[name]) ? Math.min(value, caps[name]) : value;
    };
    return {
      lossPct: capped('max_daily_loss_pct'),
      maxTrades: capped('max_trades_per_day'),
      maxOpen: capped('max_open_positions'),
      budget: capped('llm_daily_budget_usd'),
      llmOn: payload.settings.llm_enabled !== false && payload.llm_available !== false,
    };
  }

  function renderStatus() {
    const data = state.data;
    if (!data) return;
    const s = data.status;
    renderMode(s);
    renderHalt(s);
    renderMoney(data);
    renderHealth(s);
    renderQuoteHealth(asList(s.quote_health || data.quote_health));
    renderTiles(data);
    renderPending(asList(data.pending), s);
    renderPositions(asList(data.positions));
    renderOpenOrders(asList(data.open_orders));
    renderLiveCard();
    syncButtons();
  }

  function renderMode(s) {
    const info = modeInfo(s);
    const badge = $('mode-badge');
    setText($('mode-label'), info.label);
    badge.dataset.mode = info.key;
    badge.setAttribute('title', sentence(info.desc));
    document.body.dataset.mode = info.key;
  }

  function renderHalt(s) {
    const halted = Boolean(s.halted);
    const reason = s.halt_reason || 'no reason given';
    const soft = halted && /target/i.test(reason);
    const banner = $('halt-banner');
    setTone(banner, soft ? 'calm' : null);
    setText(banner.querySelector('strong'), soft ? 'New entries paused:' : 'Trading halted:');
    setText($('halt-reason'), halted ? sentence(reason) : '');
    banner.hidden = !halted;
    $('resume-btn').hidden = !halted;
  }

  function renderMoney(data) {
    const s = data.status;
    const account = data.account;
    const equity = isNum(s.equity) ? s.equity : account && isNum(account.equity) ? account.equity : null;
    setText($('equity-value'), money(equity));
    const sub = [];
    if (isNum(s.start_equity)) sub.push(`Start of day ${money(s.start_equity)}`);
    if (account && isNum(account.buying_power)) sub.push(`Buying power ${money(account.buying_power)}`);
    setText($('equity-sub'), sub.join(' · '));

    const pnl = isNum(s.pnl_today) ? s.pnl_today : null;
    const base = isNum(s.start_equity) && s.start_equity > 0 ? s.start_equity : null;
    const pnlText = pnl === null ? DASH : `${signedMoney(pnl)}${base ? ` (${signedPct((pnl / base) * 100)})` : ''}`;
    const pnlNode = $('pnl-value');
    setText(pnlNode, pnlText);
    pnlNode.classList.toggle('pos', toneOf(pnl) === 'pos');
    pnlNode.classList.toggle('neg', toneOf(pnl) === 'neg');

    const lim = limits();
    // The server's figure follows a resume (halts are then measured from the resume equity).
    const loss = isNum(s.loss_limit_usd) && s.loss_limit_usd > 0
      ? s.loss_limit_usd
      : lim && isNum(lim.lossPct) && base ? (base * lim.lossPct) / 100 : null;
    renderMeter(pnl, loss);
    setText($('pnl-loss-label'), loss ? `Loss limit ${signedMoney(-loss)}` : 'Loss limit unavailable');

    let note = '';
    if (pnl !== null && pnl < 0 && loss) {
      note = `${Math.min(100, Math.round((-pnl / loss) * 100))}% of today's loss limit used.`;
    } else if (pnl !== null && loss) {
      note = 'No daily loss budget used.';
    }
    setText($('pnl-note'), note);
  }

  function renderMeter(pnl, loss) {
    const box = $('pnl-meter');
    box.replaceChildren();
    box.hidden = !isNum(loss) || loss <= 0 || !isNum(pnl);
    if (box.hidden) return;
    const used = Math.min(100, Math.max(0, (-pnl / loss) * 100));
    const root = svg('svg', { viewBox: '0 0 100 12', preserveAspectRatio: 'none', focusable: 'false' });
    root.append(svg('rect', { class: 'meter-loss', x: 0, y: 0, width: 100, height: 12 }));
    if (used > 0) root.append(svg('rect', { class: 'meter-fill-neg', x: 0, y: 0, width: used, height: 12 }));
    box.append(root);
  }

  async function loadPerformance({ background = false } = {}) {
    if (state.performanceBusy) return;
    state.performanceBusy = true;
    const days = $('performance-days').value || '30';
    const box = $('performance-summary');
    box.setAttribute('aria-busy', 'true');
    try {
      const data = await api('GET', `/api/performance?days=${encodeURIComponent(days)}`, undefined, { background });
      if ($('performance-days').value !== days) return;
      renderPerformance(data, box);
      state.performanceLoadedAt = Date.now();
    } catch (err) {
      if (!err.fatal) box.replaceChildren(errorLine(`Performance unavailable: ${err.message}`));
    } finally {
      state.performanceBusy = false;
      box.removeAttribute('aria-busy');
      if ($('performance-days').value !== days) loadPerformance();
    }
  }

  function metric(label, value, sub) {
    return el('div', { class: 'tile' }, [
      el('dt', { text: label }),
      el('dd', {}, [value, sub ? el('span', { class: 'tile-sub', text: sub }) : null]),
    ]);
  }

  function renderPerformance(p, box) {
    const costsComplete = p.costs_configured === true;
    const complete = costsComplete && isNum(p.all_in_pnl);
    const headline = el('p', {
      class: `hero-value ${complete ? toneOf(p.all_in_pnl) || '' : 'performance-incomplete'}`,
      text: complete ? signedMoney(p.all_in_pnl) : costsComplete ? 'Performance unavailable' : 'Costs incomplete',
    });
    const warnings = asList(p.warnings).map((text) => el('li', { text }));
    box.replaceChildren(
      headline,
      el('p', { class: 'small muted', text: `${humanize(p.mode || 'unknown')} account · last ${p.days || $('performance-days').value} days · ${isNum(p.closed_trades) ? p.closed_trades : 0} closed trades` }),
      !costsComplete && isNum(p.known_cost_pnl) ? el('p', { class: 'mt-8', text: `After known costs: ${signedMoney(p.known_cost_pnl)}. This subtotal excludes missing expenses.` }) : '',
      el('dl', { class: 'tiles mt-12' }, [
        metric('Trading P&L', pnlSpan(p.trading_pnl), 'After trading commissions'),
        metric('AI spending', fineMoney(p.ai_cost), 'Recorded token-price estimates'),
        metric('Hosting estimate', money(p.hosting_cost), 'Prorated monthly input'),
        metric('Data estimate', money(p.data_cost), 'Prorated monthly input'),
        metric('Net expectancy', signedMoney(p.net_expectancy), 'Per closed trade, after commissions'),
        metric('Max drawdown', pct(p.max_drawdown_pct), 'Daily equity; before operating costs'),
        metric('Profit factor', isNum(p.profit_factor) ? p.profit_factor.toFixed(2) : DASH, 'Closed trades after commissions'),
        metric('Signal evidence', `${p.evaluated_signals ?? 0} / ${p.signal_count ?? 0}`, 'Evaluated / recorded · before costs'),
      ]),
      !costsComplete ? el('p', { class: 'inline-warn', text: 'Enter monthly hosting and market-data costs in Settings to complete this report. Enter 0 explicitly for a free service.' })
        : !complete ? el('p', { class: 'inline-warn', text: 'Complete account equity observations are needed before profit after costs can be calculated.' }) : '',
      el('details', { class: 'more mt-12' }, [
        el('summary', { text: 'Reporting notes and limitations' }),
        el('p', { class: 'small muted mt-8', text: 'Trading P&L includes realized and unrealized changes recorded in the account journal, adjusted for recorded cash flows. Monthly costs are estimates for the recorded coverage period.' }),
        warnings.length ? el('ul', { class: 'performance-warnings mt-8' }, warnings) : null,
      ]),
      el('p', { class: 'small muted mt-8', text: `Coverage begins ${when(p.covered_from)} · as of ${when(p.as_of)}. Paper and simulated results are not proof of live profitability.` }),
    );
  }

  function renderQuoteHealth(quotes) {
    if (!changed('quote-health', quotes)) return;
    const box = $('quote-health');
    if (!quotes.length) {
      box.replaceChildren(empty('No quotes observed yet. Data freshness is not verified.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (q) => q.symbol || DASH },
      { label: 'Age', num: true, value: (q) => isNum(q.age_seconds) ? `${Math.max(0, Math.round(q.age_seconds))} sec` : 'Unknown' },
      { label: 'Feed', value: (q) => {
        if (state.data.status.broker === 'sim') return chipNode('Synthetic', null);
        if (q.has_price === false) return chipNode('No data', 'warn');
        return q.delayed ? chipNode('Delayed', 'warn') : chipNode('Real-time', null);
      } },
      { label: 'Entry check', value: (q) => {
        if (q.valid && !q.delayed) return chipNode('Usable', 'ok');
        if (q.valid && !q.delay_blocks_entry) {
          return chipNode(state.data.status.delayed_paper_entries ? 'Delayed: paper entries allowed' : 'Delayed: entries allowed', 'warn');
        }
        const why = q.valid ? 'delayed data' : q.problem;
        return why ? [chipNode('Blocked', 'warn'), el('span', { class: 'cell-sub', text: why })] : chipNode('Blocked', 'warn');
      } },
    ], quotes));
  }

  const MAKER_LABELS = { claude: 'Claude', grok: 'Grok', consensus: 'Consensus', rules: 'Rules' };
  const makerLabel = (name) => MAKER_LABELS[name] || humanize(name || 'unknown');
  const AI_CHIP = {
    consensus: 'AI: Claude + Grok',
    claude: 'AI: Claude only',
    grok: 'AI: Grok only',
    rules: 'Rules only',
  };

  function setChip(id, label, tone) {
    const chip = $(id);
    setText(chip, label);
    setTone(chip, tone);
  }

  function renderHealth(s) {
    const broker = s.broker === 'ibkr' ? 'IBKR' : s.broker === 'sim' ? 'Simulator' : s.broker || 'Broker';
    setChip('chip-bot', s.running ? 'Bot running' : 'Bot stopped', s.running ? 'ok' : null);
    setChip('chip-broker', `${broker} ${s.connected ? 'connected' : 'disconnected'}`, s.connected ? 'ok' : 'bad');
    setChip('chip-market', s.stock_market_open ? 'Market open' : 'Market closed', s.stock_market_open ? 'ok' : null);
    const quotes = asList(s.quote_health || state.data.quote_health);
    const delayed = Boolean(s.data_delayed || quotes.some((q) => q.delayed));
    // A halt or a wide spread blocks entries in that symbol, but the feed itself is fine.
    const blockedQuotes = quotes.some((q) => !q.valid && !q.market_block);
    setChip('chip-data', s.broker === 'sim' ? 'Synthetic data' : delayed ? 'Delayed data' : blockedQuotes ? 'Quotes need review' : quotes.length ? 'Real-time feed' : 'Data not verified',
      delayed || blockedQuotes ? 'warn' : s.broker === 'sim' || !quotes.length ? null : 'ok');
    $('market-data-banner').hidden = !delayed;
    setText($('market-data-text'), !delayed ? ''
      : s.delayed_paper_entries
        ? 'Delayed market data: paper entries are allowed by your setting. They test the order plumbing and are left out of the edge statistics. Live trading needs real-time data.'
        : 'Delayed market data: stock entries and paid stock entry analysis are paused. Existing exits remain managed.');
    setChip('chip-autopilot', s.autopilot ? 'Autopilot on' : 'Approval required', s.autopilot ? 'ok' : 'warn');
    setChip('chip-ai', AI_CHIP[s.decision_maker] || `AI: ${makerLabel(s.decision_maker)}`,
      s.decision_warning ? 'warn' : s.decision_maker === 'consensus' ? 'ok' : null);
    const decisionWarning = $('decision-warning');
    decisionWarning.hidden = !s.decision_warning;
    setText(decisionWarning, s.decision_warning || '');
    const dataWarning = $('data-warning');
    dataWarning.hidden = !s.data_warning;
    setText(dataWarning, s.data_warning || '');
    setText($('last-cycle'), when(s.last_cycle_at));
    setText($('next-cycle'), s.running ? when(s.next_cycle_at) : 'Bot stopped');
    setText($('updated-at'), state.updatedAt ? fmtTimeSec.format(state.updatedAt) : DASH);
    const error = $('last-error');
    error.hidden = !s.last_error;
    setText(error, s.last_error ? `Last error: ${s.last_error}` : '');
  }

  function tileValue(id, value, sub) {
    $(id).replaceChildren(value, sub ? el('span', { class: 'tile-sub', text: sub }) : '');
  }

  function renderTiles(data) {
    const s = data.status;
    const account = data.account;
    const lim = limits();
    const positions = asList(data.positions);
    const of = (value, max) => (isNum(max) ? `${value} / ${max}` : String(value));
    tileValue('trades-value', of(s.trades_today ?? 0, lim && lim.maxTrades), 'entries today');
    tileValue('llm-value', isNum(lim && lim.budget) ? `${money(s.llm_cost_today)} / ${money(lim.budget)}` : money(s.llm_cost_today),
      lim && !lim.llmOn ? 'rules-only decisions' : 'spent today');
    tileValue('positions-value', of(positions.length, lim && lim.maxOpen), null);
    const dayTrades = account ? account.day_trades_remaining : null;
    tileValue('daytrades-value', isNum(dayTrades) ? String(dayTrades) : DASH,
      !isNum(dayTrades) ? 'unknown or not limited' : dayTrades < 1 ? 'stock entries blocked' : 'PDT rule');
    tileValue('cash-value', money(account && account.cash), null);
    tileValue('settled-value', money(account && account.settled_cash), null);
  }

  function renderPending(pending, s) {
    const section = $('pending-section');
    section.hidden = pending.length === 0 && s.autopilot !== false;
    setText($('pending-count'), pending.length ? String(pending.length) : '');
    const list = $('pending-list');
    // Rebuild only when the set of orders changes, so buttons keep focus across polls.
    const key = pending.map((o) => `${o.id}:${o.status}`).join(',');
    if (list.dataset.key === key && list.childElementCount) return;
    list.dataset.key = key;
    if (!pending.length) {
      list.replaceChildren(empty('Nothing is waiting. With autopilot off, new orders wait here for you.'));
      return;
    }
    list.replaceChildren(...pending.map(pendingCard));
  }

  function pendingCard(order) {
    const req = order.request || {};
    const inst = req.instrument || {};
    const ref = isNum(req.limit_price) ? req.limit_price : null;
    const title = `${req.side || ''} ${qty(req.qty)} ${inst.symbol || '?'} ${ref !== null ? `@ ${price(ref)}` : 'at market'}`;
    const created = parseTs(order.created_at);
    const rows = [
      ['Order', orderType(req)],
      ['Stop', price(req.stop_loss)],
      ['Target', price(req.take_profit)],
      ['Size', ref !== null && isNum(req.qty) ? money(ref * req.qty) : DASH],
      ['Loss at stop', ref !== null && isNum(req.stop_loss) && isNum(req.qty) ? money((ref - req.stop_loss) * req.qty) : DASH],
      ['Setup', setupLabel(req.setup)],
      ['Expires', created ? when(new Date(created.getTime() + APPROVAL_TTL_MS)) : DASH],
    ];
    const approve = el('button', { class: 'btn btn-primary', type: 'button', text: 'Approve' });
    const reject = el('button', { class: 'btn', type: 'button', text: 'Reject' });
    approve.addEventListener('click', () => decide(order, 'approve', approve));
    reject.addEventListener('click', () => decide(order, 'reject', reject));
    return el('article', { class: 'pending', 'aria-label': `Pending order: ${title}` }, [
      el('p', { class: 'pending-title', text: title }),
      el('dl', { class: 'kv' }, rows.map(([k, v]) => el('div', {}, [el('dt', { text: k }), el('dd', { text: v })]))),
      req.reason ? el('p', { class: 'entry-text', text: req.reason }) : null,
      el('div', { class: 'btn-row' }, [approve, reject]),
    ]);
  }

  async function decide(order, action, button) {
    if (!Number.isInteger(order.id)) return;
    await busy(button, async () => {
      try {
        const rec = await api('POST', `/api/orders/${order.id}/${action}`);
        const status = rec && rec.status;
        const sent = ['submitted', 'partially_filled', 'filled'].includes(status);
        if (action === 'reject') toast('Order rejected.');
        else if (sent) toast(`Order sent: ${ORDER_STATUS[status][0].toLowerCase()}.`);
        else toast(`Not sent: ${(rec && rec.message) || humanize(status || 'unknown status')}`, 'error');
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) toast('That order is no longer waiting for approval.', 'error');
        else report(err);
      } finally {
        refreshStatus();
      }
    });
  }

  function renderPositions(positions) {
    const box = $('positions-list');
    if (!changed('positions', positions)) return;
    // Prices change every poll and the table is rebuilt: keep focus on the same Close button.
    const focused = box.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    if (!positions.length) {
      box.replaceChildren(empty('No open positions.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (p) => symbolCell(p.instrument, setupLabel(p.setup)) },
      { label: 'Qty', num: true, value: (p) => qty(p.qty) },
      { label: 'Avg cost', num: true, value: (p) => price(p.avg_cost) },
      { label: 'Last (bid)', num: true, value: (p) => price(p.market_price) },
      { label: 'Unrealized P&L', num: true, value: (p) => pnlSpan(p.unrealized_pnl) },
      { label: 'Stop', num: true, value: (p) => price(p.stop_loss) },
      { label: 'Target', num: true, value: (p) => price(p.take_profit) },
      { label: 'Opened', value: (p) => when(p.opened_at) },
      { label: 'Action', value: (p) => closeButton(p) },
    ], positions));
    if (focused) focusCloseButton(focused);
  }

  function focusCloseButton(key) {
    const button = [...$('positions-list').querySelectorAll('button[data-key]')].find((b) => b.dataset.key === key);
    if (button) button.focus({ preventScroll: true }); // the owner may have scrolled away
  }

  function closeButton(position) {
    const inst = position.instrument || {};
    const button = el('button', {
      class: 'btn btn-danger btn-sm', type: 'button', text: 'Close',
      'aria-label': `Close the ${inst.symbol || ''} position`,
      'data-key': `${inst.asset_class}:${inst.symbol}`,
    });
    button.addEventListener('click', () => openClose(position));
    return button;
  }

  function openClose(position) {
    const dialog = $('close-dialog');
    if (dialog.open) return;
    const inst = position.instrument || {};
    state.closing = { key: `${inst.asset_class}:${inst.symbol}`, symbol: inst.symbol };
    setText($('close-title'), `Close ${inst.symbol}?`);
    setText($('close-what'), `Sell all ${qty(position.qty)} ${inst.symbol} at market now?`);
    // Positions the bot opened carry its entry time; the owner's own do not.
    $('close-owner').hidden = Boolean(position.opened_at);
    setText($('close-confirm'), `Sell ${inst.symbol}`);
    $('close-error').textContent = '';
    dialog.showModal();
    $('close-cancel').focus();
  }

  async function confirmClose(event) {
    event.preventDefault();
    const target = state.closing;
    if (!target) return;
    const button = $('close-confirm');
    await busy(button, async () => {
      $('close-error').textContent = '';
      try {
        const result = await api('POST', '/api/positions/close', { instrument: target.key });
        if (result && result.status) mergeStatus(result.status);
        if (state.closing === target) $('close-dialog').close();
        toast(`Selling ${target.symbol} at market. The bot will not buy it again today.`);
      } catch (err) {
        const reason = String(err.message || '').replace(/\.$/, '');
        // 409: refused before anything was sent (market closed, already closing, no position).
        let message = err.status === 409 ? `Not closed: ${reason}.` : `Not closed: ${reason}. Check your IBKR account.`;
        if (err.fatal) message = `Not sent: ${reason}.`;
        else if (err.status === 0) {
          message = `${err.message} The sell may still go through: check the positions before trying again.`;
        }
        if ($('close-dialog').open && state.closing === target) $('close-error').textContent = message;
        else toast(`${target.symbol}: ${message}`, 'error');
      } finally {
        refreshStatus();
      }
    });
  }

  function renderOpenOrders(orders) {
    const box = $('open-orders-list');
    if (!changed('open-orders', orders)) return;
    if (!orders.length) {
      box.replaceChildren(empty('No working orders.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (o) => symbolCell(o.request && o.request.instrument, o.request && o.request.reason) },
      { label: 'Side', value: (o) => (o.request && o.request.side) || DASH },
      { label: 'Qty', num: true, value: (o) => qty(o.request && o.request.qty) },
      { label: 'Type', value: (o) => orderType(o.request || {}) },
      { label: 'Status', value: (o) => statusChip(o.status) },
      { label: 'Filled', num: true, value: (o) => (o.filled_qty ? `${qty(o.filled_qty)} @ ${price(o.avg_fill_price)}` : DASH) },
      { label: 'Placed', value: (o) => when(o.created_at) },
    ], orders));
  }

  // ------------------------------------------------------------------ bot controls
  async function toggleBot() {
    const s = state.data && state.data.status;
    if (!s) return;
    const starting = !s.running;
    await busy($('startstop-btn'), async () => {
      try {
        mergeStatus(await api('POST', starting ? '/api/bot/start' : '/api/bot/stop'));
        toast(starting ? 'Bot started.' : 'Bot stopped. Open positions keep their broker-side stops.');
      } catch (err) {
        report(err, starting ? 'Could not start: ' : 'Could not stop: ');
      } finally {
        refreshStatus();
      }
    });
  }

  async function runCycle() {
    await busy($('cycle-btn'), async () => {
      try {
        await api('POST', '/api/bot/cycle');
        toast('Decision cycle started. Results appear under Activity shortly.');
      } catch (err) {
        report(err);
      }
    });
  }

  async function resume(button) {
    await busy(button, async () => {
      try {
        const status = await stepUp({
          title: 'Resume trading',
          message: "This clears today's halt. Loss limits are measured again from current equity.",
          confirm: 'Resume trading',
        }, (totp) => api('POST', '/api/bot/resume', { totp }));
        if (status === null) return;
        mergeStatus(status);
        toast('Trading resumed.');
      } catch (err) {
        report(err, 'Could not resume: ');
      } finally {
        refreshStatus();
      }
    });
  }

  // ------------------------------------------------------------------ live trading card
  function renderLiveCard() {
    const s = state.data && state.data.status;
    const allowed = Boolean((s && s.live_allowed) || (state.settings && state.settings.live_allowed));
    const card = $('live-card');
    card.hidden = !s || !(s.mode === 'live' || allowed);
    if (card.hidden) return;
    const live = s.mode === 'live';
    const armed = live && !s.shadow;
    setText($('live-allowed'), allowed ? 'Yes' : 'No');
    setText($('live-account'), live ? 'Live (real money)' : s.mode === 'paper' ? 'IBKR paper' : 'Simulator');
    const stateNode = $('live-state');
    setText(stateNode, armed ? 'Armed' : live ? 'Not armed (shadow)' : 'Off');
    stateNode.classList.toggle('neg', armed);
    let note;
    if (!allowed) note = 'Set TRADER_ALLOW_LIVE=true on the server to allow arming.';
    else if (!live) note = 'Arming is only possible while a live IBKR account is connected.';
    else if (armed) note = 'Real orders are sent, and each one still passes the risk checks. Disarm at any time to go back to shadow mode.';
    else note = 'Shadow mode: decisions are logged and new entries are blocked. The bot still closes positions it opened (stops, targets, time stops, closing before 4 PM). Arming needs a fresh 2FA code.';
    setText($('live-note'), note);
  }

  async function armLive() {
    await busy($('arm-btn'), async () => {
      try {
        const status = await stepUp({
          title: 'Arm live trading',
          message: 'Real orders will be sent to your IBKR account with real money.',
          confirm: 'Arm live trading',
          danger: true,
        }, (totp) => api('POST', '/api/live/arm', { totp }));
        if (status === null) return;
        mergeStatus(status);
        toast('Live trading armed: real orders will be sent.');
      } catch (err) {
        report(err, 'Not armed: ');
      } finally {
        refreshStatus();
      }
    });
  }

  /** Disarm blocks new entries only: say what keeps running when positions are open. */
  function confirmDisarm() {
    const positions = asList(state.data && state.data.positions).filter((p) => p && p.qty > 0);
    if (!positions.length) return true;
    return window.confirm(
      `Disarm with ${positions.length} open position(s)?\n\n`
      + 'No new entries will be sent. The bot keeps closing the positions it opened '
      + '(stops, targets, time stops, closing stocks before 4 PM, the daily-loss limit). '
      + 'Positions you opened yourself are yours to manage. KILL closes everything now.',
    );
  }

  async function disarmLive() {
    if (!confirmDisarm()) return;
    await busy($('disarm-btn'), async () => {
      try {
        mergeStatus(await api('POST', '/api/live/disarm'));
        toast('Live trading disarmed: no new entries; the bot still closes positions it opened (shadow mode).');
      } catch (err) {
        report(err, 'Could not disarm: ');
      } finally {
        refreshStatus();
      }
    });
  }

  function syncButtons() {
    const s = state.data && state.data.status;
    const toggle = $('startstop-btn');
    if (!isBusy(toggle)) {
      toggle.disabled = !s;
      setText(toggle, s && s.running ? 'Stop bot' : 'Start bot');
      toggle.classList.toggle('btn-primary', !(s && s.running));
    }
    const allowed = Boolean(s && s.live_allowed);
    const arm = $('arm-btn');
    const disarm = $('disarm-btn');
    if (!isBusy(arm)) arm.disabled = !(s && allowed && s.mode === 'live' && !s.live_armed);
    if (!isBusy(disarm)) disarm.disabled = !(s && s.live_armed);
    const save = $('settings-save');
    if (!isBusy(save)) save.disabled = !state.settingsDirty;
    $('settings-discard').disabled = !state.settingsDirty || isBusy(save);
    const replay = $('research-run');
    if (!isBusy(replay)) replay.disabled = state.researchRunning || state.researchFrames === 0;
  }

  // ------------------------------------------------------------------ activity
  async function loadInto(box, url, render) {
    box.classList.add('is-loading');
    box.setAttribute('aria-busy', 'true');
    try {
      render(await api('GET', url));
    } catch (err) {
      if (!err.fatal) box.replaceChildren(errorLine(`Could not load: ${err.message}`));
    } finally {
      box.classList.remove('is-loading');
      box.removeAttribute('aria-busy');
    }
  }

  function loadActivity() {
    return Promise.all([
      loadInto($('equity-chart'), '/api/equity?days=30', renderEquity),
      loadInto($('trades-list'), '/api/trades?limit=50', renderTrades),
      loadInto($('decisions-list'), '/api/decisions?limit=20', renderDecisions),
      loadInto($('orders-list'), '/api/orders?limit=50', renderOrders),
      loadInto($('days-list'), '/api/days?limit=30', renderDays),
    ]);
  }

  function renderEquity(data) {
    state.equity = asList(data)
      .map((p) => ({ t: parseTs(p && p.ts), v: p && p.equity }))
      .filter((p) => p.t && isNum(p.v))
      .map((p) => ({ t: p.t.getTime(), v: p.v }))
      .sort((a, b) => a.t - b.t);
    drawEquity();
  }

  /** Keep the min and max of each bucket so spikes survive downsampling. */
  function downsample(points, buckets) {
    if (points.length <= buckets * 2) return points;
    const size = Math.ceil(points.length / buckets);
    const out = [];
    for (let i = 0; i < points.length; i += size) {
      const chunk = points.slice(i, i + size);
      let lo = chunk[0];
      let hi = chunk[0];
      for (const p of chunk) {
        if (p.v < lo.v) lo = p;
        if (p.v > hi.v) hi = p;
      }
      if (lo === hi) out.push(lo);
      else out.push(...(lo.t < hi.t ? [lo, hi] : [hi, lo]));
    }
    const last = points[points.length - 1];
    if (out[out.length - 1] !== last) out.push(last);
    return out;
  }

  function niceTicks(lo, hi, count) {
    if (hi - lo < 1e-9) {
      const pad = Math.max(Math.abs(hi) * 0.01, 1);
      lo -= pad;
      hi += pad;
    }
    const raw = (hi - lo) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const ticks = [];
    for (let v = Math.floor(lo / step) * step; v <= Math.ceil(hi / step) * step + step / 2; v += step) {
      ticks.push(Number(v.toFixed(10)));
    }
    return { ticks, step };
  }

  /** Axis label with as many decimals as the step needs ($2.50 steps, $0.025 steps). */
  const tickFormats = new Map();
  function tickLabel(value, step) {
    const v = Math.abs(value) < step * 1e-6 ? 0 : value; // no "-$0.00" from float drift
    let digits = 0;
    while (digits < 4 && Math.abs(step * 10 ** digits - Math.round(step * 10 ** digits)) > 1e-6) digits += 1;
    if (digits === 1) digits = 2; // cents, not dimes
    if (!tickFormats.has(digits)) {
      tickFormats.set(digits, new Intl.NumberFormat('en-US', {
        style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits,
      }));
    }
    return `${v < 0 ? '-' : ''}${tickFormats.get(digits).format(Math.abs(v))}`;
  }

  /** Left gutter that fits the longest axis label (about 7px a character at 12px). */
  const gutter = (labels, min) => Math.max(min, 14 + 7 * Math.max(...labels.map((t) => t.length)));

  function equityReadout(first, last) {
    const change = last.v - first.v;
    const changePct = first.v ? (change / first.v) * 100 : null;
    return [
      'Latest ', el('strong', { text: money(last.v) }), ' · ',
      pnlSpan(change, `${signedMoney(change)}${isNum(changePct) ? ` (${signedPct(changePct)})` : ''}`),
      ` since ${fmtDate.format(new Date(first.t))}`,
    ];
  }

  function drawEquity(opts) {
    state.chartWidth = drawLineChart($('equity-chart'), $('equity-readout'), state.equity || [], opts);
  }

  /** Equity line with a crosshair readout (pointer, touch and arrow keys). Returns the
   * width it was drawn at, or 0 when there was nothing to draw. `keepPlace` (a resize or
   * background refresh of the same view) keeps the point being read. */
  function drawLineChart(box, readout, points, { keepPlace = false } = {}) {
    if (points.length < 2) {
      readout.replaceChildren(points.length ? `Latest ${money(points[0].v)}` : '');
      box.replaceChildren(empty(points.length
        ? 'Only one equity point so far. The chart fills in while the bot runs.'
        : 'No equity history yet. A point is recorded every few minutes while the bot runs.'));
      return 0;
    }
    const width = Math.max(260, Math.floor(box.clientWidth));
    const height = width < 520 ? 190 : 240;
    const prev = box.querySelector('svg.chart');
    const hadFocus = Boolean(prev) && prev === document.activeElement;
    const keptT = keepPlace && prev && prev.dataset.selected ? Number(prev.dataset.selected) : null;
    // Ticks from the full series (downsampling keeps each bucket's low and high anyway),
    // so the gutter can fit the labels before the plot width is known.
    const lowV = points.reduce((m, p) => Math.min(m, p.v), Infinity);
    const highV = points.reduce((m, p) => Math.max(m, p.v), -Infinity);
    const { ticks, step } = niceTicks(lowV, highV, 4);
    const tickText = ticks.map((t) => tickLabel(t, step));
    const pad = { left: gutter(tickText, 64), right: 12, top: 10, bottom: 26 };
    const data = downsample(points, Math.max(30, Math.floor((width - pad.left - pad.right) / 3)));
    const first = data[0];
    const last = data[data.length - 1];
    const yLo = ticks[0];
    const yHi = ticks[ticks.length - 1];
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const span = last.t - first.t || 1;
    const x = (t) => pad.left + ((t - first.t) / span) * plotW;
    const y = (v) => pad.top + ((yHi - v) / (yHi - yLo || 1)) * plotH;

    const root = svg('svg', {
      class: 'chart',
      width,
      height,
      viewBox: `0 0 ${width} ${height}`,
      role: 'img',
      tabindex: 0,
      'aria-label': `Equity chart: ${money(first.v)} on ${fmtDate.format(new Date(first.t))} to ${money(last.v)} on `
        + `${fmtDate.format(new Date(last.t))}; low ${money(lowV)}, high ${money(highV)}. `
        + 'Use the arrow keys to read individual points.',
    });
    ticks.forEach((tick, i) => {
      const ty = y(tick).toFixed(1);
      root.append(svg('line', { class: 'grid', x1: pad.left, x2: width - pad.right, y1: ty, y2: ty }));
      const label = svg('text', { class: 'tick', x: pad.left - 8, y: Number(ty) + 4, 'text-anchor': 'end' });
      label.textContent = tickText[i];
      root.append(label);
    });
    const multiDay = span > 36 * 3600 * 1000;
    const xFmt = (t) => (multiDay ? fmtDate : fmtTime).format(new Date(t));
    const xLabels = [[first.t, 'start'], [last.t, 'end']];
    if (width >= 480) xLabels.push([first.t + span / 2, 'middle']);
    for (const [t, anchor] of xLabels) {
      const label = svg('text', { class: 'tick', x: x(t).toFixed(1), y: height - 6, 'text-anchor': anchor });
      label.textContent = xFmt(t);
      root.append(label);
    }

    const xs = data.map((p) => x(p.t));
    const ys = data.map((p) => y(p.v));
    const line = xs.map((px, i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${ys[i].toFixed(1)}`).join('');
    const base = y(yLo).toFixed(1);
    root.append(
      svg('path', { class: 'area', d: `${line}L${xs[xs.length - 1].toFixed(1)},${base}L${xs[0].toFixed(1)},${base}Z` }),
      svg('path', { class: 'line', d: line }),
      svg('circle', { class: 'dot', cx: xs[xs.length - 1].toFixed(1), cy: ys[ys.length - 1].toFixed(1), r: 4 }),
    );
    const cross = svg('line', { class: 'crosshair', y1: pad.top, y2: height - pad.bottom, visibility: 'hidden' });
    const marker = svg('circle', { class: 'dot', r: 5, visibility: 'hidden' });
    root.append(cross, marker);

    const resetReadout = () => readout.replaceChildren(...equityReadout(first, last));
    let index = data.length - 1;
    let restoring = false; // focus moved here by a redraw, not by the reader
    const show = (i) => {
      index = Math.max(0, Math.min(data.length - 1, i));
      const px = xs[index].toFixed(1);
      cross.setAttribute('x1', px);
      cross.setAttribute('x2', px);
      marker.setAttribute('cx', px);
      marker.setAttribute('cy', ys[index].toFixed(1));
      cross.setAttribute('visibility', 'visible');
      marker.setAttribute('visibility', 'visible');
      readout.replaceChildren(el('strong', { text: money(data[index].v) }), ` · ${fmtDateTime.format(new Date(data[index].t))}`);
      root.dataset.selected = String(data[index].t);
    };
    const hide = () => {
      cross.setAttribute('visibility', 'hidden');
      marker.setAttribute('visibility', 'hidden');
      delete root.dataset.selected;
      resetReadout();
    };
    const nearest = (event) => {
      const rect = root.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * width;
      let lo = 0;
      let hi = xs.length - 1;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (xs[mid] < px) lo = mid;
        else hi = mid;
      }
      return px - xs[lo] < xs[hi] - px ? lo : hi;
    };
    root.addEventListener('pointermove', (event) => show(nearest(event)));
    root.addEventListener('pointerdown', (event) => show(nearest(event)));
    // A lifted finger also 'leaves': keep a tapped point on screen until the next tap. A
    // finger that scrolls the page instead (pointercancel) clears it.
    root.addEventListener('pointerleave', (event) => {
      if (event.pointerType !== 'touch') hide();
    });
    root.addEventListener('pointercancel', hide);
    root.addEventListener('focus', () => {
      readout.setAttribute('aria-live', 'polite');
      if (!restoring) show(index);
    });
    root.addEventListener('blur', () => {
      if (!root.isConnected) return; // replaced by a redraw, which owns the readout now
      readout.removeAttribute('aria-live');
      hide();
    });
    root.addEventListener('keydown', (event) => {
      const moves = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: data.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      show(moves[event.key]);
    });
    box.replaceChildren(root);
    resetReadout();
    // A redraw keeps focus on the chart, and with keepPlace the point being read (if it
    // is still in the window).
    // Against the full series: the downsampled first point moves with the chart width.
    const keep = keptT !== null && keptT >= points[0].t && keptT <= points[points.length - 1].t;
    if (keep) {
      index = data.reduce((best, p, i) => (Math.abs(p.t - keptT) < Math.abs(data[best].t - keptT) ? i : best), 0);
    }
    if (hadFocus) {
      restoring = true;
      root.focus({ preventScroll: true });
      restoring = false;
    }
    if (keep) show(index);
    return width;
  }

  /** SVG path for one bar: square at the zero line, 4px rounded corners at the data end. */
  function barPath(x, w, yZero, yValue) {
    const up = yValue < yZero;
    const h = Math.abs(yZero - yValue);
    const r = Math.min(4, w / 2, h);
    const top = Math.min(yZero, yValue);
    const bottom = top + h;
    if (up) {
      return `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + w - r}`
        + `Q${x + w},${top} ${x + w},${top + r}V${bottom}Z`;
    }
    return `M${x},${top}V${bottom - r}Q${x},${bottom} ${x + r},${bottom}H${x + w - r}`
      + `Q${x + w},${bottom} ${x + w},${bottom - r}V${top}Z`;
  }

  /**
   * One value per day as columns. kind 'pnl' grows up for a gain and down for a loss (the
   * direction carries the sign; the two colors only repeat it); kind 'cost' is a single
   * neutral series from zero. Hover, tap or arrow keys show the day in the readout.
   * Returns the width it was drawn at.
   */
  function drawBarChart(box, readout, items, { kind, summary, detail, keepPlace = false }) {
    const width = Math.max(260, Math.floor(box.clientWidth));
    const height = width < 520 ? 170 : 200;
    const prev = box.querySelector('svg.chart');
    const hadFocus = Boolean(prev) && prev === document.activeElement;
    const keptDay = keepPlace && prev ? prev.dataset.selected : undefined;
    const values = items.map((d) => d.v);
    const lowV = Math.min(0, ...values);
    const highV = Math.max(0, ...values);
    // Costs never go below zero: an all-zero cost period gets a $0-$1 axis, not -$1..$1.
    const { ticks, step } = niceTicks(lowV, kind === 'cost' && highV <= 0 ? 1 : highV, 3);
    const tickText = ticks.map((t) => tickLabel(t, step));
    const pad = { left: gutter(tickText, 60), right: 8, top: 10, bottom: 26 };
    const yLo = Math.min(ticks[0], 0);
    const yHi = Math.max(ticks[ticks.length - 1], 0);
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const band = plotW / items.length;
    const barW = Math.max(2, Math.min(24, band * 0.68)); // capped, with air between days
    const y = (v) => pad.top + ((yHi - v) / (yHi - yLo || 1)) * plotH;
    const cx = (i) => pad.left + band * (i + 0.5);
    const yZero = y(0);

    const root = svg('svg', {
      class: 'chart',
      width,
      height,
      viewBox: `0 0 ${width} ${height}`,
      role: 'img',
      tabindex: 0,
      'aria-label': `${summary.label}. Use the arrow keys to read each day.`,
    });
    ticks.forEach((tick, i) => {
      const ty = y(tick).toFixed(1);
      root.append(svg('line', { class: tick === 0 ? 'zero' : 'grid', x1: pad.left, x2: width - pad.right, y1: ty, y2: ty }));
      const label = svg('text', { class: 'tick', x: pad.left - 8, y: Number(ty) + 4, 'text-anchor': 'end' });
      label.textContent = tickText[i];
      root.append(label);
    });
    if (!ticks.includes(0)) {
      root.append(svg('line', { class: 'zero', x1: pad.left, x2: width - pad.right, y1: yZero.toFixed(1), y2: yZero.toFixed(1) }));
    }
    const xLabels = [[0, 'start']];
    if (items.length > 1) xLabels.push([items.length - 1, 'end']);
    if (width >= 480 && items.length > 4) xLabels.push([Math.floor(items.length / 2), 'middle']);
    for (const [i, anchor] of xLabels) {
      const tx = anchor === 'start' ? pad.left : anchor === 'end' ? width - pad.right : cx(i);
      const label = svg('text', { class: 'tick', x: tx.toFixed(1), y: height - 6, 'text-anchor': anchor });
      label.textContent = fmtDayAxis.format(items[i].t);
      root.append(label);
    }
    const bars = items.map((d, i) => {
      const cls = kind === 'cost' ? 'bar cost' : d.v >= 0 ? 'bar gain' : 'bar loss';
      const node = svg('path', {
        class: cls,
        d: Math.abs(y(d.v) - yZero) < 0.5 ? '' : barPath(cx(i) - barW / 2, barW, yZero, y(d.v)),
      });
      root.append(node);
      return node;
    });

    const resetReadout = () => {
      readout.replaceChildren(...summary.readout);
      for (const bar of bars) bar.classList.remove('active', 'dim');
      delete root.dataset.selected;
    };
    let index = items.length - 1;
    let restoring = false; // focus moved here by a redraw, not by the reader
    const show = (i) => {
      index = Math.max(0, Math.min(items.length - 1, i));
      bars.forEach((bar, j) => {
        bar.classList.toggle('active', j === index);
        bar.classList.toggle('dim', j !== index);
      });
      readout.replaceChildren(...detail(items[index]));
      root.dataset.selected = items[index].day;
    };
    const nearest = (event) => {
      const rect = root.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * width;
      return Math.floor((px - pad.left) / band);
    };
    root.addEventListener('pointermove', (event) => show(nearest(event)));
    root.addEventListener('pointerdown', (event) => show(nearest(event)));
    root.addEventListener('pointerleave', (event) => {
      if (event.pointerType !== 'touch') resetReadout();
    });
    root.addEventListener('pointercancel', resetReadout); // the finger scrolled the page
    root.addEventListener('focus', () => {
      readout.setAttribute('aria-live', 'polite');
      if (!restoring) show(index);
    });
    root.addEventListener('blur', () => {
      if (!root.isConnected) return; // replaced by a redraw, which owns the readout now
      readout.removeAttribute('aria-live');
      resetReadout();
    });
    root.addEventListener('keydown', (event) => {
      const moves = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: items.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      show(moves[event.key]);
    });
    box.replaceChildren(root);
    resetReadout();
    // A redraw keeps focus on the chart, and with keepPlace the day being read.
    const kept = keptDay ? items.findIndex((d) => d.day === keptDay) : -1;
    if (kept >= 0) index = kept;
    if (hadFocus) {
      restoring = true;
      root.focus({ preventScroll: true });
      restoring = false;
    }
    if (kept >= 0) show(index);
    return width;
  }

  // ------------------------------------------------------------------ dashboard charts
  const nyDate = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
  });

  /** "YYYY-MM-DD" -> a Date at noon UTC (stable across time zones and DST). */
  const dayDate = (day) => new Date(`${day}T12:00:00Z`);

  /** Daily rows inside the period, oldest first: weekdays, plus any other day with activity.
   * `today` is the New York date, "YYYY-MM-DD". */
  function dashDays(rows, days, today = nyDate.format(new Date())) {
    const end = dayDate(today);
    const cutoff = new Date(end.getTime() - (days - 1) * 86400000).toISOString().slice(0, 10);
    return asList(rows)
      .filter((d) => d && typeof d.day === 'string' && d.day >= cutoff)
      .map((d) => {
        const pnl = isNum(d.start_equity) && isNum(d.end_equity) ? d.end_equity - d.start_equity : null;
        const cost = isNum(d.llm_cost) ? d.llm_cost : 0;
        return { day: d.day, t: dayDate(d.day), pnl, cost, trades: d.trades || 0, wins: d.wins || 0, losses: d.losses || 0 };
      })
      .filter((d) => {
        const weekday = d.t.getUTCDay() >= 1 && d.t.getUTCDay() <= 5;
        return weekday || d.trades > 0 || d.cost > 0 || (isNum(d.pnl) && Math.abs(d.pnl) >= 0.005);
      })
      .sort((a, b) => (a.day < b.day ? -1 : 1));
  }

  async function loadDashboardCharts({ background = false } = {}) {
    if (state.dashChartsBusy) return;
    state.dashChartsBusy = true;
    const days = Number($('performance-days').value || '30');
    const boxes = ['dash-equity-chart', 'dash-pnl-chart', 'dash-cost-chart'].map((id) => $(id));
    for (const box of boxes) box.classList.add('is-loading');
    try {
      const [equity, rows] = await Promise.all([
        api('GET', `/api/equity?days=${days}`, undefined, { background }),
        api('GET', `/api/days?limit=${days + 14}`, undefined, { background }),
      ]);
      if (Number($('performance-days').value || '30') !== days) return;
      state.dashEquity = asList(equity)
        .map((p) => ({ t: parseTs(p && p.ts), v: p && p.equity }))
        .filter((p) => p.t && isNum(p.v))
        .map((p) => ({ t: p.t.getTime(), v: p.v }))
        .sort((a, b) => a.t - b.t);
      state.dashDays = dashDays(rows, days);
      state.dashPeriod = days;
      // The 60-second background refresh redraws only when the data moved.
      const fresh = changed('dash-charts', [days, state.dashEquity, state.dashDays]);
      if (fresh || !background) drawDashboardCharts({ keepPlace: background });
      state.dashChartsLoadedAt = Date.now();
    } catch (err) {
      rendered.delete('dash-charts'); // the next good response replaces the error line
      if (!err.fatal) boxes[0].replaceChildren(errorLine(`Charts unavailable: ${err.message}`));
    } finally {
      state.dashChartsBusy = false;
      for (const box of boxes) box.classList.remove('is-loading');
      if (Number($('performance-days').value || '30') !== days) loadDashboardCharts();
    }
  }

  /** Cents for a dollar or more; up to 4 decimals below that, where AI costs live. */
  const costText = (v) => (Math.abs(v) >= 1 ? usd : usdFine).format(v);

  function drawDashboardCharts({ keepPlace = false } = {}) {
    const days = state.dashPeriod || 30;
    const period = days === 1 ? 'today' : `last ${days} days`;
    setText($('dash-equity-title'), days === 1 ? 'Equity, last 24 hours' : `Equity, ${period}`);
    state.dashChartsWidth = drawLineChart($('dash-equity-chart'), $('dash-equity-readout'), state.dashEquity || [], { keepPlace })
      || Math.floor($('dash-equity-chart').clientWidth);
    const rows = state.dashDays || [];
    const pnlRows = rows.filter((d) => isNum(d.pnl)).map((d) => ({ ...d, v: d.pnl }));
    const costRows = rows.map((d) => ({ ...d, v: d.cost }));
    const pnlBox = $('dash-pnl-chart');
    const costBox = $('dash-cost-chart');
    if (days === 1 || pnlRows.length < 2) {
      $('dash-pnl-readout').replaceChildren();
      $('dash-cost-readout').replaceChildren();
      const why = days === 1
        ? 'Choose 7 days or more to see one bar per day. Today’s numbers are in the tiles above.'
        : 'Bars appear once the bot has run on at least two trading days in this period.';
      pnlBox.replaceChildren(empty(why));
      costBox.replaceChildren(empty(why));
    } else {
      const total = pnlRows.reduce((sum, d) => sum + d.v, 0);
      const up = pnlRows.filter((d) => d.v > 0.005).length;
      const down = pnlRows.filter((d) => d.v < -0.005).length;
      drawBarChart(pnlBox, $('dash-pnl-readout'), pnlRows, {
        kind: 'pnl',
        keepPlace,
        summary: {
          label: `Profit or loss per day, ${period}: total ${signedMoney(total)}, ${up} up days, ${down} down days`,
          readout: [pnlSpan(total, signedMoney(total)), ` total · ${up} up, ${down} down days`],
        },
        detail: (d) => [
          pnlSpan(d.v, signedMoney(d.v)), ` · ${dayLabel(d.day)}`,
          d.trades ? ` · ${d.trades} ${d.trades === 1 ? 'trade' : 'trades'}` : ' · no trades',
        ],
      });
      const spent = costRows.reduce((sum, d) => sum + d.v, 0);
      drawBarChart(costBox, $('dash-cost-readout'), costRows, {
        kind: 'cost',
        keepPlace,
        summary: {
          label: `AI cost per day, ${period}: total ${costText(spent)}`,
          readout: [el('strong', { text: costText(spent) }), ` total · ${costText(spent / costRows.length)} a day on average`],
        },
        detail: (d) => [el('strong', { text: usdFine.format(d.v) }), ` · ${dayLabel(d.day)}`],
      });
    }
    $('dash-days-table').replaceChildren(rows.length ? table([
      { label: 'Day', head: true, value: (d) => dayLabel(d.day) },
      { label: 'Profit or loss', num: true, value: (d) => (isNum(d.pnl) ? pnlSpan(d.pnl, signedMoney(d.pnl)) : DASH) },
      { label: 'Trades', num: true, value: (d) => String(d.trades) },
      { label: 'Wins / losses', num: true, value: (d) => `${d.wins} / ${d.losses}` },
      { label: 'AI cost', num: true, value: (d) => usdFine.format(d.cost) },
    ], rows.slice().reverse()) : empty('No daily records in this period yet.'));
  }

  function renderTrades(data) {
    const trades = asList(data);
    const box = $('trades-list');
    const summary = $('trades-summary');
    summary.hidden = trades.length === 0;
    if (!trades.length) {
      box.replaceChildren(empty('No closed trades yet.'));
      return;
    }
    const wins = trades.filter((t) => isNum(t.pnl) && t.pnl > 0).length;
    const net = trades.reduce((sum, t) => sum + (isNum(t.pnl) ? t.pnl : 0), 0);
    const fees = trades.reduce((sum, t) => sum + (isNum(t.commission) ? t.commission : 0), 0);
    summary.replaceChildren(
      `${trades.length} trade${trades.length === 1 ? '' : 's'} · ${wins} won (${pct((wins / trades.length) * 100, 0)}) · net `,
      pnlSpan(net),
      ` after ${money(fees)} in fees`,
    );
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (t) => symbolCell(t.instrument, setupLabel(t.setup)) },
      { label: 'Closed', value: (t) => when(t.exit_ts) },
      { label: 'Qty', num: true, value: (t) => qty(t.qty) },
      { label: 'Entry', num: true, value: (t) => price(t.entry_price) },
      { label: 'Exit', num: true, value: (t) => price(t.exit_price) },
      { label: 'P&L', num: true, value: (t) => pnlSpan(t.pnl) },
      { label: 'R', num: true, value: (t) => pnlSpan(t.r_multiple, rMult(t.r_multiple)) },
      { label: 'Exit reason', note: true, value: (t) => t.exit_reason || DASH },
    ], trades));
  }

  const ACTION_TONE = { BUY: 'info', CLOSE: 'warn', HOLD: null };

  function decisionItem(d) {
    const levels = [];
    if (isNum(d.limit_price)) levels.push(`limit ${price(d.limit_price)}`);
    if (isNum(d.stop_loss)) levels.push(`stop ${price(d.stop_loss)}`);
    if (isNum(d.take_profit)) levels.push(`target ${price(d.take_profit)}`);
    if (isNum(d.size_fraction) && d.size_fraction < 1) levels.push(`size ×${d.size_fraction.toFixed(2)}`);
    return el('li', { class: 'decision' }, [
      el('div', { class: 'decision-head' }, [
        chipNode(d.action || '?', ACTION_TONE[d.action] ?? null),
        el('span', { text: `${d.symbol || '?'}${d.asset_class === 'CRYPTO' ? ' (crypto)' : ''}` }),
        el('span', { class: 'entry-meta', text: isNum(d.conviction) ? `conviction ${Math.round(d.conviction * 100)}%` : '' }),
      ]),
      levels.length ? el('p', { class: 'tabular', text: levels.join(' · ') }) : null,
      d.rationale ? el('details', { class: 'more' }, [
        el('summary', { text: 'Decision rationale' }),
        el('p', { text: d.rationale }),
      ]) : null,
    ]);
  }

  function voteList(name, decisions) {
    return el('div', { class: 'vote' }, [
      el('p', { class: 'vote-maker', text: `${makerLabel(name)} voted` }),
      decisions.length
        ? el('ul', { class: 'decision-list' }, decisions.map(decisionItem))
        : el('p', { class: 'small muted', text: 'No decisions (HOLD on everything).' }),
    ]);
  }

  function votesBlock(votes) {
    const names = votes && typeof votes === 'object' ? Object.keys(votes) : [];
    if (!names.length) return null;
    const box = el('details', { class: 'more votes' }, [
      el('summary', { text: 'How each model voted' }),
      ...names.map((name) => voteList(name, asList(votes[name]))),
    ]);
    return box;
  }

  function decisionEntry(b) {
    const maker = makerLabel(b.maker);
    const decisions = asList(b.decisions);
    const candidates = asList(b.candidates);
    const tokens = b.input_tokens || b.output_tokens
      ? `${(b.input_tokens || 0).toLocaleString('en-US')} in / ${(b.output_tokens || 0).toLocaleString('en-US')} out tokens · `
      : '';
    return el('li', { class: 'entry' }, [
      el('div', { class: 'entry-head' }, [
        el('span', { class: 'entry-title', text: b.model ? `${maker} · ${b.model}` : maker }),
        b.refused ? chipNode('Refused', 'warn') : null,
        b.error ? chipNode('Error', 'bad') : null,
        el('span', { class: 'entry-meta', text: when(b.ts) }),
      ]),
      b.market_view ? el('p', { class: 'entry-text', text: b.market_view }) : null,
      decisions.length
        ? el('ul', { class: 'decision-list' }, decisions.map(decisionItem))
        : el('p', { class: 'small muted', text: 'No decisions in this cycle.' }),
      votesBlock(b.votes),
      b.error ? el('p', { class: 'inline-error', text: `Error: ${b.error}` }) : null,
      candidates.length
        ? el('details', { class: 'more' }, [
          el('summary', { text: `${candidates.length} candidate${candidates.length === 1 ? '' : 's'} considered` }),
          el('ul', {}, candidates.map((c) => el('li', {
            class: 'tabular',
            text: `${(c.instrument && c.instrument.symbol) || '?'} · ${setupLabel(c.setup)} · score ${isNum(c.score) ? c.score.toFixed(2) : DASH}`
              + ` · ${price(c.price)} (stop ${price(c.suggested_stop)}, target ${price(c.suggested_target)})`,
          }))),
        ])
        : null,
      el('p', { class: 'entry-meta', text: `${tokens}cost ${fineMoney(b.cost_usd)}` }),
    ]);
  }

  function renderDecisions(data) {
    const batches = [...asList(data)].sort((a, b) => (parseTs(b.ts)?.getTime() || 0) - (parseTs(a.ts)?.getTime() || 0));
    $('decisions-list').replaceChildren(batches.length
      ? el('ul', { class: 'entries' }, batches.map(decisionEntry))
      : empty('No decision cycles yet.'));
  }

  function renderOrders(data) {
    const orders = asList(data);
    const box = $('orders-list');
    if (!orders.length) {
      box.replaceChildren(empty('No orders yet.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (o) => symbolCell(o.request && o.request.instrument, setupLabel(o.request && o.request.setup)) },
      { label: 'Time', value: (o) => when(o.created_at) },
      { label: 'Side', value: (o) => `${(o.request && o.request.side) || DASH}${o.request && o.request.is_exit ? ' (exit)' : ''}` },
      { label: 'Qty', num: true, value: (o) => qty(o.request && o.request.qty) },
      { label: 'Type', value: (o) => orderType(o.request || {}) },
      { label: 'Status', value: (o) => statusChip(o.status) },
      { label: 'Fill', num: true, value: (o) => (o.filled_qty ? `${qty(o.filled_qty)} @ ${price(o.avg_fill_price)}` : DASH) },
      { label: 'Fees', num: true, value: (o) => (o.commission ? money(o.commission) : DASH) },
      { label: 'Note', note: true, value: (o) => o.message || (o.request && o.request.reason) || DASH },
    ], orders));
  }

  function renderDays(data) {
    const days = asList(data);
    const box = $('days-list');
    if (!days.length) {
      box.replaceChildren(empty('No trading days recorded yet.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Day', head: true, value: (d) => el('span', { class: 'cell-title', text: dayLabel(d.day) }) },
      { label: 'Start equity', num: true, value: (d) => money(d.start_equity) },
      { label: 'End equity', num: true, value: (d) => money(d.end_equity) },
      { label: 'Realized P&L', num: true, value: (d) => pnlSpan(d.realized_pnl) },
      { label: 'Entries', num: true, value: (d) => String(d.trades ?? 0) },
      { label: 'Won / lost', num: true, value: (d) => `${d.wins ?? 0} / ${d.losses ?? 0}` },
      { label: 'Fees', num: true, value: (d) => money(d.fees) },
      { label: 'AI cost', num: true, value: (d) => fineMoney(d.llm_cost) },
      { label: 'Halt', note: true, value: (d) => (d.halted ? sentence(d.halt_reason || 'halted') : '') },
    ], days));
  }

  // ------------------------------------------------------------------ signals
  function loadSignals() {
    return Promise.all([loadStats(), loadSignalList(), loadFilledSetups(), loadResearch()]);
  }

  function loadSignalPeriod() {
    return Promise.all([loadStats(), loadFilledSetups(), loadResearch()]);
  }

  function loadFilledSetups() {
    const days = encodeURIComponent($('stats-days').value || '30');
    return loadInto($('filled-setups-list'), `/api/performance?days=${days}`, (p) => {
      const rows = asList(p.setup_stats);
      $('filled-setups-list').replaceChildren(rows.length ? table([
        { label: 'Setup', head: true, value: (s) => setupLabel(s.setup) },
        { label: 'Closed trades', num: true, value: (s) => String(s.trades ?? 0) },
        { label: 'Trading P&L', num: true, value: (s) => pnlSpan(s.net_pnl) },
        { label: 'Per trade', num: true, value: (s) => pnlSpan(s.net_expectancy) },
        { label: 'Observed days', num: true, value: (s) => String(s.days_observed ?? 0) },
        { label: 'Exploratory range / trade', num: true, value: (s) => isNum(s.confidence_low) && isNum(s.confidence_high)
          ? `${signedMoney(s.confidence_low)} to ${signedMoney(s.confidence_high)}` : 'More history needed' },
      ], rows) : empty('No closed trades in this period. Profitability has not been established.'));
    });
  }

  async function loadResearch() {
    clearTimeout(state.researchTimer);
    if (document.hidden || state.expired || state.redirecting) return;
    const request = ++state.researchRequest;
    const days = $('stats-days').value || '30';
    const box = $('research-list');
    box.setAttribute('aria-busy', 'true');
    try {
      const data = await api('GET', `/api/research?days=${encodeURIComponent(days)}`, undefined, { background: state.researchRunning });
      if (request === state.researchRequest) renderResearch(data);
    } catch (err) {
      if (!err.fatal && request === state.researchRequest) box.replaceChildren(errorLine(`Comparison unavailable: ${err.message}`));
    } finally {
      if (request === state.researchRequest) box.removeAttribute('aria-busy');
    }
  }

  async function runResearch() {
    await busy($('research-run'), async () => {
      const request = ++state.researchRequest;
      try {
        const data = await api('POST', '/api/research/run', { days: Number($('stats-days').value || '30') });
        if (request === state.researchRequest) renderResearch(data);
      } catch (err) {
        report(err, 'Could not start comparison: ');
      }
    });
  }

  function renderResearch(data) {
    state.researchRunning = data.status === 'running';
    state.researchFrames = isNum(data.frame_count) ? data.frame_count : null;
    syncButtons();
    const box = $('research-list');
    const report = data.report;
    if (!report) {
      const message = data.status === 'running' ? 'Comparing captured decisions…'
        : data.status === 'failed' ? `Comparison failed: ${data.error || 'Try again.'}`
          : `No comparison has been run for this period. ${data.frame_count ?? 0} captured data frames are available. New cycles will add data for replay.`;
      box.replaceChildren(empty(message));
    } else {
      const arms = Object.entries(report.arms || {}).map(([name, arm]) => ({ name, ...arm }));
      const warnings = [...asList(report.warnings)];
      if (!warnings.some((text) => /original portfolio|alternative portfolio/i.test(text))) {
        warnings.unshift('Captured-vote selection replay: model opinions were recorded against the original portfolio. This is not an independent, portfolio-specific AI test.');
      }
      if (!warnings.some((text) => /sparse|between observations|intra-cycle/i.test(text))) {
        warnings.push('Sparse captured cycles can miss price moves between observations. These replay results do not demonstrate executable live profitability.');
      }
      box.replaceChildren(
        data.status === 'running' ? el('p', { class: 'inline-warn', text: 'A new comparison is running. Previous results are shown below.' }) : '',
        data.status === 'failed' ? errorLine(`Last run failed: ${data.error || 'Try again.'} Previous results follow.`) : '',
        el('p', { class: 'small muted mt-12', text: `${report.frame_count ?? 0} captured frames · ${report.decision_cycles ?? 0} decision cycles · starting virtual equity ${money(report.starting_equity)}` }),
        el('p', { class: 'small muted mt-8', text: `Captured ${when(report.started_at)} to ${when(report.ended_at)} · sources: ${asList(report.source_types).join(', ') || 'unknown'}${data.generated_at ? ` · generated ${when(data.generated_at)}` : ''}` }),
        arms.length ? table([
          { label: 'Decision method', head: true, value: (a) => makerLabel(a.name) },
          { label: 'Trading P&L', num: true, value: (a) => pnlSpan(a.trading_pnl_usd) },
          { label: 'Model cost', num: true, value: (a) => isNum(a.model_cost_usd) ? fineMoney(a.model_cost_usd) : 'Unknown split' },
          { label: 'After model cost', num: true, value: (a) => isNum(a.all_in_pnl_usd) ? pnlSpan(a.all_in_pnl_usd) : 'Incomplete costs' },
          { label: 'Drawdown', num: true, value: (a) => [
            el('span', { text: pct(a.max_drawdown_pct) }),
            el('span', { class: 'cell-sub', text: a.drawdown_basis === 'trading_only' ? 'Before model costs' : 'After model costs' }),
          ] },
          { label: 'Closed trades', num: true, value: (a) => String(asList(a.trades).length) },
          { label: 'Votes observed / missing', num: true, value: (a) => a.name === 'rules' ? 'Not applicable' : `${a.observed_vote_cycles ?? 0} / ${a.missing_vote_cycles ?? 0}` },
        ], arms) : empty('No replay accounts available.'),
        el('p', { class: 'small muted mt-8', text: 'Trading P&L includes virtual open positions and commissions. After model cost excludes hosting and market-data expenses. Missing model votes are not filled with invented decisions.' }),
        el('ul', { class: 'performance-warnings mt-12' }, warnings.map((text) => el('li', { text }))),
        el('details', { class: 'more' }, [
          el('summary', { text: 'Replay assumptions and account warnings' }),
          el('ul', {}, [
            ...asList(report.assumptions).map((text) => el('li', { text })),
            ...arms.flatMap((a) => asList(a.warnings).map((text) => el('li', { text: `${makerLabel(a.name)}: ${text}` }))),
          ]),
        ]),
      );
    }
    clearTimeout(state.researchTimer);
    if (state.researchRunning && state.view === 'signals' && !state.expired) state.researchTimer = setTimeout(loadResearch, 5000);
  }

  function loadStats() {
    const days = encodeURIComponent($('stats-days').value || '30');
    return loadInto($('stats-list'), `/api/signals/stats?days=${days}`, renderStats);
  }

  function loadSignalList() {
    const setup = $('signals-setup').value;
    const query = setup ? `&setup=${encodeURIComponent(setup)}` : '';
    return loadInto($('signals-list'), `/api/signals?limit=100${query}`, renderSignals);
  }

  function rememberSetups(names) {
    const select = $('signals-setup');
    for (const name of names) {
      if (!name || state.setups.has(name)) continue;
      state.setups.add(name);
      select.append(el('option', { value: name, text: setupLabel(name) }));
    }
  }

  function renderStats(data) {
    const stats = asList(data);
    rememberSetups(stats.map((s) => s.setup));
    const box = $('stats-list');
    if (!stats.length) {
      box.replaceChildren(empty('No signals logged in this period yet.'));
      return;
    }
    const soft = (s, text) => el('span', { class: s.enough_data ? null : 'tentative', text });
    const softR = (s, v) => (s.enough_data ? pnlSpan(v, rMult(v)) : soft(s, rMult(v)));
    box.replaceChildren(table([
      {
        label: 'Setup',
        head: true,
        value: (s) => [
          el('span', { class: 'cell-title', text: `${setupLabel(s.setup)} ` }),
          el('span', s.enough_data
            ? { class: 'badge badge-few', text: 'N ≥ 30: initial review' }
            : {
              class: 'badge badge-few',
              text: 'Limited sample',
              title: `${s.evaluated ?? 0} evaluated signals; price diagnostics are not proof of profitability`,
            }),
        ],
      },
      { label: 'Signals', num: true, value: (s) => String(s.signals ?? 0) },
      { label: 'Taken', num: true, value: (s) => String(s.taken ?? 0) },
      { label: 'Evaluated (N)', num: true, value: (s) => String(s.evaluated ?? 0) },
      { label: 'Target first', num: true, value: (s) => soft(s, pct(s.target_first_pct, 0)) },
      { label: 'Avg R at close', num: true, value: (s) => softR(s, s.avg_r_close) },
      { label: 'Avg MFE', num: true, value: (s) => soft(s, rMult(s.avg_mfe_r)) },
      { label: 'Avg MAE', num: true, value: (s) => soft(s, rMult(s.avg_mae_r)) },
    ], stats));
  }

  const HIT = { target: ['Target first', 'ok'], stop: ['Stop first', 'bad'], neither: ['Neither hit', null] };

  function signalOutcome(sig) {
    if (!sig.evaluated) return chipNode('Pending', null);
    const [label, tone] = HIT[sig.first_hit] || ['Scored', null];
    const parts = [];
    if (isNum(sig.r_close)) parts.push(`${rMult(sig.r_close)} at close`);
    if (isNum(sig.mfe_r) || isNum(sig.mae_r)) parts.push(`MFE ${rMult(sig.mfe_r)} / MAE ${rMult(sig.mae_r)}`);
    return [chipNode(label, tone), parts.length ? el('span', { class: 'cell-sub', text: parts.join(' · ') }) : null];
  }

  function renderSignals(data) {
    const signals = asList(data);
    rememberSetups(signals.map((s) => s.setup));
    const box = $('signals-list');
    if (!signals.length) {
      box.replaceChildren(empty('No signals yet. Every qualifying setup appears here, traded or not.'));
      return;
    }
    box.replaceChildren(table([
      { label: 'Symbol', head: true, value: (s) => symbolCell(s.instrument, setupLabel(s.setup)) },
      { label: 'Time', value: (s) => when(s.ts) },
      { label: 'Score', num: true, value: (s) => (isNum(s.score) ? s.score.toFixed(2) : DASH) },
      { label: 'Entry / stop / target', num: true, value: (s) => `${price(s.price)} / ${price(s.stop)} / ${price(s.target)}` },
      {
        label: 'Taken',
        note: true,
        value: (s) => (s.taken ? chipNode('Taken', 'ok') : el('span', { text: s.skip_reason ? `Skipped: ${s.skip_reason}` : 'Not taken' })),
      },
      { label: 'Outcome', value: signalOutcome },
    ], signals));
  }

  // ------------------------------------------------------------------ settings
  const SETTINGS_GROUPS = [
    { title: 'Bot', keys: ['autopilot', 'cycle_seconds', 'monitor_seconds', 'trade_stocks', 'trade_crypto', 'bar_size'] },
    {
      title: 'Universe',
      keys: ['stock_universe', 'crypto_universe', 'dynamic_universe', 'min_avg_volume', 'allow_fractional_stocks'],
    },
    {
      title: 'Stock session',
      note: 'New York (market) time.',
      keys: ['stock_entry_start', 'stock_entry_cutoff', 'flatten_stocks_before_close'],
    },
    {
      title: 'AI',
      note: 'Consensus: a BUY needs Claude AND Grok to agree; either one can close a position. Leaving consensus needs a 2FA code.',
      keys: ['llm_enabled', 'decision_mode', 'llm_model', 'llm_effort', 'llm_min_candidate_score'],
    },
    {
      title: 'Setup selection',
      note: 'Only enabled setups can create new entries. Existing positions continue to follow their exit rules.',
      keys: ['enabled_setups'],
    },
    {
      title: 'Operating costs',
      object: 'operating_costs',
      note: 'Monthly USD estimates are prorated over the selected reporting period. Leave unknown costs blank; enter 0 for a free service.',
    },
    {
      title: 'Risk limits',
      object: 'risk',
      note: 'Loosening any of these needs a 2FA code. Sizing never depends on the daily target.',
    },
    {
      title: 'Exits',
      object: 'exits',
      note: 'Managed by the bot on top of the stop-loss (and the target, unless winners run) placed with every entry. 0 turns a rule off.',
    },
  ];

  const FIELDS = {
    autopilot: { label: 'Autopilot', help: 'Off: every order waits for your approval on the Dashboard.' },
    cycle_seconds: { label: 'Decision interval', unit: 'seconds', int: true, min: 30, max: 3600 },
    monitor_seconds: {
      label: 'Monitor interval', unit: 'seconds', int: true, min: 5, max: 300,
      help: 'How often stops, targets and the loss limit are checked.',
    },
    trade_stocks: { label: 'Trade stocks' },
    trade_crypto: { label: 'Trade crypto' },
    bar_size: { label: 'Bar size', options: ['1 min', '5 mins', '15 mins'] },
    stock_universe: {
      label: 'Stock symbols', list: true,
      help: 'Comma-separated, e.g. F, SOFI, INTC. Symbols the account cannot afford are skipped.',
    },
    crypto_universe: { label: 'Crypto symbols', list: true, help: 'Comma-separated, e.g. BTC, ETH.' },
    dynamic_universe: { label: 'Add scanner top movers', help: 'Needs real-time market data.' },
    min_avg_volume: { label: 'Minimum average volume', unit: 'shares/day', int: true, min: 0 },
    allow_fractional_stocks: { label: 'Fractional shares' },
    stock_entry_start: { label: 'Entries from', time: true },
    stock_entry_cutoff: { label: 'No new entries after', time: true },
    flatten_stocks_before_close: {
      label: 'Flatten stocks before the close',
      help: 'Day trades only: flat 10 minutes before the close. Turning it off needs a 2FA code.',
    },
    llm_enabled: { label: 'Use AI for decisions', help: 'Off: rules-only decisions, whatever the mode.' },
    decision_mode: {
      label: 'Decision mode', options: ['consensus', 'claude', 'grok', 'rules'],
      help: 'Consensus requires Claude and Grok to both BUY and pays for both models. Single-model modes use one model; rules uses no AI. Compare results after model costs before choosing a mode.',
    },
    llm_model: { label: 'Claude model', options: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'] },
    llm_effort: { label: 'Claude effort', options: ['low', 'medium', 'high', 'xhigh', 'max'] },
    llm_min_candidate_score: {
      label: 'Minimum candidate score', min: 0, max: 1, step: 0.05,
      help: 'The AI is only asked when a setup scores at least this (0 to 1).',
    },
    'risk.daily_profit_target_pct': {
      label: 'Daily profit target', unit: '% of start equity', min: 0, optional: true,
      help: 'New entries stop for the day (when that option is on) once the gain reaches this percent of start-of-day equity. Default 0 (off). At 0 or empty, the fixed dollar amount below applies instead. It never increases size.',
    },
    'risk.daily_profit_target_usd': {
      label: 'Daily profit target (fixed dollars)', unit: '$', min: 0, optional: true,
      help: 'Used when the percent above is 0 or empty. Leave both at 0 or empty for no target.',
    },
    'risk.stop_trading_at_target': { label: 'Stop new entries at the target' },
    'risk.max_daily_loss_pct': {
      label: 'Daily loss limit', unit: '% of start equity', min: 0, positive: true, help: 'Halts trading for the day.',
    },
    'risk.max_risk_per_trade_pct': {
      label: 'Risk per trade', unit: '% of equity', min: 0, positive: true, help: 'Loss if the stop is hit.',
    },
    'risk.max_position_pct': { label: 'Max position size', unit: '% of equity', min: 0, positive: true },
    'risk.max_total_exposure_pct': { label: 'Max total exposure', unit: '% of equity', min: 0, positive: true },
    'risk.max_open_positions': { label: 'Max open positions', int: true, min: 1 },
    'risk.max_trades_per_day': { label: 'Max entries per day', int: true, min: 1 },
    'risk.max_order_notional_usd': { label: 'Max order size', unit: '$', min: 0, positive: true },
    'risk.min_price': { label: 'Minimum share price', unit: '$', min: 0 },
    'risk.price_collar_pct': {
      label: 'Limit price collar', unit: '%', min: 0, positive: true,
      help: 'A limit price must be within this distance of the last price.',
    },
    'risk.min_reward_risk': { label: 'Minimum reward:risk', unit: '×', min: 0, positive: true },
    'risk.max_fee_to_reward_pct': {
      label: 'Max fees vs reward', unit: '%', min: 0, positive: true,
      help: 'Round-trip fees as a share of the expected profit.',
    },
    'risk.loss_streak_halve_after': { label: 'Halve size after', unit: 'losses in a row', int: true, min: 1 },
    'risk.llm_daily_budget_usd': {
      label: 'AI budget per day', unit: '$', min: 0, help: 'AI spend per day, Claude and Grok together. Once spent, until tomorrow: in consensus mode no new entries (the rules only manage exits); with one model, rules-only decisions.',
    },
    'risk.flatten_on_halt': { label: 'Close everything at the loss limit' },
    'risk.max_spread_pct': { label: 'Max bid/ask spread', unit: '%', min: 0, positive: true },
    'risk.halt_cooldown_minutes': { label: 'Wait after a trading halt', unit: 'minutes', int: true, min: 0 },
    'risk.allow_leveraged_etfs': {
      label: 'Allow leveraged and inverse ETFs', help: 'Not recommended for a small account.',
    },
    'risk.allow_delayed_paper_entries': {
      label: 'Paper: trade on delayed prices',
      help: 'Paper account only. Lets the bot buy on the free 15-minute-delayed prices so the order plumbing gets tested. Those trades are not evidence of an edge and are left out of the per-setup results. Live trading always needs real-time data.',
    },
    'exits.time_stop_minutes': {
      label: 'Time stop', unit: 'minutes', int: true, min: 0, max: 1440,
      help: 'Close a trade that has not reached the minimum R by then.',
    },
    'exits.time_stop_min_r': { label: 'Time stop minimum', unit: 'R', min: 0 },
    'exits.breakeven_at_r': { label: 'Move stop to entry at', unit: 'R', min: 0 },
    'exits.trail_atr_mult': { label: 'Trailing stop distance', unit: '× ATR', min: 0 },
    'exits.let_winners_run': {
      label: 'Let winners run',
      help: 'Only with the trailing stop on: new trades get no fixed target, so the trailing stop decides when a winner is sold. Stocks are still sold by 3:50 PM. Trades already open keep their target.',
    },
    'operating_costs.hosting_monthly_usd': { label: 'Monthly hosting cost', unit: '$ / month', min: 0, nullable: true },
    'operating_costs.data_monthly_usd': { label: 'Monthly market-data cost', unit: '$ / month', min: 0, nullable: true },
  };

  const fieldId = (path) => `set-${path.replace(/[^A-Za-z0-9_-]/g, '-')}`;

  function capLabel(cap, unit) {
    if (unit === '$') return money(cap);
    if (unit && unit.startsWith('%')) return `${cap}%`;
    return String(cap);
  }

  function buildField(path, value, meta, caps) {
    const id = fieldId(path);
    const label = meta.label || humanize(path.split('.').pop());
    const helpId = `${id}-help`;
    const errorId = `${id}-error`;
    const help = meta.help ? el('p', { id: helpId, class: 'help', text: meta.help }) : null;
    const error = el('p', { id: errorId, class: 'field-error' });
    const described = [help ? helpId : null, errorId];

    if (path === 'enabled_setups') {
      const enabled = new Set(asList(value));
      return el('div', { class: 'field' }, [
        el('fieldset', { id, class: 'setup-choices', 'data-path': path, 'data-kind': 'setups', 'aria-describedby': errorId }, [
          el('legend', { text: 'Trade these setups' }),
          ...Object.keys(SETUP_LABELS).map((setup) => {
            const input = el('input', { type: 'checkbox', value: setup, 'data-setup': setup });
            input.checked = enabled.has(setup);
            return el('label', { class: 'setup-choice' }, [input, el('span', { text: setupLabel(setup) })]);
          }),
        ]),
        el('p', { class: 'help', text: 'All unchecked pauses new entries from these setups. Setup-level results are available in Signals.' }),
        error,
      ]);
    }

    if (typeof value === 'boolean') {
      const input = el('input', {
        id, type: 'checkbox', class: 'switch', role: 'switch',
        'data-path': path, 'data-kind': 'bool', 'aria-describedby': described.filter(Boolean).join(' '),
      });
      input.checked = value;
      // The whole row is the label, so the tap target is the full width and >= 44 px tall.
      return el('div', { class: 'field' }, [
        el('label', { class: 'switch-row' }, [el('span', { class: 'field-label', text: label }), input]),
        help,
        error,
      ]);
    }

    let control;
    const extra = [];
    // Optional (Cursor) and nullable (Codex) number fields both accept an empty value.
    const emptyOk = Boolean(meta.optional || meta.nullable);
    const optionalNumber = (value === null || value === undefined) && emptyOk;
    if (typeof value === 'number' || optionalNumber) {
      const cap = path.startsWith('risk.') ? caps[path.slice(5)] : undefined;
      const max = Math.min(isNum(meta.max) ? meta.max : Infinity, isNum(cap) ? cap : Infinity);
      const unitId = `${id}-unit`;
      const capId = `${id}-cap`;
      if (meta.unit) described.unshift(unitId);
      if (isNum(cap)) described.push(capId);
      const input = el('input', {
        id, type: 'number', required: !emptyOk,
        inputmode: meta.int ? 'numeric' : 'decimal',
        step: meta.step ?? (meta.int ? 1 : 'any'),
        min: isNum(meta.min) ? meta.min : null,
        max: Number.isFinite(max) ? max : null,
        value: optionalNumber ? '' : String(value),
        'data-path': path, 'data-kind': meta.int ? 'int' : 'number',
        'data-nullable': meta.nullable ? 'true' : null,
        'data-positive': meta.positive ? 'true' : null,
        'data-optional': meta.optional ? 'true' : null,
        'data-cap': isNum(cap) ? String(cap) : null,
        'aria-describedby': described.filter(Boolean).join(' '),
      });
      control = el('div', { class: 'input-wrap' }, [input, meta.unit ? el('span', { id: unitId, class: 'unit', text: meta.unit }) : null]);
      if (isNum(cap)) extra.push(el('p', { id: capId, class: 'cap', text: `Hard cap: ${capLabel(cap, meta.unit)}` }));
    } else if (Array.isArray(value)) {
      control = el('input', {
        id, type: 'text', value: value.join(', '), autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false',
        'data-path': path, 'data-kind': 'list', 'aria-describedby': described.filter(Boolean).join(' '),
      });
    } else if (typeof value === 'string' && meta.options) {
      const options = meta.options.includes(value) ? meta.options : [value, ...meta.options];
      control = el('select', {
        id, 'data-path': path, 'data-kind': 'string', 'aria-describedby': described.filter(Boolean).join(' '),
      }, options.map((o) => el('option', { value: o, text: o })));
      control.value = value;
    } else if (typeof value === 'string') {
      control = el('input', {
        id, type: meta.time ? 'time' : 'text', value, required: Boolean(meta.time), step: meta.time ? 60 : null,
        autocomplete: 'off', 'data-path': path, 'data-kind': meta.time ? 'time' : 'string',
        'aria-describedby': described.filter(Boolean).join(' '),
      });
    } else {
      return null; // null or nested values this form cannot edit are kept unchanged
    }
    return el('div', { class: 'field' }, [el('label', { for: id, text: label }), control, ...extra, help, error]);
  }

  function fieldset(title, note, fields) {
    const items = fields.filter(Boolean);
    if (!items.length) return null;
    return el('fieldset', { class: 'fieldset' }, [
      el('legend', { text: title }),
      note ? el('p', { class: 'fieldset-note', text: note }) : null,
      el('div', { class: 'field-grid' }, items),
    ]);
  }

  function orderedKeys(obj, prefix) {
    const known = Object.keys(FIELDS)
      .filter((k) => k.startsWith(`${prefix}.`))
      .map((k) => k.slice(prefix.length + 1))
      .filter((k) => k in obj);
    return [...known, ...Object.keys(obj).filter((k) => !known.includes(k))];
  }

  function renderSettingsForm() {
    const payload = state.settings;
    if (!payload || !payload.settings) return;
    const settings = payload.settings;
    const caps = payload.hard_caps || {};
    const used = new Set();
    const groups = [];
    const nested = (name, title, note) => {
      used.add(name);
      const obj = settings[name];
      const keys = orderedKeys(obj, name);
      groups.push(fieldset(title, note, keys.map((k) => buildField(`${name}.${k}`, obj[k], FIELDS[`${name}.${k}`] || {}, caps))));
    };
    for (const group of SETTINGS_GROUPS) {
      if (group.object) {
        if (settings[group.object] && typeof settings[group.object] === 'object') nested(group.object, group.title, group.note);
        continue;
      }
      const keys = group.keys.filter((k) => k in settings);
      keys.forEach((k) => used.add(k));
      groups.push(fieldset(group.title, group.note, keys.map((k) => buildField(k, settings[k], FIELDS[k] || {}, caps))));
    }
    // Fields the server added after this page was written still get a generic editor.
    const rest = Object.keys(settings).filter((k) => !used.has(k));
    for (const key of rest) {
      const value = settings[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) nested(key, humanize(key), null);
    }
    groups.push(fieldset('Other', null, rest
      .filter((k) => !used.has(k))
      .map((k) => buildField(k, settings[k], FIELDS[k] || {}, caps))));
    $('settings-fields').replaceChildren(...groups.filter(Boolean));
    const note = aiNote(payload);
    $('llm-note').hidden = !note;
    setText($('llm-note'), note);
    $('settings-error').textContent = '';
    setDirty(false);
  }

  function aiNote(payload) {
    const keys = payload.ai_keys || {};
    const mode = payload.settings.decision_mode;
    if (payload.llm_available === false || (!keys.claude && !keys.grok)) {
      return 'No AI API key is configured on the server, so the bot makes rules-only decisions.';
    }
    if (mode === 'consensus' && !(keys.claude && keys.grok)) {
      const have = keys.claude ? 'Claude' : 'Grok';
      const missing = keys.claude ? 'xAI (Grok)' : 'Anthropic (Claude)';
      return `Consensus needs both API keys. No ${missing} key is configured, so ${have} decides alone.`;
    }
    if ((mode === 'claude' && !keys.claude) || (mode === 'grok' && !keys.grok)) {
      return `No ${mode === 'claude' ? 'Anthropic' : 'xAI'} API key is configured: rules-only decisions.`;
    }
    return '';
  }

  function setDirty(dirty) {
    state.settingsDirty = dirty;
    $('settings-form').dataset.dirty = String(dirty);
    setText($('settings-status'), dirty ? 'Unsaved changes' : '');
    syncButtons();
  }

  function setFieldError(input, message) {
    input.setAttribute('aria-invalid', 'true');
    const node = $(`${input.id}-error`);
    if (node) node.textContent = message;
  }

  function clearFieldErrors() {
    for (const input of $('settings-fields').querySelectorAll('[aria-invalid="true"]')) {
      input.removeAttribute('aria-invalid');
      const node = $(`${input.id}-error`);
      if (node) node.textContent = '';
    }
  }

  function setPath(obj, path, value) {
    const parts = path.split('.');
    let target = obj;
    for (const part of parts.slice(0, -1)) target = target[part];
    target[parts[parts.length - 1]] = value;
  }

  function numberError(input, value, raw) {
    const min = input.getAttribute('min');
    const max = input.getAttribute('max');
    if ((input.dataset.optional === 'true' || input.dataset.nullable === 'true') && raw === '') return null;
    if (raw === '' || !Number.isFinite(value)) return 'Enter a number.';
    if (input.dataset.kind === 'int' && !Number.isInteger(value)) return 'Enter a whole number.';
    if (input.dataset.positive && value <= 0) return 'Must be greater than 0.';
    if (min !== null && value < Number(min)) return `Must be at least ${min}.`;
    if (max !== null && value > Number(max)) {
      return input.dataset.cap && Number(max) === Number(input.dataset.cap) ? `The hard cap is ${max}.` : `Must be at most ${max}.`;
    }
    return null;
  }

  /** Read the form into a copy of the loaded settings (unknown fields are kept as loaded). */
  function collectSettings() {
    const out = structuredClone(state.settings.settings);
    const invalid = [];
    clearFieldErrors();
    for (const input of $('settings-fields').querySelectorAll('[data-path]')) {
      const kind = input.dataset.kind;
      let value;
      let error = null;
      if (kind === 'bool') {
        value = input.checked;
      } else if (kind === 'setups') {
        value = [...input.querySelectorAll('input[data-setup]:checked')].map((checkbox) => checkbox.dataset.setup);
      } else if (kind === 'list') {
        value = input.value.split(/[\s,;]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
      } else if (kind === 'int' || kind === 'number') {
        const raw = input.value.trim();
        if ((input.dataset.optional === 'true' || input.dataset.nullable === 'true') && raw === '') {
          value = null;
        } else {
          value = Number(raw);
          error = numberError(input, value, raw);
        }
      } else if (kind === 'time') {
        value = input.value.slice(0, 5);
        if (!/^\d{2}:\d{2}$/.test(value)) error = 'Enter a time as HH:MM.';
      } else {
        value = input.value;
      }
      if (error) {
        setFieldError(input, error);
        invalid.push(input);
        continue;
      }
      setPath(out, input.dataset.path, value);
    }
    if (invalid.length) {
      invalid[0].focus();
      return null;
    }
    return out;
  }

  function showServerErrors(err) {
    const detail = err.detail;
    let items = [];
    if (Array.isArray(detail)) items = detail.map((d) => [locPath(d && d.loc), (d && d.msg) || 'invalid value']);
    else if (typeof detail === 'string') items = [[(/^([a-z_]+(?:\.[a-z_]+)?)\b/.exec(detail) || [])[1] || '', detail]];
    let first = null;
    for (const [path, message] of items) {
      const input = path ? $('settings-fields').querySelector(`[data-path="${CSS.escape(path)}"]`) : null;
      if (!input) continue;
      setFieldError(input, sentence(message));
      first = first || input;
    }
    $('settings-error').textContent = `Not saved: ${err.message}`;
    if (first) first.focus();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!state.settings || !state.settingsDirty) return;
    $('settings-error').textContent = '';
    const settings = collectSettings();
    if (!settings) {
      setText($('settings-status'), 'Fix the highlighted fields.');
      return;
    }
    await busy($('settings-save'), async () => {
      try {
        let payload;
        try {
          payload = await api('PUT', '/api/settings', { settings });
        } catch (err) {
          if (!isStepUp(err)) throw err;
          payload = await stepUp({
            title: 'Confirm risk change',
            message: 'These changes loosen a risk limit (or turn on autopilot for a live account).',
            confirm: 'Save settings',
          }, (totp) => api('PUT', '/api/settings', { settings, totp }));
          if (payload === null) {
            setText($('settings-status'), 'Not saved: these changes need a 2FA code.');
            return;
          }
        }
        if (!payload || !payload.settings) throw new ApiError(200, 'Unexpected response from the server.');
        state.settings = payload;
        renderSettingsForm();
        renderStatus();
        toast('Settings saved.');
        loadAudit();
      } catch (err) {
        if (err instanceof ApiError && err.status === 422) showServerErrors(err);
        else if (!err.fatal) $('settings-error').textContent = `Not saved: ${err.message}`;
      }
    });
  }

  async function loadSettings() {
    try {
      const payload = await api('GET', '/api/settings');
      if (!payload || !payload.settings) throw new ApiError(200, 'Unexpected response from the server.');
      state.settings = payload;
      if (!state.settingsDirty) renderSettingsForm();
      renderStatus();
    } catch (err) {
      if (!err.fatal) $('settings-error').textContent = `Could not load settings: ${err.message}`;
    }
  }

  function loadAudit() {
    return loadInto($('audit-list'), '/api/audit?limit=50', renderAudit);
  }

  function auditDetail(detail) {
    if (!detail || typeof detail !== 'object' || !Object.keys(detail).length) return null;
    const text = JSON.stringify(detail);
    return el('p', { class: 'audit-detail', text: text.length > 400 ? `${text.slice(0, 400)}…` : text });
  }

  function renderAudit(data) {
    const rows = asList(data);
    $('audit-list').replaceChildren(rows.length
      ? el('ul', { class: 'entries' }, rows.map((r) => el('li', { class: 'entry' }, [
        el('div', { class: 'entry-head' }, [
          el('span', { class: 'entry-title', text: humanize(r.action || 'event') }),
          /(_failed|_denied|_rate_limited)$/.test(r.action || '') ? chipNode('Failed', 'bad') : null,
          el('span', { class: 'entry-meta', text: `${when(r.ts)} · ${r.actor || 'unknown'}${r.ip ? ` · ${r.ip}` : ''}` }),
        ]),
        auditDetail(r.detail),
      ])))
      : empty('No audit entries yet.'));
  }

  function loadSettingsView() {
    renderLiveCard();
    return Promise.all([state.settingsDirty ? null : loadSettings(), loadAudit(), loadPush()]);
  }

  // ------------------------------------------------------------------ phone alerts
  // Web Push: the browser subscribes at its push service with the server's public key; the
  // server keeps the subscription and sends each alert there (trader/webpush.py). The
  // service worker shows it. Devices are named by a hash of their endpoint, as on the server.
  const PUSH_DEVICE_KEY = 'trader-push-device';
  // Set while this device swaps its subscription for one with the server's new key, so a
  // failed subscribe() between the two steps is retried on the next start.
  const PUSH_PENDING_KEY = 'trader-push-resubscribe';
  const pushSupported = () => window.isSecureContext && 'serviceWorker' in navigator
    && 'PushManager' in window && 'Notification' in window;

  function keyBytes(base64url) {
    const text = atob(base64url.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (base64url.length % 4)) % 4));
    return Uint8Array.from(text, (ch) => ch.charCodeAt(0));
  }

  /** True when `sub` was made with the server's current key (`key`, bytes). */
  function sameKey(sub, key) {
    const current = sub && sub.options && sub.options.applicationServerKey
      ? new Uint8Array(sub.options.applicationServerKey) : null;
    return Boolean(current) && current.length === key.length && current.every((b, i) => b === key[i]);
  }

  async function deviceIdOf(endpoint) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  }

  async function pushSubscription() {
    if (!pushSupported()) return null;
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      return reg ? await reg.pushManager.getSubscription() : null;
    } catch {
      return null;
    }
  }

  function remember(deviceId) {
    try {
      if (deviceId) localStorage.setItem(PUSH_DEVICE_KEY, deviceId);
      else localStorage.removeItem(PUSH_DEVICE_KEY);
      localStorage.removeItem(PUSH_PENDING_KEY);
    } catch { /* storage is optional */ }
  }

  async function loadPush() {
    let info;
    try {
      info = await api('GET', '/api/push');
    } catch (err) {
      if (!err.fatal) setText($('push-status'), `Could not load phone alerts: ${err.message}`);
      return;
    }
    const sub = await pushSubscription();
    const myId = sub ? await deviceIdOf(sub.endpoint) : null;
    renderPush(info, myId);
  }

  function renderPush(info, myId) {
    state.push = info;
    const devices = asList(info.devices);
    const mine = Boolean(myId) && devices.some((d) => d.id === myId);
    const blocked = pushSupported() && Notification.permission === 'denied';
    let status;
    if (!pushSupported()) {
      status = 'This browser cannot show app alerts. On Android, use Chrome (installing the app to the home screen works best).';
    } else if (!info.available) {
      status = info.problem || 'Phone alerts are not available on this server.';
    } else if (mine) {
      status = 'Alerts are on for this device.';
    } else if (blocked) {
      status = 'Alerts are blocked for this site. Allow notifications in the browser’s site settings, then reload.';
    } else {
      status = 'Alerts are off on this device.';
    }
    setText($('push-status'), status);
    $('push-enable').hidden = !(pushSupported() && info.available && !mine && !blocked);
    $('push-disable').hidden = !mine;
    $('push-reset').hidden = !info.key_mismatch;
    $('push-test').hidden = !(info.available && devices.length);
    $('push-test').dataset.device = mine ? myId : '';
    setText($('push-test'), mine || devices.length === 0 ? 'Send a test alert' : 'Send a test alert to all devices');
    $('push-devices').replaceChildren(devices.length ? table([
      { label: 'Device', head: true, value: (d) => (d.id === myId ? [d.label || 'Device', ' ', chipNode('This device', 'ok')] : d.label || 'Device') },
      { label: 'Added', value: (d) => when(d.created_at) },
      { label: 'Last alert', value: (d) => (d.last_ok_at ? when(d.last_ok_at) : d.failures ? `${d.failures} failed` : 'none yet') },
      {
        label: '',
        value: (d) => {
          const button = el('button', { class: 'btn btn-sm', type: 'button', text: 'Remove' });
          button.setAttribute('aria-label', `Remove ${d.label || 'device'}`);
          button.addEventListener('click', () => busy(button, () => removePushDevice(d.id, d.id === myId)));
          return button;
        },
      },
    ], devices) : []);
    const note = $('push-note');
    note.hidden = !info.ntfy;
    setText(note, info.ntfy
      ? 'ntfy alerts are on too (TRADER_NTFY_URL in .env). Remove that line and restart if you only want app alerts.'
      : '');
  }

  async function enablePush() {
    try {
      const permission = await Notification.requestPermission(); // first: needs the tap
      if (permission !== 'granted') {
        toast(permission === 'denied'
          ? 'Alerts are blocked for this site. Allow notifications in the browser’s site settings.'
          : 'Alerts were not allowed.', 'error');
        return;
      }
      await loadPush(); // the server's current key, not the one this card last rendered
      if (!state.push || !state.push.public_key) return;
      const key = keyBytes(state.push.public_key);
      const reg = (await navigator.serviceWorker.getRegistration()) || (await navigator.serviceWorker.ready);
      for (let attempt = 0; ; attempt += 1) {
        let sub = await reg.pushManager.getSubscription();
        if (sub && !sameKey(sub, key)) {
          await sub.unsubscribe(); // made with another key: the server cannot use it
          sub = null;
        }
        if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
        const json = sub.toJSON();
        const info = await api('POST', '/api/push/subscribe', { endpoint: json.endpoint, keys: json.keys });
        remember(info.device);
        renderPush(info, info.device);
        const test = await api('POST', '/api/push/test', { device: info.device });
        const kept = asList(test.devices).some((d) => d.id === info.device);
        if (!test.sent && !kept && attempt === 0) {
          // The push service no longer knows this subscription (the server dropped it):
          // start over once with a fresh one.
          await sub.unsubscribe().catch(() => undefined);
          continue;
        }
        if (!kept) remember(null);
        toast(test.sent
          ? 'Alerts are on. A test alert is on its way.'
          : kept
            ? 'Alerts are on, but the test alert was not delivered. Try “Send a test alert”.'
            : 'The push service refused this device. Try again later.', test.sent ? 'info' : 'error');
        renderPush(test, kept ? info.device : null);
        return;
      }
    } catch (err) {
      report(err, 'Could not turn on alerts: ');
    }
  }

  async function removePushDevice(deviceId, isThisDevice) {
    try {
      const info = await api('DELETE', `/api/push/devices/${deviceId}`);
      if (isThisDevice) {
        const sub = await pushSubscription();
        if (sub) await sub.unsubscribe().catch(() => undefined);
        remember(null);
      }
      const sub = isThisDevice ? null : await pushSubscription();
      renderPush(info, sub ? await deviceIdOf(sub.endpoint) : null);
    } catch (err) {
      report(err, 'Could not remove the device: ');
    }
  }

  async function disablePush() {
    const sub = await pushSubscription();
    if (!sub) return loadPush();
    return removePushDevice(await deviceIdOf(sub.endpoint), true);
  }

  async function testPush() {
    try {
      const device = $('push-test').dataset.device || null;
      const info = await api('POST', '/api/push/test', device ? { device } : {});
      toast(info.sent ? 'Test alert sent.' : 'The test alert was not delivered. Check the devices list.', info.sent ? 'info' : 'error');
      const sub = await pushSubscription();
      renderPush(info, sub ? await deviceIdOf(sub.endpoint) : null);
    } catch (err) {
      report(err, 'Could not send a test alert: ');
    }
  }

  async function resetPush() {
    try {
      const info = await api('POST', '/api/push/reset');
      const sub = await pushSubscription();
      if (sub) await sub.unsubscribe().catch(() => undefined);
      remember(null);
      renderPush(info, null);
      toast('Phone alerts were reset. Turn them on again on each device.');
    } catch (err) {
      report(err, 'Could not reset phone alerts: ');
    }
  }

  /** On start, keep this device in step with the server.
   * - After a reset (new server key) a subscription made with the old key is dropped, so its
   *   endpoint dies at the push service and the old key cannot reach this device. If this
   *   device was subscribed at the reset, it subscribes again with the new key.
   * - A browser can renew its subscription (new endpoint): hand the new one over.
   * - A device removed on the server (from another device, the CLI, or by the push service)
   *   remembers that it is off and stays off. */
  async function syncPush() {
    if (!pushSupported()) return;
    let stored = null;
    let pending = false;
    try {
      stored = localStorage.getItem(PUSH_DEVICE_KEY);
      pending = localStorage.getItem(PUSH_PENDING_KEY) === '1';
    } catch { /* storage is optional */ }
    const wanted = Boolean(stored) && Notification.permission === 'granted';
    let sub = await pushSubscription();
    if (!sub && !(pending && wanted)) {
      if (stored || pending) remember(null);
      return;
    }
    const refresh = (info, id) => { if (state.view === 'settings') renderPush(info, id); };
    try {
      const info = await api('GET', '/api/push', undefined, { background: true });
      if (!info.available || !info.public_key) return;
      const devices = asList(info.devices);
      const key = keyBytes(info.public_key);
      if (sub && sameKey(sub, key)) {
        if (!wanted) return;
        const id = await deviceIdOf(sub.endpoint);
        const listed = devices.some((d) => d.id === stored);
        if (id === stored || (!listed && !pending)) {
          if (!listed) {
            remember(null);
            refresh(info, null);
          }
          return;
        }
        // Same key, new endpoint: the browser renewed the subscription, or a swap to a new
        // key stopped before the server heard of it (pending). Register it now.
      } else {
        if (!(wanted && asList(info.resubscribe).includes(stored))) {
          if (sub) await sub.unsubscribe().catch(() => undefined);
          remember(null);
          refresh(info, null);
          return;
        }
        try { localStorage.setItem(PUSH_PENDING_KEY, '1'); } catch { /* best effort */ }
        if (sub) await sub.unsubscribe().catch(() => undefined);
        const reg = await navigator.serviceWorker.getRegistration();
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      }
      const json = sub.toJSON();
      let latest = await api('POST', '/api/push/subscribe', { endpoint: json.endpoint, keys: json.keys });
      remember(latest.device);
      if (stored !== latest.device && devices.some((d) => d.id === stored)) {
        latest = await api('DELETE', `/api/push/devices/${stored}`);
      }
      refresh(latest, latest.device);
    } catch { /* retried on the next start */ }
  }

  async function logout() {
    await busy($('logout-btn'), async () => {
      try {
        await api('POST', '/api/auth/logout');
        state.redirecting = true;
        state.settingsDirty = false;
        window.location.replace('/login');
      } catch (err) {
        report(err, 'Could not sign out: ');
      }
    });
  }

  // ------------------------------------------------------------------ navigation
  function viewFromHash() {
    const name = window.location.hash.replace('#', '');
    return VIEWS.includes(name) ? name : 'dashboard';
  }

  function showView(name) {
    const switched = state.view !== name;
    state.view = name;
    for (const view of VIEWS) {
      const active = view === name;
      const tab = $(`tab-${view}`);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      $(`view-${view}`).hidden = !active;
    }
    if (!switched) return;
    if (name !== 'signals') clearTimeout(state.researchTimer);
    window.scrollTo(0, 0);
    if (name === 'dashboard') {
      loadPerformance();
      loadDashboardCharts();
    }
    else if (name === 'activity') loadActivity();
    else if (name === 'signals') loadSignals();
    else if (name === 'settings') loadSettingsView();
  }

  function selectView(name) {
    if (window.location.hash !== `#${name}`) window.location.hash = name; // hashchange shows it
    else showView(name);
  }

  function onTabKey(event) {
    const index = VIEWS.indexOf(state.view);
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: VIEWS.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = VIEWS[(moves[event.key] + VIEWS.length) % VIEWS.length];
    selectView(next);
    $(`tab-${next}`).focus();
  }

  // ------------------------------------------------------------------ wiring
  function registerServiceWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register('/static/sw.js', { scope: '/' }).catch(() => {
      /* offline support is optional; the app works without it */
    });
  }

  function wire() {
    for (const view of VIEWS) $(`tab-${view}`).addEventListener('click', () => selectView(view));
    $('tablist').addEventListener('keydown', onTabKey);
    window.addEventListener('hashchange', () => showView(viewFromHash()));

    $('kill-btn').addEventListener('click', openKill);
    $('kill-top').addEventListener('click', openKill);
    $('kill-form').addEventListener('submit', confirmKill);
    $('kill-cancel').addEventListener('click', () => $('kill-dialog').close());
    $('kill-dialog').addEventListener('cancel', (event) => {
      if (isBusy($('kill-confirm'))) event.preventDefault(); // let a running kill finish
    });
    $('close-form').addEventListener('submit', confirmClose);
    $('close-cancel').addEventListener('click', () => {
      if (!isBusy($('close-confirm'))) $('close-dialog').close(); // let a running close finish
    });
    $('close-dialog').addEventListener('cancel', (event) => {
      if (isBusy($('close-confirm'))) event.preventDefault(); // let a running close finish
    });
    $('close-dialog').addEventListener('close', () => {
      // The button that opened the dialog may have been rebuilt: return focus to its row.
      const active = document.activeElement;
      if (state.closing && (!active || active === document.body || $('close-dialog').contains(active))) {
        focusCloseButton(state.closing.key);
      }
    });
    $('startstop-btn').addEventListener('click', toggleBot);
    $('cycle-btn').addEventListener('click', runCycle);
    $('resume-btn').addEventListener('click', () => resume($('resume-btn')));
    $('halt-resume').addEventListener('click', () => resume($('halt-resume')));
    $('reload-btn').addEventListener('click', () => window.location.reload());
    $('toast').addEventListener('click', () => {
      $('toast').textContent = '';
    });

    $('activity-refresh').addEventListener('click', () => busy($('activity-refresh'), loadActivity));
    $('signals-refresh').addEventListener('click', () => busy($('signals-refresh'), loadSignals));
    $('stats-days').addEventListener('change', loadSignalPeriod);
    $('signals-setup').addEventListener('change', loadSignalList);
    $('performance-days').addEventListener('change', () => {
      loadPerformance();
      loadDashboardCharts();
    });
    $('research-run').addEventListener('click', runResearch);

    const form = $('settings-form');
    form.addEventListener('submit', saveSettings);
    form.addEventListener('input', () => setDirty(true));
    form.addEventListener('change', () => setDirty(true));
    $('settings-discard').addEventListener('click', renderSettingsForm);
    $('arm-btn').addEventListener('click', armLive);
    $('disarm-btn').addEventListener('click', disarmLive);
    $('audit-refresh').addEventListener('click', () => busy($('audit-refresh'), loadAudit));
    $('logout-btn').addEventListener('click', logout);
    $('push-enable').addEventListener('click', () => busy($('push-enable'), enablePush));
    $('push-disable').addEventListener('click', () => busy($('push-disable'), disablePush));
    $('push-test').addEventListener('click', () => busy($('push-test'), testPush));
    $('push-reset').addEventListener('click', () => busy($('push-reset'), resetPush));

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
        clearTimeout(state.researchTimer);
      } else {
        refreshStatus();
        if (state.view === 'signals' && state.researchRunning) loadResearch();
      }
    });
    window.addEventListener('beforeunload', (event) => {
      if (state.settingsDirty) event.preventDefault();
    });
    new ResizeObserver(() => {
      const width = Math.floor($('equity-chart').clientWidth);
      if (state.view === 'activity' && state.equity && width && Math.abs(width - state.chartWidth) > 4) drawEquity({ keepPlace: true });
    }).observe($('equity-chart'));
    new ResizeObserver(() => {
      const width = Math.floor($('dash-equity-chart').clientWidth);
      if (state.view === 'dashboard' && state.dashDays && width && Math.abs(width - state.dashChartsWidth) > 4) {
        drawDashboardCharts({ keepPlace: true });
      }
    }).observe($('dash-equity-chart'));
  }

  async function start() {
    registerServiceWorker();
    wire();
    showView(viewFromHash());
    try {
      await loadSession();
    } catch (err) {
      if (err.fatal) return;
      setStale(err.message); // offline: polling keeps retrying and fetches the token later
    }
    refreshStatus();
    if (state.view !== 'settings') loadSettings(); // the settings view already loaded them
    syncPush();
  }

  start();
})();
