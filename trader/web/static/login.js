/* AI Trader sign-in page: password + TOTP code -> POST /api/auth/login -> "/". */
(() => {
  'use strict';

  const TIMEOUT_MS = 15000;
  const $ = (id) => document.getElementById(id);
  const form = $('login-form');
  const password = $('password');
  const code = $('code');
  const error = $('login-error');
  const submit = $('login-submit');

  function showError(message, focus) {
    error.textContent = message;
    if (focus) focus.focus();
  }

  function setBusy(isBusy) {
    submit.disabled = isBusy;
    submit.textContent = isBusy ? 'Signing in…' : 'Sign in';
    if (isBusy) submit.setAttribute('aria-busy', 'true');
    else submit.removeAttribute('aria-busy');
  }

  async function request(method, path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      return await fetch(path, {
        method,
        headers: body === undefined
          ? { Accept: 'application/json' }
          : { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'manual',
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  const isJson = (res) => (res.headers.get('content-type') || '').includes('application/json');

  /** A proxy (Cloudflare Access) answered instead of the app: its session ran out. */
  const proxyBlocked = (res) => res.type === 'opaqueredirect' || (res.status === 403 && !isJson(res));

  async function alreadySignedIn() {
    try {
      const res = await request('GET', '/api/session');
      if (proxyBlocked(res)) {
        showError('Session expired - reload to sign in again');
        return;
      }
      if (!res.ok || !isJson(res)) return;
      const data = await res.json();
      if (data && data.authenticated) window.location.replace('/');
    } catch {
      /* offline or slow: the form still works once the network is back */
    }
  }

  async function signIn(event) {
    event.preventDefault();
    if (submit.disabled) return;
    error.textContent = '';
    const totp = code.value.replace(/\D/g, '');
    if (!password.value) {
      showError('Enter your password.', password);
      return;
    }
    if (totp.length !== 6) {
      showError('Enter the 6-digit code from your authenticator app.', code);
      return;
    }
    setBusy(true);
    try {
      const res = await request('POST', '/api/auth/login', { password: password.value, totp });
      if (res.ok) {
        password.value = '';
        await res.text(); // finish reading the response before leaving the page
        window.location.replace('/');
        return;
      }
      code.value = '';
      if (proxyBlocked(res)) showError('Session expired - reload to sign in again');
      else if (res.status === 401) showError('Incorrect password or code.', code);
      else if (res.status === 429) showError('Too many failed attempts. Wait 15 minutes, then try again.');
      else showError(`Sign-in failed (HTTP ${res.status}). Try again.`, code);
    } catch (err) {
      const timedOut = err && err.name === 'AbortError';
      showError(timedOut ? 'The server did not answer in time. Try again.' : "Can't reach the server. Check your connection.");
    } finally {
      setBusy(false);
    }
  }

  code.addEventListener('input', () => {
    const digits = code.value.replace(/\D/g, '').slice(0, 6);
    if (digits !== code.value) code.value = digits;
  });
  form.addEventListener('submit', signIn);

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/static/sw.js', { scope: '/' }).catch(() => {
      /* offline support is optional */
    });
  }
  alreadySignedIn();
})();
