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
        needle, "\n  globalThis.__charts = { dashDays, tickLabel, barPath, nyDate, gutter };\n", 1
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
    # Today is Tuesday 2026-10-27 (New York): the 7-day period starts Wednesday 10-21.
    out = _run(
        """
const c = globalThis.__charts;
const rows = [
  { day: '2026-10-26', start_equity: 600, end_equity: 607.5, llm_cost: 0.2, trades: 1 },
  { day: '2026-10-25', start_equity: 607.5, end_equity: 607.5, llm_cost: 0, trades: 0 },
  { day: '2026-10-24', start_equity: null, end_equity: 600, llm_cost: 0.05, trades: 0 },
  { day: '2026-10-20', start_equity: 590, end_equity: 600, llm_cost: 0.1, trades: 2 },
  { day: '2026-10-07', start_equity: 580, end_equity: 590, llm_cost: 0.1, trades: 2 },
  { day: '2026-10-21', start_equity: 600, end_equity: 595, llm_cost: 0.1, trades: 1 },
];
const seven = c.dashDays(rows, 7, '2026-10-27');
const thirty = c.dashDays(rows, 30, '2026-10-27');
console.log(JSON.stringify({
  seven: seven.map((d) => [d.day, d.pnl, d.cost]),
  thirty: thirty.map((d) => d.day),
}));
"""
    )
    assert out["seven"] == [
        ["2026-10-21", -5, 0.1],  # first day of the period
        ["2026-10-24", None, 0.05],  # Saturday with AI cost: kept; no start equity, no P&L
        ["2026-10-26", 7.5, 0.2],
    ]  # Sunday 10-25 had no activity: not a bar; 10-20 is 8 days back
    assert out["thirty"] == ["2026-10-07", "2026-10-20", "2026-10-21", "2026-10-24", "2026-10-26"]


def test_axis_labels_and_bar_shapes() -> None:
    out = _run(
        """
const c = globalThis.__charts;
console.log(JSON.stringify({
  neg: c.tickLabel(-20, 10), pos: c.tickLabel(1500, 500), cents: c.tickLabel(0.2, 0.1),
  quarter: [-5, -2.5, 0, 2.5].map((v) => c.tickLabel(v, 2.5)),
  fine: [0, 0.025, 0.05, 0.075].map((v) => c.tickLabel(v, 0.025)),
  drift: c.tickLabel(-1e-12, 0.1),
  gutters: [c.gutter(['$480', '$510'], 64), c.gutter(['$1,000,002.50', '$999,997.50'], 64)],
  up: c.barPath(10, 12, 100, 60), down: c.barPath(10, 12, 100, 140),
}));
"""
    )
    assert out["neg"] == "-$20" and out["pos"] == "$1,500" and out["cents"] == "$0.20"
    assert out["quarter"] == ["-$5.00", "-$2.50", "$0.00", "$2.50"]  # not -$2 / $3
    assert out["fine"] == ["$0.000", "$0.025", "$0.050", "$0.075"]
    assert out["drift"] == "$0.00"  # never "-$0.00"
    assert out["gutters"] == [64, 105]  # a $1M account's $2.50 labels get a wider gutter
    # Up bars: square at the zero line (100), rounded at the top (60); down bars mirror it.
    assert out["up"].startswith("M10,100V64Q10,60 14,60") and out["up"].endswith("V100Z")
    assert out["down"].startswith("M10,100V136Q10,140 14,140") and out["down"].endswith("V100Z")
