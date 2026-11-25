"""
test_room_check.py
------------------
Checks the optional "room to the next key level" check: the distance maths, the check inside evaluate(), that it is
OFF by default, that it never looks at the future, and the experiment's statistics.

Run:   python test_room_check.py
"""

import numpy as np
import pandas as pd

import candle_signals as cs
import strategy_lab as lab
from test_no_lookahead import make_candles


def row(**kw):
    base = {"pdh": np.nan, "or_high": np.nan, "swing_high": np.nan, "pdl": np.nan, "or_low": np.nan, "swing_low": np.nan}
    base.update(kw)
    return pd.Series(base)


def main():
    # --- the distance: nearest opposing level ahead, in units of the stop distance ---
    r = cs.room_ahead(row(pdh=25150.0, or_high=25300.0, swing_high=25220.0), "CE", 25000.0, 100.0)
    assert r == (1.5, "previous day high"), r                       # 150 points / 100 = 1.5 R, the nearest one
    assert cs.room_ahead(row(pdh=24900.0), "CE", 25000.0, 100.0) is None, "a level BEHIND the entry is not in the way"
    assert cs.room_ahead(row(), "CE", 25000.0, 100.0) is None, "no level at all"
    assert cs.room_ahead(row(pdh=25100.0), "CE", 25000.0, 0.0) is None
    p = cs.room_ahead(row(pdl=24850.0, swing_low=24700.0, or_low=24950.0), "PE", 25000.0, 100.0)
    assert p == (0.5, "opening range low"), p
    assert cs.room_ahead(row(pdh=25050.0), "PE", 25000.0, 100.0) is None, "highs do not block a PE"

    # --- inside evaluate(): off by default, and the check follows the distance ---
    assert cs.DEFAULT_FILTERS["room"] is False
    loose = {k: False for k in cs.DEFAULT_FILTERS}
    loose["confirm"] = False
    df = make_candles(seed=2, days=6)
    f = cs.compute_features(df, "nifty", 5, None)
    found = None
    for i in range(40, len(f) - 1):
        plan = cs.evaluate(f, i, "nifty", 5, loose)
        if plan["signal"] != "NONE":
            found = (i, plan)
            break
    assert found, "need a signal to test the check"
    i, plan = found
    side, entry, risk = plan["signal"], plan["entry"], plan["risk_points"]
    default_plan = cs.evaluate(f, i, "nifty", 5, loose)
    assert not any(x["key"] == "room" for x in default_plan["reasons"]), "no room reason while the check is off"
    sign = 1 if side == "CE" else -1
    cols = ("pdh", "or_high", "swing_high") if side == "CE" else ("pdl", "or_low", "swing_low")
    g = f.copy()
    for c in cols:
        g.iloc[i, g.columns.get_loc(c)] = np.nan
    on = dict(loose, room=True)
    res = cs.evaluate(g, i, "nifty", 5, on)
    reason = [x for x in res["reasons"] if x["key"] == "room"][0]
    assert reason["ok"] and "No key level" in reason["text"] and res["signal"] == side
    g.iloc[i, g.columns.get_loc(cols[0])] = entry + sign * 1.0 * risk                     # a level 1 R ahead: before the 2 R target
    res = cs.evaluate(g, i, "nifty", 5, on)
    reason = [x for x in res["reasons"] if x["key"] == "room"][0]
    assert not reason["ok"] and "only 1.0 R ahead" in reason["text"] and res["signal"] == "NONE"
    g.iloc[i, g.columns.get_loc(cols[0])] = entry + sign * 2.5 * risk                     # 2.5 R ahead: room for the target
    res = cs.evaluate(g, i, "nifty", 5, on)
    reason = [x for x in res["reasons"] if x["key"] == "room"][0]
    assert reason["ok"] and "2.5 R ahead" in reason["text"] and res["signal"] == side
    assert res["entry"] == entry and abs(res["stop"] - plan["stop"]) < 1e-9, "entry and stop are the same as before"

    # --- no look-ahead: the same decision with or without later candles ---
    for j in range(40, len(df) - 1, 11):
        a = cs.evaluate(f, j, "nifty", 5, on)
        b = cs.evaluate(cs.compute_features(df.iloc[: j + 1], "nifty", 5, None), j, "nifty", 5, on)
        assert (a["signal"], a.get("entry"), a["score"]) == (b["signal"], b.get("entry"), b["score"]), j

    # --- the experiment's statistics ---
    same = lab.diff_range([0.1] * 30 + [-0.1] * 30, [0.1] * 30 + [-0.1] * 30)
    assert same[1] < 0 < same[2], "identical groups: the range includes 0"
    better = lab.diff_range([0.5] * 40 + [-0.2] * 20, [-0.3] * 40 + [0.1] * 20)
    assert better[0] > 0 and better[1] > 0, "a clearly better kept group gives a range above 0"
    assert lab.diff_range([0.1] * 3, [0.2] * 50) is None, "too few trades: no claim"

    vix = pd.Series(15.0, index=pd.date_range("2026-07-20", periods=60))
    fv = cs.compute_features(make_candles(seed=4, days=22), "nifty", 5, vix)
    trades = lab.room_split(fv, "nifty", 5)
    assert all({"r_net", "target", "room", "room_ok"} <= set(t) for t in trades)
    assert all((t["room"] is None or t["room"] >= cs.ROOM_MIN_R) == t["room_ok"] for t in trades)
    res = lab.judge_room(fv, "nifty", "5m", 20)
    assert not res["passed"], "random data must not pass the gate"

    print("OK: room distance maths, check inside evaluate (off by default, same entry and stop), no look-ahead, experiment statistics")


if __name__ == "__main__":
    main()
