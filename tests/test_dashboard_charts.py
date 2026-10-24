"""Dashboard chart helpers in app.js (no build step), run under Node with the DOM stand-in
from the settings form test."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.test_settings_form import _DOM, APP_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(script: str) -> dict:
    source = APP_JS.read_text(encoding="utf-8")
    needle = "\n  start();\n"
    assert source.count(needle) == 1
    source = source.replace(
        needle, "\n  globalThis.__charts = { dashDays, tickLabel, barPath, nyDate };\n", 1
    )
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603
        [node, "--input-type=module", "-e", _DOM + source + script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_daily_rows_keep_trading_days_in_the_period_and_compute_the_day_pnl() -> None:
    out = _run(
        """
const c = globalThis.__charts;
const today = new Date(`${c.nyDate.format(new Date())}T12:00:00Z`);
const ago = (n) => new Date(today.getTime() - n * 86400000).toISOString().slice(0, 10);
const weekday = (d) => {
  const g = new Date(`${d}T12:00:00Z`).getUTCDay();
  return g >= 1 && g <= 5;
};
// Find a recent weekend day (quiet) and a weekday inside the last 7 days.
let quietWeekend = null; let weekdayDay = null;
for (let n = 0; n < 7; n += 1) {
  const d = ago(n);
  if (!weekday(d) && !quietWeekend) quietWeekend = d;
  if (weekday(d) && !weekdayDay) weekdayDay = d;
}
const rows = [
  { day: weekdayDay, start_equity: 600, end_equity: 607.5, llm_cost: 0.2, trades: 1 },
  { day: quietWeekend, start_equity: 607.5, end_equity: 607.5, llm_cost: 0, trades: 0 },
  { day: ago(20), start_equity: 590, end_equity: 600, llm_cost: 0.1, trades: 2 },
  { day: ago(2), start_equity: null, end_equity: 600, llm_cost: 0.05, trades: 0 },
];
const seven = c.dashDays(rows, 7);
const thirty = c.dashDays(rows, 30);
console.log(JSON.stringify({
  seven: seven.map((d) => [d.day, d.pnl, d.cost]),
  thirtyDays: thirty.map((d) => d.day),
  weekdayDay, quietWeekend, old: ago(20), two: ago(2),
  sorted: thirty.every((d, i) => i === 0 || thirty[i - 1].day < d.day),
}));
"""
    )
    seven = {day: (pnl, cost) for day, pnl, cost in out["seven"]}
    assert seven[out["weekdayDay"]] == (7.5, 0.2)
    assert out["quietWeekend"] not in seven  # a quiet weekend day is not a bar
    assert out["old"] not in seven and out["old"] in out["thirtyDays"]  # outside 7, inside 30
    if out["two"] in seven:  # a day with no start equity has no P&L, but its cost counts
        assert seven[out["two"]] == (None, 0.05)
    assert out["sorted"]


def test_axis_labels_and_bar_shapes() -> None:
    out = _run(
        """
const c = globalThis.__charts;
console.log(JSON.stringify({
  neg: c.tickLabel(-20, 10), pos: c.tickLabel(1500, 500), cents: c.tickLabel(0.2, 0.1),
  up: c.barPath(10, 12, 100, 60), down: c.barPath(10, 12, 100, 140),
}));
"""
    )
    assert out["neg"] == "-$20" and out["pos"] == "$1,500" and out["cents"] == "$0.20"
    # Up bars: square at the zero line (100), rounded at the top (60); down bars mirror it.
    assert out["up"].startswith("M10,100V64Q10,60 14,60") and out["up"].endswith("V100Z")
    assert out["down"].startswith("M10,100V136Q10,140 14,140") and out["down"].endswith("V100Z")
