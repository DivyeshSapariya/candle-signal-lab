"""
test_option_chain.py
--------------------
Checks the option-chain saving with a FAKE Angel One (no login, no internet, no real database).

Run:   python test_option_chain.py
"""

import os
import tempfile
from datetime import date, datetime, timedelta

import pandas as pd

import candle_store
import data_sources as ds
from data_sources import IST


def fake_master():
    rows = []
    for expiry in ("01Dec2026", "08Dec2026"):
        for strike in range(24800, 25900, 50):
            for side in ("CE", "PE"):
                rows.append({"name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": expiry,
                             "strike": f"{strike * 100}.000000", "symbol": f"NIFTY{expiry[:2]}DEC26{strike}{side}",
                             "token": f"{expiry[:2]}{strike}{side}"})
    rows.append({"name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "FUTIDX", "expiry": "29Dec2026",
                 "strike": "-1.000000", "symbol": "NIFTY29DEC26FUT", "token": "1"})
    rows.append({"name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": "17Nov2026",
                 "strike": "2535000.000000", "symbol": "NIFTY17NOV2625350CE", "token": "old"})   # already expired
    return rows


def fake_history(contract, start, end):
    rows = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if day.weekday() < 5:
            for k in range(75):
                ts = day.replace(hour=9, minute=15) + timedelta(minutes=5 * k)
                if start <= ts <= end:
                    rows.append((ts, 100.0 + k, 10.0 * k, 1000.0 + k))
        day += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["datetime", "close", "volume", "oi"]).set_index("datetime")
    return df


def main():
    ds._scrip_master.update(date=datetime.now(IST).strftime("%Y%m%d"), rows=fake_master())
    now = datetime(2026, 11, 24, 16, 0, tzinfo=IST)

    # contracts: nearest expiry only, still-listed only, ATM +/- 5 strikes, both sides
    cs = ds.angel_option_contracts("nifty", 25349.65, 5, now)
    assert {c["expiry"] for c in cs} == {date(2026, 12, 1)}, "nearest expiry only (the 17 Nov contract is gone)"
    assert sorted({c["strike"] for c in cs}) == [25100, 25150, 25200, 25250, 25300, 25350, 25400, 25450, 25500, 25550, 25600], cs
    assert len(cs) == 22 and {c["side"] for c in cs} == {"CE", "PE"}
    # a contract that expires TODAY is still taken after the close, the next one is not
    today = ds.angel_option_contracts("nifty", 25350, 1, datetime(2026, 12, 1, 16, 0, tzinfo=IST))
    assert {c["expiry"] for c in today} == {date(2026, 12, 1)}
    # near the edge of the list it does not fail
    assert ds.angel_option_contracts("nifty", 24000, 5, now)[0]["strike"] == 24800

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "t.db")
        con = candle_store.connect(path)

        # no index candles saved -> says so, saves nothing, does not crash
        assert "run collect first" in candle_store.backfill_option_chain(con, "nifty", now=now)

        out = candle_store.backfill_option_chain(con, "nifty", days=2, spot=25350, now=now,
                                                 fetch=fake_history, log=lambda *_: None)
        n = con.execute("SELECT COUNT(*) FROM option_oi").fetchone()[0]
        assert n > 0 and "ok" in out, out
        assert con.execute("SELECT COUNT(DISTINCT strike) FROM option_oi").fetchone()[0] == 11
        # running again changes nothing (no duplicates)
        candle_store.backfill_option_chain(con, "nifty", days=2, spot=25350, now=now, fetch=fake_history,
                                           log=lambda *_: None)
        assert con.execute("SELECT COUNT(*) FROM option_oi").fetchone()[0] == n, "duplicates were saved"

        # the unfinished candle is never saved
        late = datetime(2026, 11, 24, 10, 7, tzinfo=IST)
        con2 = candle_store.connect(os.path.join(tmp, "t2.db"))
        candle_store.backfill_option_chain(con2, "nifty", days=1, spot=25350, now=late, fetch=fake_history,
                                           log=lambda *_: None)
        last = con2.execute("SELECT MAX(ts) FROM option_oi").fetchone()[0]
        assert datetime.fromtimestamp(last, IST) + timedelta(minutes=5) <= late, "saved a running candle"

        # one failing contract does not stop the others, and the failure is reported
        def flaky(contract, start, end):
            if contract["strike"] == 25350 and contract["side"] == "CE":
                raise RuntimeError("boom")
            return fake_history(contract, start, end)
        con3 = candle_store.connect(os.path.join(tmp, "t3.db"))
        out = candle_store.backfill_option_chain(con3, "nifty", days=1, spot=25350, now=now, fetch=flaky,
                                                 log=lambda *_: None)
        assert "1 contracts failed" in out and con3.execute("SELECT COUNT(*) FROM option_oi").fetchone()[0] > 0

        # missing open interest is stored as empty, not as 0
        def no_oi(contract, start, end):
            df = fake_history(contract, start, end)
            df["oi"] = float("nan")
            return df
        con4 = candle_store.connect(os.path.join(tmp, "t4.db"))
        candle_store.backfill_option_chain(con4, "nifty", days=1, spot=25350, now=now, fetch=no_oi,
                                           log=lambda *_: None)
        assert con4.execute("SELECT COUNT(*) FROM option_oi WHERE oi IS NOT NULL").fetchone()[0] == 0

        # loading gives a time-indexed table, and status prints without error
        df = candle_store.load_option_chain("nifty", path)
        assert len(df) == n and df.index.tz is not None and set(df["side"]) == {"CE", "PE"}
        candle_store.status(con)
        for c in (con, con2, con3, con4):
            c.close()

    print("OK: option contracts (nearest expiry, ATM +/- strikes), saving without duplicates, no running candle, failures isolated")


if __name__ == "__main__":
    main()
