"""thin_curve: the equity chart's server-side thinning."""

from __future__ import annotations

from trader.db import thin_curve


def test_thin_curve_keeps_the_span_the_extremes_and_the_last_point() -> None:
    points = [{"ts": f"t{i:05d}", "equity": float(i % 10)} for i in range(1000)]
    points[503]["equity"] = -5.0
    thin = thin_curve(points, 50)
    assert len(thin) <= 101
    assert thin[0]["ts"] <= points[19]["ts"]  # starts in the first bucket
    assert thin[-1] is points[-1]
    assert any(p["equity"] == -5.0 for p in thin)  # a dip survives
    assert [p["ts"] for p in thin] == sorted(p["ts"] for p in thin)
    assert thin_curve(points[:80], 50) == points[:80]  # small curves pass through


def test_thin_curve_keeps_the_first_point() -> None:
    """Review finding: thinning kept each run's low and high but not the period's first
    point, so the chart's "change since" was measured from a run extreme (wrong sign)."""
    points = [{"ts": f"t{i:05d}", "equity": e} for i, e in enumerate([5.0, 9.0, 1.0] + [3.0] * 997)]
    thin = thin_curve(points, 50)
    assert thin[0] is points[0] and thin[-1] is points[-1]
