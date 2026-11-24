"""
test_oi_filters.py
------------------
Checks the open-interest features and the three optional oi_* checks:
right numbers, never the future, never on by default, and no numbers shown with too little history.

Run:   python test_oi_filters.py
"""

import contextlib
import io

import numpy as np
import pandas as pd

import candle_chain as cc
import candle_research as cr
import candle_signals as cs
from data_sources import IST
from test_no_lookahead import make_candles


def make_chain(day, strikes=(25300, 25350, 25400, 25450, 25500, 25550, 25600), bars=75, bump_from=None):
    """Saved option table for one day: OI grows with time; puts grow faster than calls (put writing)."""
    rows = []
    for k in range(bars):
        ts = pd.Timestamp(f"{day} 09:15", tz=IST) + pd.Timedelta(minutes=5 * k)
        for strike in strikes:
            ce = 1000.0 + 10 * k + (strike - 25300) * 2
            pe = 1000.0 + 30 * k + (25600 - strike) * 2
            if bump_from is not None and k >= bump_from:
                ce, pe = ce * 5, pe * 5                         # a "future" change that must not leak backwards
            rows.append((ts, "2026-08-06", float(strike), "CE", 1.0, 1.0, ce))
            rows.append((ts, "2026-08-06", float(strike), "PE", 1.0, 1.0, pe))
    df = pd.DataFrame(rows, columns=["datetime", "expiry", "strike", "side", "close", "volume", "oi"]).set_index("datetime")
    return df


def main():
    df = make_candles(seed=2, days=6)
    days = list(dict.fromkeys(df.index.strftime("%Y-%m-%d")))
    chain = make_chain(days[3])

    # --- features: right numbers, only on the day that has OI ---
    f = cs.compute_features(df, "nifty", 5, None, None, chain)
    assert f["oi_pcr"].notna().sum() > 60 and f["oi_pcr"].notna().sum() <= 75
    other = f[f["day"] != pd.Timestamp(days[3]).date()]
    assert other["oi_pcr"].isna().all(), "OI must not appear on days that have none"
    row = f[f.index == pd.Timestamp(f"{days[3]} 10:15", tz=IST)].iloc[0]             # bar k = 12 at the key (5m)
    k = 12
    ce_sum = sum(1000.0 + 10 * k + (s - 25300) * 2 for s in (25300, 25350, 25400, 25450, 25500, 25550, 25600))
    pe_sum = sum(1000.0 + 30 * k + (25600 - s) * 2 for s in (25300, 25350, 25400, 25450, 25500, 25550, 25600))
    assert abs(row["oi_pcr"] - pe_sum / ce_sum) < 1e-9, (row["oi_pcr"], pe_sum / ce_sum)
    assert row["oi_call_wall"] == 25600 and row["oi_put_wall"] == 25300
    assert row["oi_flow"] > 0, "puts grow faster than calls = positive build-up"

    # --- the future never leaks: changing LATER bars does not change an earlier candle ---
    f2 = cs.compute_features(df, "nifty", 5, None, None, make_chain(days[3], bump_from=40))
    early = f.index < pd.Timestamp(f"{days[3]} 09:15", tz=IST) + pd.Timedelta(minutes=5 * 40)
    assert np.allclose(f.loc[early, ["oi_pcr", "oi_flow"]].fillna(-9), f2.loc[early, ["oi_pcr", "oi_flow"]].fillna(-9))
    # a 15-minute candle waits for its bar: it uses the bar that finished before it closed
    f15 = cs.compute_features(make_candles(seed=2, days=6).resample("15min", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(), "nifty", 15, None, None, chain)
    start = pd.Timestamp(f"{days[3]} 10:00", tz=IST)                       # closes 10:15; bars up to 10:10 are usable
    got = f15.loc[start]
    bars = cc._bar_features(chain)
    assert abs(got["oi_pcr"] - bars.loc[pd.Timestamp(f"{days[3]} 10:10", tz=IST), "oi_pcr"]) < 1e-12

    # --- too few strikes or stale bars give nothing, not a guess ---
    thin = make_chain(days[3], strikes=(25300, 25350, 25400))
    assert cs.compute_features(df, "nifty", 5, None, None, thin)["oi_pcr"].isna().all()
    gap = chain[(chain.index < pd.Timestamp(f"{days[3]} 10:00", tz=IST)) | (chain.index > pd.Timestamp(f"{days[3]} 11:00", tz=IST))]
    fg = cs.compute_features(df, "nifty", 5, None, None, gap)
    at = fg.loc[pd.Timestamp(f"{days[3]} 10:45", tz=IST)]
    assert pd.isna(at["oi_pcr"]), "a bar older than 20 minutes is stale"

    # --- off by default; with no OI the checks change nothing ---
    assert not any(cs.DEFAULT_FILTERS[k] for k in ("oi_pcr", "oi_wall", "oi_flow"))
    f0 = cs.compute_features(df, "nifty", 5, None)
    loose = {"vwap": False}
    on = {"oi_pcr": True, "oi_wall": True, "oi_flow": True}
    for i in range(40, len(f0) - 1, 5):
        a, b = cs.evaluate(f0, i, "nifty", 5, loose), cs.evaluate(f0, i, "nifty", 5, {**loose, **on})
        assert (a["signal"], a.get("entry")) == (b["signal"], b.get("entry")), i

    # --- the checks themselves, on a real signal candle ---
    allow = {k: False for k in cs.DEFAULT_FILTERS}
    allow["confirm"] = False
    pick = None
    for i in range(40, len(f0) - 1):
        r = cs.evaluate(f0, i, "nifty", 5, allow)
        if r["signal"] != "NONE":
            pick = (i, r["signal"])
            break
    assert pick, "need a signal to test the checks"
    i, side = pick
    g = f0.copy()
    c = g.iloc[i]
    good_pcr, bad_pcr = (1.3, 0.7) if side == "CE" else (0.7, 1.3)
    for pcr, flow, expect in ((good_pcr, 0.05 if side == "CE" else -0.05, True), (bad_pcr, -0.05 if side == "CE" else 0.05, False)):
        g.iloc[i, g.columns.get_loc("oi_pcr")] = pcr
        g.iloc[i, g.columns.get_loc("oi_flow")] = flow
        g.iloc[i, g.columns.get_loc("oi_call_wall")] = c["close"] + 10 * c["atr"]
        g.iloc[i, g.columns.get_loc("oi_put_wall")] = c["close"] - 10 * c["atr"]
        for key in ("oi_pcr", "oi_flow"):
            res = cs.evaluate(g, i, "nifty", 5, {**allow, key: True})
            reason = [r for r in res["reasons"] if r["key"] == key]
            assert reason and reason[0]["ok"] is expect, (key, expect, res["reasons"])
            assert (res["signal"] != "NONE") is expect
    # wall: right under the call wall blocks a CE, over the put wall blocks a PE; far away does not
    g.iloc[i, g.columns.get_loc("oi_call_wall")] = c["close"] + 0.5 * c["atr"]
    g.iloc[i, g.columns.get_loc("oi_put_wall")] = c["close"] - 0.5 * c["atr"]
    res = cs.evaluate(g, i, "nifty", 5, {**allow, "oi_wall": True})
    assert res["signal"] == "NONE" and [r for r in res["reasons"] if r["key"] == "oi_wall"][0]["ok"] is False
    g.iloc[i, g.columns.get_loc("oi_call_wall")] = c["close"] + 5 * c["atr"]
    g.iloc[i, g.columns.get_loc("oi_put_wall")] = c["close"] - 5 * c["atr"]
    assert cs.evaluate(g, i, "nifty", 5, {**allow, "oi_wall": True})["signal"] == side

    # --- research: with a few days of OI no numbers are shown ---
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cr.oi_section(f, "nifty", 5)
    text = out.getvalue()
    assert "Not enough history yet" in text and "avg" not in text.lower(), text

    print("OK: OI features (values, no look-ahead, stale/thin data empty), oi_* checks off by default, no numbers on thin history")


if __name__ == "__main__":
    main()
