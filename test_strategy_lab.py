"""
test_strategy_lab.py
--------------------
Checks the strategy lab: no look-ahead, the strategy rules do what they say, and the gate is strict.

Run:   python test_strategy_lab.py
"""

import numpy as np
import pandas as pd

import candle_signals as cs
import strategy_lab as lab
from test_no_lookahead import make_candles


def main():
    df = make_candles(seed=4, days=22)
    f = cs.compute_features(df, "nifty", 5, None)

    # 1) the four signal makers use only candles up to the decision candle (no look-ahead)
    total = 0
    for name, (fn, rr, _) in lab.STRATEGIES.items():
        full_s, full_r = fn(f, 5)
        hits = [int(i) for i in np.flatnonzero(full_s)]
        spread = hits[:: max(1, len(hits) // 10)][:10]           # real signals, spread over the data
        for i in sorted(set(spread + list(range(80, len(df) - 1, 97)))):
            part = cs.compute_features(df.iloc[: i + 1], "nifty", 5, None)
            s2, r2 = fn(part, 5)
            assert full_s[i] == s2[i], f"{name}: look-ahead at candle {i}"
            if full_s[i]:
                assert abs(full_r[i] - r2[i]) < 1e-9, f"{name}: stop differs at candle {i}"
                total += 1
    print(f"OK: no look-ahead in the 4 strategies ({total} signals compared)")

    # 2) the rules do what the docstring says
    sides, risks = lab.signals_orb(f, 5)
    days = f["day"].values
    for d in pd.unique(days):
        n = int(np.sum((days == d) & (sides != 0)))
        assert n <= 1, "ORB: more than one breakout trade in a day"
    idx = np.flatnonzero(sides)
    assert len(idx) > 0
    assert (f["start_min"].values[idx] >= cs.TRADE_START_MIN + 15).all(), "ORB traded before the range was done"
    for i in idx:
        assert (sides[i] == 1 and f["close"].values[i] > f["or_high"].values[i]) or \
               (sides[i] == -1 and f["close"].values[i] < f["or_low"].values[i]), "ORB: close not outside range"
    for name in lab.STRATEGIES:
        s, r = lab.STRATEGIES[name][0](f, 5)
        hit = np.flatnonzero(s)
        assert (f["start_min"].values[hit] + 5 <= cs.TRADE_END_MIN).all(), f"{name}: decided after 3 pm"
        atr = f["atr"].values[hit]
        assert (r[hit] >= cs.MIN_RISK_ATR * atr - 1e-9).all() and (r[hit] <= cs.MAX_RISK_ATR * atr + 1e-9).all(), \
            f"{name}: stop outside 0.25..1.5 ATR"
    sv, _ = lab.signals_vwap_pull(f, 5)
    for i in np.flatnonzero(sv):
        row = f.iloc[i]
        assert row["htf15"] == row["htf60"] == sv[i], "VWAP pullback against the trend"
    sm, _ = lab.signals_mean_rev(f, 5)
    for i in np.flatnonzero(sm):
        assert f["adx"].values[i] < lab.QUIET_ADX, "mean reversion on a trending day"
    print("OK: ORB one per day after the range; VWAP pullback with the trend; mean reversion only on quiet days")

    # 3) trades and the gate
    for name in lab.STRATEGIES:
        t = lab.run_strategy(f, 5, name)
        assert all(x["status"] in ("TARGET", "STOP", "TIME") for x in t)
        assert all(abs(x["r_net"] - (x["r"] - 0.1)) < 1e-6 for x in t)
    res = lab.judge(f, "nifty", "5m", "orb", runs=20)
    assert not res["passed"], "random data must not pass the gate"      # no edge exists in this data
    assert res["trades"] < lab.MIN_TRADES or res["why"], "gate gave no reason"
    print(f"OK: trades use the main simulator and cost; random data fails the gate ({res['why']})")


if __name__ == "__main__":
    main()
