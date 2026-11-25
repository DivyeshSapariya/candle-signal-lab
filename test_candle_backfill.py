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

    # 4b) futures: saved under <index>_fut, only for the time each contract was the nearest one
    from datetime import date
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        con4 = candle_store.connect(os.path.join(folder, "f.db"))
        windows = []

        def fut_fetch(key, interval, start, end, allow_empty=False):
            windows.append((start, end))
            return fake_candles(start, end)

        contracts = [{"expiry": date(2026, 11, 26), "symbol": "SENSEX26NOVFUT", "token": "1", "exchange": "BFO"},
                     {"expiry": date(2026, 12, 31), "symbol": "SENSEX26DECFUT", "token": "2", "exchange": "BFO"}]
        note = candle_store.backfill_futures(con4, "sensex", "5m", days=120,
                                             contracts=contracts, now=datetime(2026, 11, 23, 16, 0, tzinfo=IST),
                                             log=lambda *a, **k: None, fetch=fut_fetch)
        first = min(w[0] for w in windows)
        assert first == datetime(2026, 10, 30, tzinfo=IST), first          # day after the Oct 29 expiry
        assert all(w[1] <= datetime(2026, 11, 23, 16, 0, tzinfo=IST) for w in windows)
        keys = {r[0] for r in con4.execute("SELECT DISTINCT idx FROM candles")}
        assert keys == {"sensex_fut"}, keys
        assert "SENSEX26NOVFUT" in note and "SENSEX26DECFUT" not in note, note   # Dec is not nearest yet
        assert len(candle_store.load_futures("sensex", "5m", os.path.join(folder, "f.db"))) > 0
        con4.close()
    print("OK: futures saved as <index>_fut, only while each contract was the nearest")

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


def test_one_at_a_time():
    """Many dashboard requests at once must reach Angel One one by one, with a pause between them."""
    import threading
    import time

    old_gap = ds.ANGEL_CALL_GAP_SECONDS
    ds.ANGEL_CALL_GAP_SECONDS = 0.05
    stamps, running, overlap = [], [0], []

    def call():
        running[0] += 1
        overlap.append(running[0])
        stamps.append(time.time())
        time.sleep(0.01)
        running[0] -= 1
        return {"status": True, "data": []}

    try:
        threads = [threading.Thread(target=lambda: ds._angel_request(call)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        ds.ANGEL_CALL_GAP_SECONDS = old_gap
    assert max(overlap) == 1, "two requests reached Angel One at the same time"
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 8 and min(gaps) >= 0.045, f"requests too close together: {min(gaps):.3f}s"
    print("OK: 8 simultaneous requests are sent one at a time with a pause between them")


if __name__ == "__main__":
    test_one_at_a_time()
    main()
