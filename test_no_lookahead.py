"""
test_no_lookahead.py
--------------------
Checks that the rules never use the future.

For many candles i, the decision made on candles [0..i] must be the same whether or not
the later candles exist. Run:   python test_no_lookahead.py
"""

import numpy as np
import pandas as pd

import candle_signals as cs
from data_sources import IST


def make_candles(seed=3, days=12):
    rng = np.random.default_rng(seed)
    price, rows = 24500.0, []
    for d in pd.bdate_range("2026-08-03", periods=days):
        for ts in pd.date_range(d.strftime("%Y-%m-%d") + " 09:15", periods=75, freq="5min", tz=IST):
            o = price
            c = o + rng.normal(0, 14)
            rows.append((ts, o, max(o, c) + abs(rng.normal(0, 6)),
                         min(o, c) - abs(rng.normal(0, 6)), c, 1000))
            price = c
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"]
                        ).set_index("datetime")


def strip(plan):
    return {k: plan.get(k) for k in ("signal", "entry", "stop", "target", "score", "decision_index")}


def main():
    df = make_candles()
    full = cs.compute_features(df, "nifty", 5, None)
    checked = signals = 0
    for i in range(40, len(df) - 1, 7):
        part = cs.compute_features(df.iloc[: i + 1], "nifty", 5, None)
        a = strip(cs.evaluate(full, i, "nifty", 5, {"confirm": True}))
        b = strip(cs.evaluate(part, i, "nifty", 5, {"confirm": True}))
        checked += 1
        signals += a["signal"] != "NONE"
        assert a == b, f"LOOKAHEAD at candle {i}: with future {a} vs without {b}"
    # also force plenty of signals by relaxing filters, to exercise the comparison
    loose = {k: False for k in cs.DEFAULT_FILTERS}
    loose["confirm"] = True
    for i in range(40, len(df) - 1, 3):
        part = cs.compute_features(df.iloc[: i + 1], "nifty", 5, None)
        a = strip(cs.evaluate(full, i, "nifty", 5, loose))
        b = strip(cs.evaluate(part, i, "nifty", 5, loose))
        checked += 1
        signals += a["signal"] != "NONE"
        assert a == b, f"LOOKAHEAD at candle {i} (loose filters): {a} vs {b}"
    print(f"OK: {checked} candles checked, {signals} signals compared, no lookahead found.")


if __name__ == "__main__":
    main()
