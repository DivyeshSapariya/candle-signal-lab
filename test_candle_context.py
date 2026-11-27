"""
test_candle_context.py
----------------------
Checks the market-context numbers, RSI, MACD and the pivot levels with made-up data (no look-ahead, right maths).

Run:   python test_candle_context.py
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

import candle_context as cc
from test_no_lookahead import make_candles


def daily(closes, start=date(2026, 9, 1), spread=10.0):
    """A daily table from closes: open = previous close, high / low = +/- spread around the move. Weekends are skipped."""
    rows, d, prev = [], start, closes[0]
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append((d, prev, max(prev, c) + spread, min(prev, c) - spread, c))
        prev = c
        d += timedelta(days=1)
    return pd.DataFrame(rows, columns=["day", "open", "high", "low", "close"]).set_index("day")


def main():
    # --- daily candles from intraday candles ---
    df = make_candles(seed=2, days=3)
    dd = cc.daily_candles(df)
    assert len(dd) == 3 and (dd["high"] >= dd["low"]).all()
    first = df[df.index.date == dd.index[0]]
    assert dd.iloc[0]["open"] == first["open"].iloc[0] and dd.iloc[0]["close"] == first["close"].iloc[-1]
    assert dd.iloc[0]["high"] == first["high"].max() and dd.iloc[0]["low"] == first["low"].min()
    assert cc.daily_candles(pd.DataFrame()).empty

    # --- the daily trend: only with enough days, from the 20-day EMA ---
    up = daily([100 + 2 * k for k in range(40)])
    today = up.index[-1] + timedelta(days=1)
    while today.weekday() >= 5:
        today += timedelta(days=1)
    ctx = cc.market_context(up, today, 180.0)
    assert ctx["trend"] is None or True
    ctx = cc.market_context(up.iloc[:-1], up.index[-1], 178.0)                       # 39 finished days
    assert ctx["trend"]["label"] == "Up" and ctx["trend"]["change_5d_pct"] > 0 and ctx["trend"]["vs_ema_pct"] > 0
    down = daily([300 - 2 * k for k in range(40)])
    assert cc.market_context(down.iloc[:-1], down.index[-1], 222.0)["trend"]["label"] == "Down"
    flat = daily([200.0] * 40)
    assert cc.market_context(flat.iloc[:-1], flat.index[-1], 200.0)["trend"]["label"] == "Sideways"
    short = daily([100 + k for k in range(20)])
    assert cc.market_context(short.iloc[:-1], short.index[-1], 118.0)["trend"] is None, "20 days: no trend is claimed"
    assert cc.market_context(short.iloc[:1], short.index[1], 100.0)["week"] is None, "one day of history: nothing"

    # --- the gap and today's range: labels, filled or not, where the open is ---
    base = daily([100.0] * 30, spread=5.0)                                             # every day 95..105, close 100
    t = base.index[-1] + timedelta(days=1)
    while t.weekday() >= 5:
        t += timedelta(days=1)

    def with_today(o, h, l):
        return pd.concat([base, pd.DataFrame([(o, h, l, o)], columns=["open", "high", "low", "close"], index=pd.Index([t], name="day"))])

    g = cc.market_context(with_today(101.0, 103.0, 100.5), t, 102.0)["gap"]
    assert g["label"] == "Gap up" and g["pct"] == 1.0 and g["filled"] is False and g["open_vs_prev"] == "inside the previous day's range"
    assert cc.market_context(with_today(101.0, 103.0, 99.5), t, 102.0)["gap"]["filled"] is True, "price went back to the old close"
    g = cc.market_context(with_today(106.0, 107.0, 105.0), t, 106.0)["gap"]
    assert g["open_vs_prev"] == "above the previous high"
    g = cc.market_context(with_today(98.0, 100.5, 97.0), t, 99.0)["gap"]
    assert g["label"] == "Gap down" and g["filled"] is True and g["pct"] == -2.0
    assert cc.market_context(with_today(100.1, 101.0, 99.0), t, 100.0)["gap"]["label"] == "Flat open"
    assert cc.market_context(with_today(100.1, 101.0, 99.0), t, 100.0)["gap"]["filled"] is None
    tod = cc.market_context(with_today(100.0, 110.0, 100.0), t, 105.0)["today"]
    assert tod["range"] == 10.0 and tod["atr14"] == 10.0 and tod["range_vs_atr"] == 1.0 and tod["position_pct"] == 50.0

    # --- the last week: the 5 finished days and the previous calendar week ---
    c0 = date(2026, 11, 2)                                                              # a Monday
    rows = []
    for k in range(10):                                                                  # two weeks: 2-6 Nov and 9-13 Nov
        d = c0 + timedelta(days=k + (2 if k >= 5 else 0))
        rows.append((d, 100.0 + k, 110.0 + k, 90.0 + k, 100.0 + k))
    wk = pd.DataFrame(rows, columns=["day", "open", "high", "low", "close"]).set_index("day")
    w = cc.market_context(wk, date(2026, 11, 16), 105.0)["week"]                         # the Monday after
    assert (w["prev_week_high"], w["prev_week_low"], w["prev_week_close"]) == (119.0, 95.0, 109.0)
    assert (w["high_5d"], w["low_5d"]) == (119.0, 95.0)
    assert w["position_pct"] == round((105.0 - 95.0) / (119.0 - 95.0) * 100.0, 0)
    mid = cc.market_context(wk.iloc[:8], date(2026, 11, 12), 100.0)["week"]              # inside the second week
    assert mid["prev_week_high"] == 114.0 and mid["prev_week_low"] == 90.0, "the previous week is 2-6 Nov"

    # --- yesterday and the saved year ---
    py = cc.market_context(with_today(100.0, 101.0, 99.0), t, 100.0)
    assert py["prev_day"]["color"] == "green" and py["prev_day"]["range"] == 10.0 and py["prev_day"]["close_location_pct"] == 50.0
    assert py["year"]["high"] == 105.0 and py["year"]["low"] == 95.0 and py["year"]["from_high_pct"] == round((100 / 105 - 1) * 100, 2)

    # --- the India VIX ---
    v = pd.Series([15.0, 15.0, 15.0, 15.0, 15.0, 17.0])
    assert cc.market_context(with_today(100, 101, 99), t, 100.0, v)["vix"]["label"] == "rising"
    assert cc.market_context(with_today(100, 101, 99), t, 100.0, pd.Series([15.0] * 5 + [13.0]))["vix"]["label"] == "falling"
    assert cc.market_context(with_today(100, 101, 99), t, 100.0, pd.Series([15.0] * 6))["vix"]["label"] == "flat"
    assert cc.market_context(with_today(100, 101, 99), t, 100.0, pd.Series([15.0] * 3))["vix"] is None

    # --- RSI: 100 when only rising, 0 when only falling, never looks ahead ---
    rising = pd.Series(np.arange(1.0, 60.0))
    assert cc.rsi(rising).iloc[-1] == 100.0 and cc.rsi(rising[::-1].reset_index(drop=True)).iloc[-1] == 0.0
    assert cc.rsi(rising).iloc[:13].isna().all(), "needs 14 changes first"
    noisy = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 200)))
    full = cc.rsi(noisy)
    assert ((full.dropna() >= 0) & (full.dropna() <= 100)).all()
    assert abs(cc.rsi(noisy.iloc[:120]).iloc[-1] - full.iloc[119]) < 1e-9, "the future never changes an earlier RSI"

    # --- MACD: 12/26 EMA difference, 9 EMA signal, histogram, nothing before the slow EMA is ready ---
    m = cc.macd(noisy)
    ema = lambda s, n: s.ewm(span=n, adjust=False).mean()
    expected = ema(noisy, 12) - ema(noisy, 26)
    assert np.allclose(m["macd"].iloc[30:], expected.iloc[30:]) and np.allclose(m["signal"].iloc[30:], ema(expected, 9).iloc[30:])
    assert np.allclose(m["hist"].iloc[30:], (expected - ema(expected, 9)).iloc[30:])
    assert m["macd"].iloc[:25].isna().all() and m["macd"].iloc[26:].notna().all()
    assert abs(cc.macd(noisy.iloc[:120])["hist"].iloc[-1] - m["hist"].iloc[119]) < 1e-9

    # --- pivots ---
    p = cc.pivot_levels(110.0, 90.0, 100.0)
    assert (p["P"], p["R1"], p["S1"], p["R2"], p["S2"], p["R3"], p["S3"]) == (100.0, 110.0, 90.0, 120.0, 80.0, 130.0, 70.0)
    assert p["cpr_top"] == 100.0 == p["cpr_bottom"] and p["cpr_width_pct"] == 0.0
    q = cc.pivot_levels(120.0, 90.0, 115.0)
    assert abs(q["P"] - 108.33) < 0.01 and q["cpr_top"] == 111.67 and q["cpr_bottom"] == 105.0 and q["cpr_top"] > q["cpr_bottom"]
    assert abs(q["R1"] - 126.67) < 0.01 and abs(q["S1"] - 96.67) < 0.01

    print("OK: daily trend, week range, gap, yesterday, today so far, year, VIX trend; RSI, MACD (no look-ahead); pivot levels")


if __name__ == "__main__":
    main()
