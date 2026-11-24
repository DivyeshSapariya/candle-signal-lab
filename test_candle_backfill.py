"""
test_candle_backfill.py
-----------------------
Checks the Angel One downloader with a FAKE Angel One (no login, no internet, no real database).

Run:   python test_candle_backfill.py
"""

import os
import tempfile
from datetime import datetime, timedelta

import pandas as pd

import candle_store
import data_sources as ds
from data_sources import IST


def fake_candles(start, end, interval_min=5):
    """5-minute candles 09:15-15:25 on weekdays between start and end."""
    rows = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for k in range(75):
                ts = day.replace(hour=9, minute=15) + timedelta(minutes=interval_min * k)
                if start <= ts <= end:
                    rows.append((ts, 100.0 + k, 101.0 + k, 99.0 + k, 100.5 + k, 0))
        day += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")
    if df.empty:
        df.index = pd.DatetimeIndex([], tz=IST, name="datetime")
    return df


def main():
    now = datetime(2026, 11, 20, 16, 0, tzinfo=IST)               # a Friday, after the close
    oldest_data = now - timedelta(days=100)                       # the fake Angel One has 100 days only
    calls = []

    def fake_fetch(index, interval, start, end, allow_empty=False):
        calls.append((start, end))
        assert end - start <= timedelta(days=candle_store.ANGEL_CHUNK_DAYS[interval]) + timedelta(seconds=1), "piece too long"
        return fake_candles(max(start, oldest_data), end)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        con = candle_store.connect(os.path.join(folder, "t.db"))
        quiet = lambda *a, **k: None

        # 1) asking for 365 days: pieces are short, newest first, and it stops when data runs out
        note = candle_store.backfill_one(con, "nifty", "5m", 365, fetch=fake_fetch, now=now, log=quiet)
        assert len(calls) < 365 // 60 + 2, f"did not stop early: {len(calls)} requests"
        assert calls[0][1] == now, "must start from now"
        total = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        days = con.execute("SELECT COUNT(DISTINCT date(ts + 19800, 'unixepoch')) FROM candles").fetchone()[0]
        assert 60 <= days <= 75 and total == days * 75, (days, total)
        print(f"OK: pieces are short and newest first, stopped after {len(calls)} requests ({days} days saved)")

        # 2) running it again changes nothing (no duplicates)
        calls.clear()
        candle_store.backfill_one(con, "nifty", "5m", 365, fetch=fake_fetch, now=now, log=quiet)
        assert con.execute("SELECT COUNT(*) FROM candles").fetchone()[0] == total
        print("OK: a second run adds no duplicates")

        # 3) a failure in the middle keeps what is already saved and says where it stopped
        con2 = candle_store.connect(os.path.join(folder, "t2.db"))
        count = []

        def flaky(index, interval, start, end, allow_empty=False):
            count.append(1)
            if len(count) == 2:
                raise RuntimeError("Access denied because of exceeding access rate")
            return fake_candles(start, end)

        note = candle_store.backfill_one(con2, "sensex", "5m", 150, fetch=flaky, now=now, log=quiet)
        saved = con2.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        assert saved > 0 and "stopped at" in note and "exceeding access rate" in note, note
        print("OK: a failed piece keeps the earlier pieces and reports why it stopped")

        # 4) Yahoo never overwrites a saved candle, Angel One does
        con3 = candle_store.connect(os.path.join(folder, "t3.db"))
        a = fake_candles(now - timedelta(days=1), now)
        candle_store.save_candles(con3, "nifty", "5m", a, replace=True)
        b = a.copy()
        b["close"] = 1.0
        candle_store.save_candles(con3, "nifty", "5m", b, replace=False)
        assert con3.execute("SELECT MAX(close) FROM candles").fetchone()[0] > 50, "Yahoo overwrote Angel One"
        candle_store.save_candles(con3, "nifty", "5m", b, replace=True)
        assert con3.execute("SELECT MAX(close) FROM candles").fetchone()[0] == 1.0
        print("OK: Yahoo keeps saved candles, Angel One can update them")
        for c in (con, con2, con3):
            c.close()

    # 5) rate-limit retry in data_sources: waits, then succeeds; other errors are not retried
    slept = []
    import time as _time
    real_sleep, real_gap, real_waits = _time.sleep, ds.ANGEL_CALL_GAP_SECONDS, ds.ANGEL_RETRY_WAITS
    _time.sleep = lambda s: slept.append(s)
    try:
        class Client:
            n = 0

            def getCandleData(self, params):
                Client.n += 1
                if Client.n < 3:
                    raise Exception("Couldn't parse the JSON response: b'Access denied because of exceeding access rate'")
                return {"status": True, "data": [["2026-11-20T09:15:00+05:30", 1, 2, 0.5, 1.5, 0]]}

        res = ds._angel_get_candles(Client(), {})
        assert res["status"] and Client.n == 3 and sum(slept) >= sum(real_waits[:2])

        class Answers:
            n = 0

            def getCandleData(self, params):          # Angel One also answers with a plain "Too many requests" message
                Answers.n += 1
                if Answers.n == 1:
                    return {"message": "Too many requests", "errorcode": "AB1021", "status": False, "data": None}
                return {"status": True, "data": [["2026-11-20T09:15:00+05:30", 1, 2, 0.5, 1.5, 0]]}

        assert ds._angel_get_candles(Answers(), {})["status"] and Answers.n == 2

        class Broken:
            def getCandleData(self, params):
                raise ValueError("bad token")

        try:
            ds._angel_get_candles(Broken(), {})
            raise AssertionError("should have raised")
        except ValueError:
            pass
    finally:
        _time.sleep = real_sleep
    print("OK: 'too fast' answers are retried after a wait, other errors are not hidden")


if __name__ == "__main__":
    main()
