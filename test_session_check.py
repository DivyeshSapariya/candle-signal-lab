"""
test_session_check.py
---------------------
Checks the lines session_check.py prints, with made-up data (no internet, no real files).

Run:   python test_session_check.py
"""

import os
import sqlite3
import tempfile
from datetime import date, datetime

import pandas as pd

import candle_store
import session_check as sc
from data_sources import IST


def flags(lines):
    return [f.strip() for f, _ in lines]


def main():
    # --- trading day ---
    assert flags(sc.trading_day_lines(date(2026, 11, 28))) == ["INFO"]            # Saturday
    assert flags(sc.trading_day_lines(date(2026, 11, 24))) == ["INFO"]            # holiday in holidays.csv
    assert flags(sc.trading_day_lines(date(2026, 11, 25))) == ["OK"]

    # --- freshness: during the session, after it, stale, empty ---
    day = date(2026, 11, 25)
    at = lambda h, m: datetime(2026, 11, 25, h, m, tzinfo=IST)
    assert flags(sc.freshness_lines("nifty", at(11, 0), at(11, 7), day)) == ["OK"]      # 2 min behind
    assert flags(sc.freshness_lines("nifty", at(10, 0), at(11, 7), day)) == ["CHECK"]   # 62 min behind
    assert flags(sc.freshness_lines("nifty", at(15, 10), at(16, 5), day)) == ["OK"]     # the feed's last real candle
    assert flags(sc.freshness_lines("nifty", at(15, 25), at(16, 5), day)) == ["OK"]     # a longer feed is fine too
    assert flags(sc.freshness_lines("nifty", at(14, 0), at(16, 5), day)) == ["CHECK"]   # stopped early
    assert flags(sc.freshness_lines("nifty", datetime(2026, 11, 23, 15, 25, tzinfo=IST), at(11, 0), day)) == ["CHECK"]
    assert flags(sc.freshness_lines("nifty", None, at(11, 0), day)) == ["CHECK"]

    # --- journal ---
    cols = ["signal_time", "status", "r", "alerts", "note"]
    rows = [
        ("2026-11-25T10:20:00+05:30", "TARGET", "2.0", "new,result", ""),
        ("2026-11-25T11:10:00+05:30", "STOP", "-1.0", "new,result", ""),
        ("2026-11-25T13:00:00+05:30", "SKIPPED", "", "", "Daily limit: 2 ideas already today (maximum 2)"),
        ("2026-11-25T14:00:00+05:30", "PENDING", "", "new", ""),
        ("2026-11-20T10:20:00+05:30", "TARGET", "2.0", "new,result", ""),                 # another day
    ]
    df = pd.DataFrame(rows, columns=cols)
    text = " | ".join(t for _, t in sc.journal_lines(df, day))
    assert "+0.80R" in text, text                      # (2.0 - 1.0) - 2 x 0.1
    assert "3 new-idea, 2 result" in text and "1 idea(s) were skipped by a daily risk limit" in text
    assert "still waiting" in text
    assert flags(sc.journal_lines(df, date(2026, 11, 26))) == ["INFO"]
    assert flags(sc.journal_lines(pd.DataFrame(columns=cols), day)) == ["INFO"]

    # --- saved data ---
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "t.db")
        con = candle_store.connect(path)
        base = int(datetime(2026, 11, 25, 9, 15, tzinfo=IST).timestamp())
        con.executemany("INSERT INTO candles VALUES ('nifty','5m',?,1,1,1,1,0)", [(base + 300 * k,) for k in range(75)])
        con.executemany("INSERT INTO candles VALUES ('sensex','5m',?,1,1,1,1,0)", [(base + 300 * k,) for k in range(40)])
        con.execute("INSERT INTO option_oi VALUES ('nifty','2026-12-01',25300,'CE',?,1,1,100)", (base,))
        con.execute("INSERT INTO collect_log VALUES ('2026-11-25T16:00:05+05:30','nifty','5m',75,'a','b','ok')")
        con.execute("INSERT INTO collect_log VALUES ('2026-11-25T16:02:00+05:30','sensex','5m',0,'a','b','stopped at 20 Nov: boom')")
        con.commit()
        lines = sc.db_lines(con, day)
        con.close()
    by_text = {t.split(":")[0]: f.strip() for f, t in lines}
    assert by_text["NIFTY 5m candles saved for the day"] == "OK"
    assert by_text["SENSEX 5m candles saved for the day"] == "CHECK"                  # only 40 of 75
    assert any(f.strip() == "CHECK" and "stopped" in t for f, t in lines), "a failed collection run is flagged"
    assert any("NIFTY option open-interest rows saved: 1" in t for _, t in lines)

    # --- settings lines run without any env ---
    assert sc.settings_lines()

    print("OK: trading day, data freshness, journal summary, saved-data and collection-run checks")


if __name__ == "__main__":
    main()
