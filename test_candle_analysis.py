"""
test_candle_analysis.py
-----------------------
Checks that the "Live analysis" panel always agrees with the real decision.

Run:   python test_candle_analysis.py
"""

from datetime import timedelta

import candle_signals as cs
from candle_analysis import live_analysis, rejected_setups
from test_no_lookahead import make_candles


def main():
    df = make_candles(seed=5, days=20)
    f = cs.compute_features(df, "nifty", 5, None)
    loose = {k: False for k in cs.DEFAULT_FILTERS}
    loose["confirm"] = True

    # 1) rejected setups: every one is a real NONE decision that had a pattern, and it says why
    for filters in (None, loose):
        rej = rejected_setups(f, "nifty", 5, filters, lookback=len(f), limit=10 ** 6)
        times = {r["time"] for r in rej}
        assert len(times) == len(rej), "duplicate rejected rows"
        for i in range(30, len(f)):
            res = cs.evaluate(f, i, "nifty", 5, filters)
            listed = f.index[i].strftime("%d %b %H:%M") in times
            expected = res["signal"] == "NONE" and bool(res.get("reasons"))
            assert listed == expected, f"candle {i}: listed={listed} but expected={expected}"
        assert all(r["why"] and r["setup"] for r in rej), "a rejected row has no reason"
        print(f"OK: {len(rej)} rejected setups match evaluate() (filters={'loose' if filters else 'default'})")

    # 2) the 4 steps agree with the decision for every candle
    ideas = blocked = nopattern = 0
    for i in range(30, len(f)):
        plan = cs.evaluate(f, i, "nifty", 5, None)
        a = live_analysis(plan, f.index[i], 5, True)
        assert [s["key"] for s in a["steps"]] == ["pattern", "confirm", "filters", "points"]
        states = [s["state"] for s in a["steps"]]
        if plan["signal"] != "NONE":
            assert states == ["ok"] * 4, f"candle {i}: idea made but steps are {states}"
            ideas += 1
        else:
            assert "fail" in states or states[0] == "fail", f"candle {i}: no idea but no step failed {states}"
            if not plan.get("reasons"):
                assert states == ["fail", "wait", "wait", "wait"], states
                nopattern += 1
            else:
                blocked += 1
    print(f"OK: steps match decisions ({ideas} ideas, {blocked} blocked setups, {nopattern} no-pattern candles)")

    # 3) next-candle time: running candle closes two candle-lengths after the last finished start
    a = live_analysis(cs.evaluate(f, len(f) - 1, "nifty", 5, None), f.index[-1], 5, True)
    expect = (f.index[-1] + timedelta(minutes=10)).strftime("%H:%M")
    assert a["next_candle_closes"] == expect, (a["next_candle_closes"], expect)
    a = live_analysis(cs.evaluate(f, len(f) - 1, "nifty", 5, None), f.index[-1], 5, False)
    assert a["next_candle_closes"] is None
    print("OK: next-candle time shown only while the market is open")


if __name__ == "__main__":
    main()
