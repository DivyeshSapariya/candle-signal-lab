"""
test_late_trade.py
------------------
A late-day idea must be closed at the day's last candle, also when the feed has no 15:25 candle
(Angel One's closing print is often flat and is dropped). While today's session is still running
the trade must stay open.

Run:   python test_late_trade.py
"""

import candle_signals as cs
from candle_backtest import simulate_trade
from test_no_lookahead import make_candles


def main():
    df = make_candles(seed=2, days=3)
    start = df.index.hour * 60 + df.index.minute
    day2 = df.index.normalize() == sorted(set(df.index.normalize()))[1]

    def run(frame, at, mode="confirm", risk=5000.0):
        f = cs.compute_features(frame, "nifty", 5, None)
        i = next(k for k in range(len(f)) if f.index[k].strftime("%H:%M") == at and f.index[k].normalize() == sorted(set(f.index.normalize()))[1])
        price = float(f["close"].iloc[i])
        plan = {"signal": "CE", "entry": price, "risk_points": risk, "mode": mode}   # stop / target far away: only the day end can close it
        return f, simulate_trade(f, i, plan, 5)

    # 1) the full day: closed at 15:25 as before
    f, res = run(df, "14:35")
    assert res["status"] == "TIME" and res["exit_time"].startswith("2026-08-04T15:25"), res

    # 2) no 15:25 candle (and none after 15:10): the next day's candle shows the day is over -> closed at 15:10
    short = df[~(day2 & (start >= 15 * 60 + 15))]
    f, res = run(short, "14:35")
    assert res["status"] == "TIME" and res["exit_time"].startswith("2026-08-04T15:10"), res
    assert res["bars"] == 7 and res["r"] is not None          # 14:40 (fill) to 15:10

    # 3) the data simply ends today (the session is still running): the trade stays open, nothing is invented
    live = df[df.index <= df.index[day2][-1]]
    live = live[~(live.index.normalize() == live.index[-1].normalize()) | (live.index.hour * 60 + live.index.minute < 15 * 60 + 10)]
    f2 = cs.compute_features(live, "nifty", 5, None)
    i = len(f2) - 6
    plan = {"signal": "CE", "entry": float(f2["close"].iloc[i]), "risk_points": 5000.0, "mode": "confirm"}
    assert simulate_trade(f2, i, plan, 5)["status"] == "OPEN"

    # 4) a trade that reaches its stop or target first is not touched by the new rule
    f, res = run(short, "14:35", risk=0.5)
    assert res["status"] in ("STOP", "TARGET"), res
    # 5) the time limit still closes a long trade early (24 candles of 5 minutes = 120 minutes)
    f, res = run(short, "09:40")
    assert res["status"] == "TIME" and res["bars"] == 24, res

    # 6) the journal: an idea saved late on day 2 stays waiting while day 2 is the newest day, and is closed
    #    once day 3 arrives, even though day 2 has no 15:25 candle
    import os
    import tempfile

    import candle_journal as cj
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cj.JOURNAL_FILE = os.path.join(tmp, "j.csv")
        cj.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
        days_sorted = sorted(set(short.index.normalize()))
        only_day2 = short[short.index.normalize() <= days_sorted[1]]
        f_live = cs.compute_features(only_day2, "nifty", 5, None)
        i = next(k for k in range(len(f_live)) if f_live.index[k].strftime("%H:%M") == "14:35" and f_live.index[k].normalize() == days_sorted[1])
        price = float(f_live["close"].iloc[i])
        plan = {"signal": "CE", "signal_time": f_live.index[i - 1].isoformat(), "decision_time": f_live.index[i].isoformat(),
                "mode": "confirm", "entry": price, "stop": price - 5000, "target": price + 10000, "risk_points": 5000.0,
                "option": "NIFTY test CE", "score": 3, "context": {}, "reasons": []}
        assert cj.record_idea(plan, "nifty", "5m", "now")
        cj.update_results(f_live, "nifty", 5)
        assert cj._load().iloc[0]["status"] in ("PENDING", "OPEN"), "still waiting while the session may still be running"
        f_next = cs.compute_features(short[short.index.normalize() <= days_sorted[2]], "nifty", 5, None)
        cj.update_results(f_next, "nifty", 5)
        row = cj._load().iloc[0]
        assert row["status"] == "TIME" and row["exit_time"].startswith("2026-08-04T15:10"), dict(row)

    print("OK: a late-day trade is closed at the day's last candle even without a 15:25 candle; a running session stays open")


if __name__ == "__main__":
    main()
