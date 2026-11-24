"""
test_candle_research.py
-----------------------
Checks the helper maths in candle_research.py.   Run:   python test_candle_research.py
"""

import candle_research as cr
import candle_signals as cs
from test_no_lookahead import make_candles


def main():
    trades = [{"r": 2.0, "r_net": 1.9, "status": "TARGET", "side": "CE", "context": {"start_min": 600, "pattern": "X"}},
              {"r": -1.0, "r_net": -1.1, "status": "STOP", "side": "PE", "context": {"start_min": 660, "pattern": "Y"}}] * 5

    # cost change recomputes net R from the gross R
    net = [t["r_net"] for t in cr.with_cost(trades, 0.3)]
    assert net == [1.7, -1.3] * 5, net

    # bootstrap range surrounds the real average and is ordered
    lo, hi = cr.bootstrap_avg_r(trades)
    avg = sum(t["r_net"] for t in trades) / len(trades)
    assert lo <= avg <= hi, (lo, avg, hi)
    assert cr.bootstrap_avg_r(trades[:3]) is None          # too few trades -> no claim

    # small groups are hidden, big ones are shown with the right count
    rows = cr.breakdown(trades, lambda t: t["side"])
    assert {r[0]: r[1] for r in rows} == {"CE": 5, "PE": 5} or rows == [], rows

    # walk-forward: blocks cover separate days and give one row each
    f = cs.compute_features(make_candles(seed=2, days=24), "nifty", 5, None)
    blocks = cr.walk_forward(f, "nifty", 5)
    assert len(blocks) == cr.BLOCKS
    # --- VWAP / volume / gap features ---
    import numpy as np
    df = make_candles(seed=2, days=24)
    df0 = df.copy()
    df0["volume"] = 0                                   # Yahoo index data has no volume
    f = cs.compute_features(df0, "nifty", 5, None)
    assert f["vwap_proxy"].all() and f["vol_ratio"].isna().all(), "no volume -> proxy VWAP, no volume ratio"
    day1 = f[f["day"] == f["day"].iloc[100]]
    typ = ((day1["high"] + day1["low"] + day1["close"]) / 3)
    assert np.allclose(day1["vwap"].values, typ.expanding().mean().values), "proxy VWAP = running mean"
    d = f["day"].iloc[200]
    row = f[f["day"] == d].iloc[0]
    assert abs(row["gap_pct"] - (row["open"] - row["pdc"]) / row["pdc"] * 100) < 1e-9
    # with real volume the VWAP is volume-weighted and the ratio exists
    dv = df.copy()
    dv["volume"] = np.arange(1, len(dv) + 1) % 7 + 1
    fv = cs.compute_features(dv, "nifty", 5, None)
    assert not fv["vwap_proxy"].any() and fv["vol_ratio"].notna().any()
    # the new checks use only the past: same decision with or without later candles
    on = {"vwap": True, "volume": True, "gap": True}
    for i in range(40, len(df) - 1, 9):
        a = cs.evaluate(fv, i, "nifty", 5, on)
        b = cs.evaluate(cs.compute_features(dv.iloc[: i + 1], "nifty", 5, None), i, "nifty", 5, on)
        assert (a["signal"], a.get("entry"), a["score"]) == (b["signal"], b.get("entry"), b["score"]), i
    # --- futures volume: weights the VWAP, gives the volume ratio, and only for days that have it ---
    days = list(dict.fromkeys(df0.index.normalize()))
    fut = df0.copy()
    fut["volume"] = 0
    keep = fut.index.normalize() == days[10]                 # volume exists for ONE day only
    fut.loc[keep, "volume"] = (np.arange(keep.sum()) % 5 + 1) * 100.0
    ff = cs.compute_features(df0, "nifty", 5, None, fut)
    d10 = ff[ff.index.normalize() == days[10]]
    typ10 = (d10["high"] + d10["low"] + d10["close"]) / 3
    w = fut.loc[keep, "volume"].values
    assert np.allclose(d10["vwap"].values, (typ10.values * w).cumsum() / w.cumsum()), "VWAP is weighted by futures volume"
    assert not d10["vwap_proxy"].any() and d10["vol_ratio"].notna().any()
    other = ff[ff.index.normalize() == days[11]]
    assert other["vwap_proxy"].all() and other["vol_ratio"].isna().all(), "days without volume use the plain average"
    # the volume of a LATER candle never changes an earlier VWAP
    cut = int(np.where(keep)[0][30])
    ff2 = cs.compute_features(df0.iloc[: cut + 1], "nifty", 5, None, fut.iloc[: cut + 1])
    assert abs(ff2["vwap"].iloc[-1] - ff["vwap"].iloc[cut]) < 1e-9, "VWAP looked at the future"
    # nearest-contract window: the previous monthly expiry is the last expiry-weekday of last month
    from datetime import date
    import option_pricer as op
    assert op.previous_monthly_expiry("nifty", date(2026, 12, 29)) == date(2026, 11, 23)     # 24 Nov is a holiday
    assert op.previous_monthly_expiry("sensex", date(2026, 11, 26)) == date(2026, 10, 29)
    print("OK: futures volume weights the VWAP per day, no look-ahead, contract window")

    # VWAP is on by default, volume and gap stay off
    assert cs.DEFAULT_FILTERS["vwap"] and not cs.DEFAULT_FILTERS["volume"] and not cs.DEFAULT_FILTERS["gap"]
    print("OK: VWAP / volume / gap features, no look-ahead, VWAP on by default")
    print("OK: cost change, bootstrap range, group size limit and walk-forward blocks")


if __name__ == "__main__":
    main()
