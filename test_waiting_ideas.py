"""
test_waiting_ideas.py
---------------------
A saved idea whose market is not watched by the alert loop and not open in the page must still be settled.
Checks with fake candles, a fake journal file and a fake Telegram (nothing real is touched).

Run:   python test_waiting_ideas.py
"""

import os
import tempfile
import time
from datetime import timedelta

import pandas as pd

import candle_journal as cj
import candle_signals as cs
import dashboard as D
from test_no_lookahead import make_candles


def main():
    df = make_candles(seed=2, days=3)
    days = sorted(set(df.index.normalize()))
    day2 = df[df.index.normalize() == days[1]]
    until_noon = df[(df.index <= day2.index[0] + timedelta(hours=3, minutes=40))]        # day 2 up to about 12:55
    f_short = cs.compute_features(until_noon, "nifty", 5, None)
    i = next(k for k in range(len(f_short)) if f_short.index[k].strftime("%H:%M") == "10:00" and f_short.index[k].normalize() == days[1])
    price = float(f_short["close"].iloc[i])

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cj.JOURNAL_FILE = os.path.join(tmp, "j.csv")
        cj.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
        plan = {"signal": "CE", "signal_time": f_short.index[i - 1].isoformat(), "decision_time": f_short.index[i].isoformat(),
                "mode": "confirm", "entry": price, "stop": price - 3, "target": price + 6, "risk_points": 3.0,
                "option": "NIFTY test CE", "score": 3, "context": {}, "reasons": []}
        assert cj.record_idea(plan, "nifty", "5m", "now", alerts="new")
        assert cj.waiting_markets() == {("nifty", "5m")}

        sent, fetched = [], []
        D.tg.send_async = lambda text: sent.append(text)
        D.tg.format_result = lambda info: f"result {info['status']}"
        D.get_vix_daily = lambda: None

        def fake_candles(source, index, interval, include_forming=True):
            fetched.append((index, interval))
            return until_noon

        D.get_candles = fake_candles
        D._cache.clear()
        D._waiting_seen.clear()
        last = until_noon.index[-1]
        now = last.to_pydatetime() + timedelta(minutes=5, seconds=20)        # the next candle has just closed

        # 1) a watched market is left to its normal build
        assert D._update_waiting_ideas("angel", {("nifty", "5m")}, now) == 0 and not fetched

        # 2) an unwatched market with a waiting idea is fetched and the idea is settled; one result alert goes out
        assert D._update_waiting_ideas("angel", set(), now) == 1
        row = cj._load().iloc[0]
        assert row["status"] in ("TARGET", "STOP", "TIME"), dict(row)
        assert len(sent) == 1 and sent[0].startswith("result"), sent
        assert cj.waiting_markets() == set()

        # 3) nothing is waiting now: no more fetches, no more alerts
        n = len(fetched)
        assert D._update_waiting_ideas("angel", set(), now) == 0 and len(fetched) == n and len(sent) == 1

        # 4) a still-waiting idea is looked at once per candle, not on every tick
        later_plan = dict(plan, signal_time=until_noon.index[-2].isoformat(), decision_time=until_noon.index[-1].isoformat(),
                          entry=price, stop=price - 3, target=price + 6)        # decided on the newest candle: stays waiting
        assert cj.record_idea(later_plan, "nifty", "5m", "now", alerts="")
        D._waiting_seen.clear()
        before = len(fetched)
        D._update_waiting_ideas("angel", set(), now)
        assert len(fetched) == before + 1
        D._update_waiting_ideas("angel", set(), now + timedelta(seconds=30))               # the next tick, same candle
        assert len(fetched) == before + 1, "must not fetch again within the same candle"
        D._update_waiting_ideas("angel", set(), now + timedelta(minutes=6))                # a later candle: fetch again
        assert len(fetched) == before + 2

        # 5) before the first candle of the day has closed it does nothing
        early = now.replace(hour=9, minute=12)
        D._waiting_seen.clear()
        b2 = len(fetched)
        D._update_waiting_ideas("angel", set(), early)
        assert len(fetched) == b2

        # 6) it never makes a new idea: the journal has only the two saved ones
        assert len(cj._load()) == 2

    print("OK: waiting ideas of unwatched markets are settled once per candle, with one result alert, no new ideas")


if __name__ == "__main__":
    main()
