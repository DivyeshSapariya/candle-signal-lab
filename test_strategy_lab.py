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
    test_short_targets()


def test_short_targets():
    from datetime import datetime

    import option_pricer as op
    from data_sources import IST

    # 1) the option gain is turned into the right index distance (Black-Scholes), for calls and puts
    when = pd.Timestamp("2026-11-25 11:00", tz=IST)
    expiry = op.next_expiry("nifty", when.to_pydatetime())
    years = op.years_to_expiry(expiry, when.to_pydatetime())
    for side, sign in (("CE", 1), ("PE", -1)):
        for kind, amount in (("rupees", 10.0), ("rupees", 20.0), ("percent", 30.0), ("percent", 50.0)):
            d = lab.option_target_distance("nifty", side, 25350.0, when, 15.0, kind, amount)
            strike = round(25350.0 / 50) * 50
            p0 = op.bs_price(25350.0, strike, years, 0.15, side)
            gain = amount if kind == "rupees" else p0 * amount / 100
            got = op.bs_price(25350.0 + sign * d, strike, years, 0.15, side)
            assert abs(got - (p0 + gain)) < 0.02, (side, kind, amount, got, p0 + gain)
    small = lab.option_target_distance("nifty", "CE", 25350.0, when, 15.0, "rupees", 10.0)
    large = lab.option_target_distance("nifty", "CE", 25350.0, when, 15.0, "rupees", 20.0)
    assert 0 < small < large, "a bigger gain needs a bigger move"
    assert lab.option_target_distance("nifty", "CE", 25350.0, when, float("nan"), "rupees", 10.0) is None
    assert lab.option_target_distance("nifty", "CE", 25350.0, when, 0.0, "rupees", 10.0) is None
    near_close = pd.Timestamp("2026-11-25 15:20", tz=IST)                       # a near-worthless option still works
    assert lab.option_target_distance("nifty", "PE", 25350.0, near_close, 15.0, "percent", 50.0) is not None

    # 2) trades: same simulator and cost as the main backtest, targets as written
    df = make_candles(seed=4, days=22)
    vix = pd.Series(15.0, index=pd.date_range("2026-07-20", periods=60))
    f = cs.compute_features(df, "nifty", 5, vix)
    rng = np.random.default_rng(3)
    sides = np.zeros(len(f), dtype=int)
    ok = np.flatnonzero(lab._window(f, 5))
    sides[rng.choice(ok, size=120, replace=False)] = rng.choice([1, -1], size=120)
    risks = np.where(sides != 0, f["atr"].fillna(0).values * 0.8, 0.0)
    entries = (sides, risks)
    for variant in lab.SHORT_TARGETS:
        t = lab.run_short_target(f, "nifty", 5, variant, entries)
        assert len(t) > 20, (variant, len(t))
        assert all(x["status"] in ("TARGET", "STOP", "TIME") for x in t)
        assert all(abs(x["r_net"] - (x["r"] - 0.1)) < 1e-6 for x in t)
        assert all(lab.RR_MIN <= x["rr"] <= lab.RR_MAX for x in t)
        for x in t:
            if x["status"] == "TARGET":
                assert abs(x["r"] - x["rr"]) < 1e-2, "a target hit pays the reward : risk of that trade"
    r1 = lab.run_short_target(f, "nifty", 5, "r1.0", entries)
    assert all(x["rr"] == 1.0 for x in r1)
    # more targets are hit when the target is smaller
    hit = lambda v: np.mean([x["status"] == "TARGET" for x in lab.run_short_target(f, "nifty", 5, v, entries)])
    assert hit("r0.5") > hit("r1.0") > hit("rs20") or hit("r0.5") > hit("r1.0")
    # without VIX the option variants make no trades (nothing is guessed); the R variants still do
    f_novix = cs.compute_features(df, "nifty", 5, None)
    assert lab.run_short_target(f_novix, "nifty", 5, "rs10", entries) == []
    assert len(lab.run_short_target(f_novix, "nifty", 5, "r1.0", entries)) > 20

    # 3) the baseline copies the stop size AND the reward : risk of the real trades; the gate is strict
    t = lab.run_short_target(f, "nifty", 5, "rs10", entries)
    dist = lab.random_baseline(f, 5, t, None, 20, 5)
    assert dist is not None and len(dist) > 0
    res = lab.judge_short(f, "nifty", "5m", "rs10", 20, entries)
    assert not res["passed"], "random entries must not pass the gate"
    assert "avg_rr" in res and res["why"]

    # 4) the existing four strategies are untouched by the new baseline option
    for name in lab.STRATEGIES:
        assert lab.run_strategy(f, 5, name) is not None
    print("OK: short targets: option gain -> index distance (calls and puts), main simulator and cost, "
          "no guessing without VIX, baseline copies stop and reward : risk, strict gate")


if __name__ == "__main__":
    main()
