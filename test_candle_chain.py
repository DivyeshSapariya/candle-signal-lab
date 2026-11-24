"""
test_candle_chain.py
--------------------
Checks the open-interest facts for the dashboard (candle_chain.py). No internet, no real database.

Run:   python test_candle_chain.py
"""

from datetime import date, datetime

import pandas as pd

import candle_chain as cc
from data_sources import IST


def table(rows):
    """rows: (time, expiry, strike, side, oi) -> same shape as candle_store.load_option_chain."""
    df = pd.DataFrame(rows, columns=["datetime", "expiry", "strike", "side", "oi"])
    df["close"], df["volume"] = 1.0, 1.0
    df = df.set_index("datetime")
    df.index = pd.DatetimeIndex(df.index).tz_localize(IST)
    return df


def main():
    t = lambda d, h, m=0: datetime(2026, 11, d, h, m)
    rows = []
    for day, scale in ((20, 1.0), (23, 1.5)):                       # two sessions, two times each
        for hh, extra in ((10, -5), (15, 0)):
            for strike, ce, pe in ((25300, 100, 400), (25350, 300, 200), (25400, 900, 100)):
                rows.append((t(day, hh), "2026-12-01", float(strike), "CE", ce * scale + extra))
                rows.append((t(day, hh), "2026-12-01", float(strike), "PE", pe * scale + extra))
    df = table(rows)

    # the LAST value of each day is used, and the previous session is the day before
    expiry, cur, prev, label = cc.saved_snapshot(df)
    assert expiry == "2026-12-01" and label == "23 Nov"
    assert cur[(25400.0, "CE")] == 1350 and prev[(25400.0, "CE")] == 900
    assert cc.previous_session(df, "2026-12-01", date(2026, 11, 23))[(25400.0, "CE")] == 900
    assert cc.previous_session(df, "2026-12-01", date(2026, 11, 20)) == {}, "nothing before the first day"
    assert cc.previous_session(df, "2026-12-08", date(2026, 11, 24)) == {}, "other expiry"

    s = cc.summarize(cur, prev, 25340.0, expiry, "saved", label)
    assert s["calls"] == 150 + 450 + 1350 + 0 and s["puts"] == 600 + 300 + 150
    assert s["pcr"] == round(1050 / 1950, 2)
    assert s["call_wall"]["strike"] == 25400.0 and s["put_wall"]["strike"] == 25300.0
    assert s["call_wall"]["chg"] == 450                                # 1350 - 900
    assert s["calls_chg_pct"] == 50.0 and s["puts_chg_pct"] == 50.0
    assert [r["strike"] for r in s["rows"]] == [25300.0, 25350.0, 25400.0]
    assert any("40 points above the put wall and 60 below the call wall" in f for f in s["facts"]), s["facts"]
    assert s["facts"][-1] == cc.LOOKAROUND

    # no previous session: no changes are claimed
    s2 = cc.summarize(cur, {}, 25340.0, expiry, "live", "now")
    assert s2["calls_chg_pct"] is None and s2["call_wall"]["chg"] is None
    assert not any("previous session" in f for f in s2["facts"])

    # index outside the walls, and no call OI at all, do not crash
    assert any("ABOVE the call wall" in f for f in cc.summarize(cur, {}, 25500.0, expiry, "live", "x")["facts"])
    assert any("BELOW the put wall" in f for f in cc.summarize(cur, {}, 25200.0, expiry, "live", "x")["facts"])
    only_puts = {k: v for k, v in cur.items() if k[1] == "PE"}
    assert cc.summarize(only_puts, {}, 25340.0, expiry, "live", "x")["pcr"] is None
    assert cc.summarize({}, {}, 25340.0, expiry, "live", "x") is None
    assert cc.saved_snapshot(table([])[0:0]) is None

    print("OK: open-interest facts (last value per day, previous session, walls, ratio, no look-ahead of other expiries)")


if __name__ == "__main__":
    main()
