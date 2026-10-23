"""The settings page must show empty profit-target fields so they can be edited.

app.js has no build step. These checks run the form helpers under a small DOM stand-in
in Node, which is what the browser page uses for render and collect.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from trader.models import BotSettings

APP_JS = Path(__file__).parent.parent / "trader" / "web" / "static" / "app.js"

# Enough of a DOM for renderSettingsForm and collectSettings. Not a browser.
_DOM = r"""
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this._text = '';
    this.id = '';
    this.disabled = false;
    this.hidden = false;
    this.checked = false;
    this._value = '';
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text; }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
    if (key === 'id') {
      this.id = String(value);
      byId[this.id] = this;
    }
    if (key === 'value') this._value = String(value);
  }
  getAttribute(key) {
    return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null;
  }
  removeAttribute(key) { delete this.attrs[key]; }
  get dataset() {
    const self = this;
    const attr = (prop) => 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
    return new Proxy({}, {
      get(_t, prop) { return self.attrs[attr(prop)]; },
      set(_t, prop, value) { self.attrs[attr(prop)] = String(value); return true; },
      deleteProperty(_t, prop) { delete self.attrs[attr(prop)]; return true; },
    });
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  append(child) {
    if (child && typeof child === 'object') {
      child.parent = this;
      this.children.push(child);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const node of nodes) if (node) this.append(node);
  }
  querySelectorAll(sel) { return this._match(sel); }
  querySelector(sel) { const found = this._match(sel); return found.length ? found[0] : null; }
  _match(sel) {
    const out = [];
    const walk = (node) => {
      if (node !== this && matches(node, sel)) out.push(node);
      for (const child of node.children || []) walk(child);
    };
    walk(this);
    return out;
  }
  focus() {}
}

function matches(node, sel) {
  const found = /^\[([A-Za-z0-9_-]+)(?:="([^"]*)")?\]$/.exec(sel);
  if (!found) return false;
  const value = node.attrs[found[1]];
  if (value === undefined) return false;
  return found[2] === undefined || value === found[2];
}

const byId = {};
globalThis.document = {
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return new El(tag); },
  createElementNS(_ns, tag) { return new El(tag); },
};
for (const id of [
  'settings-fields', 'llm-note', 'settings-error', 'settings-form', 'settings-status',
  'startstop-btn', 'arm-btn', 'disarm-btn', 'settings-save', 'settings-discard',
]) {
  const node = document.createElement('div');
  node.setAttribute('id', id);
}
"""


def _form_script() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    needle = "\n  start();\n"
    assert source.count(needle) == 1
    source = source.replace(
        needle,
        "\n  globalThis.__settingsForm = { state, renderSettingsForm, collectSettings };\n",
        1,
    )
    return _DOM + source


def _run_form(settings: dict) -> dict:
    script = (
        _form_script()
        + """
const payload = JSON.parse(process.argv[1]);
const api = globalThis.__settingsForm;
api.state.settings = {
  settings: payload.settings,
  hard_caps: payload.hard_caps || {},
  ai_keys: {},
};
api.renderSettingsForm();
const fields = document.getElementById('settings-fields');
const read = (path) => fields.querySelector('[data-path="' + path + '"]');
const pct = read('risk.daily_profit_target_pct');
const usd = read('risk.daily_profit_target_usd');
if (!pct || !usd) {
  console.log(JSON.stringify({
    error: 'missing profit fields', pct: Boolean(pct), usd: Boolean(usd),
  }));
  process.exit(1);
}
const shown = {
  pct: pct.value,
  usd: usd.value,
  pctOptional: pct.dataset.optional,
  usdOptional: usd.dataset.optional,
};
pct.value = '';
usd.value = '80';
const collected = api.collectSettings();
console.log(JSON.stringify({
  shown,
  collectedPct: collected.risk.daily_profit_target_pct,
  collectedUsd: collected.risk.daily_profit_target_usd,
}));
"""
    )
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", script, json.dumps({"settings": settings})],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_empty_profit_target_fields_are_editable() -> None:
    fresh = BotSettings().model_dump(mode="json")
    shown = _run_form(fresh)
    assert shown["shown"]["pct"] == "1.5"
    assert shown["shown"]["usd"] == ""
    assert shown["shown"]["pctOptional"] == "true"
    assert shown["shown"]["usdOptional"] == "true"
    assert shown["collectedPct"] is None
    assert shown["collectedUsd"] == 80

    legacy = BotSettings().model_dump(mode="json")
    legacy["risk"]["daily_profit_target_pct"] = None
    legacy["risk"]["daily_profit_target_usd"] = 100
    legacy_shown = _run_form(legacy)
    assert legacy_shown["shown"]["pct"] == ""
    assert legacy_shown["shown"]["usd"] == "100"
    assert legacy_shown["collectedPct"] is None
    assert legacy_shown["collectedUsd"] == 80
