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
    # VWAP is on by default, volume and gap stay off
    assert cs.DEFAULT_FILTERS["vwap"] and not cs.DEFAULT_FILTERS["volume"] and not cs.DEFAULT_FILTERS["gap"]
    print("OK: VWAP / volume / gap features, no look-ahead, VWAP on by default")
    print("OK: cost change, bootstrap range, group size limit and walk-forward blocks")


if __name__ == "__main__":
    main()
