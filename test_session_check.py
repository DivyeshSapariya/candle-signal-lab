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

    # --- the automatic mode: which day, report file, Telegram summary, never twice, never on a non-trading day ---
    wed_after = datetime(2026, 11, 25, 16, 0, tzinfo=IST)
    assert sc.session_day_to_check(wed_after) == date(2026, 11, 25)
    assert sc.session_day_to_check(datetime(2026, 11, 25, 11, 0, tzinfo=IST)) == date(2026, 11, 23), "before the close: the last closed day (24 Nov is a holiday)"
    assert sc.session_day_to_check(datetime(2026, 11, 28, 12, 0, tzinfo=IST)) == date(2026, 11, 27), "Saturday: Friday"
    assert sc.session_day_to_check(datetime(2026, 11, 30, 9, 0, tzinfo=IST)) == date(2026, 11, 27), "Monday morning: Friday"
    assert sc.session_day_to_check(datetime(2026, 11, 24, 16, 30, tzinfo=IST)) == date(2026, 11, 23), "a holiday: the day before"
    assert sc.report_path(date(2026, 11, 25), "r").replace("\\", "/") == "r/session_check_2026-11-25.txt"

    import candle_journal as cj
    sent = []
    sc.tg.is_configured = lambda: True
    sc.tg.send_message = lambda text, **kw: sent.append(text)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cj.JOURNAL_FILE = os.path.join(tmp, "j.csv")
        db = os.path.join(tmp, "t.db")
        con = candle_store.connect(db)
        base = int(datetime(2026, 11, 25, 9, 15, tzinfo=IST).timestamp())
        con.executemany("INSERT INTO candles VALUES ('nifty','5m',?,1,1,1,1,0)", [(base + 300 * k,) for k in range(73)])
        con.commit()
        con.close()
        folder = os.path.join(tmp, "reports")
        path = sc.run_auto(wed_after, db, folder)
        assert path and os.path.exists(path) and path.endswith("session_check_2026-11-25.txt")
        text = open(path, encoding="utf-8").read()
        assert "End-of-day check for Wed 25 Nov 2026" in text and "NIFTY 5m candles saved for the day: 73" in text
        assert len(sent) == 1 and sent[0].startswith("End-of-day check, Wed 25 Nov 2026"), sent
        assert "item(s) to check" in sent[0] and "SENSEX 5m candles saved" in sent[0], "problems are listed in the message"
        assert sent[0].rstrip().endswith("session_check_2026-11-25.txt")
        # never twice for the same day; --force repeats it
        assert sc.run_auto(wed_after, db, folder) is None and len(sent) == 1
        assert sc.run_auto(wed_after, db, folder, force=True) and len(sent) == 2
        # a weekend after the last report: Friday is checked once, Saturday later does nothing new
        sat = datetime(2026, 11, 28, 12, 0, tzinfo=IST)
        assert sc.run_auto(sat, db, folder) and sc.run_auto(sat, db, folder) is None
        # Telegram off or failing: the report is still written, nothing crashes
        sc.tg.is_configured = lambda: False
        assert sc.run_auto(datetime(2026, 11, 26, 16, 0, tzinfo=IST), db, folder) and len(sent) == 3
        sc.tg.is_configured = lambda: True

        def boom(text, **kw):
            raise RuntimeError("no network")
        sc.tg.send_message = boom
        assert sc.run_auto(datetime(2026, 11, 30, 16, 0, tzinfo=IST), db, folder)

    # --- the Telegram summary lists the day's ideas in RUPEES (not R) ---
    import json
    lv = json.dumps({"profit_per_unit": 98.6, "loss_per_unit": 42.05})
    cols = ["signal_time", "index", "option", "interval", "status", "r", "note", "option_levels"]
    rows = [
        ["2026-11-25T10:00:00+05:30", "nifty", "NIFTY 25800 CE", "5m", "TARGET", "2.0", "", lv],
        ["2026-11-25T11:00:00+05:30", "sensex", "SENSEX 84400 CE", "5m", "STOP", "-1.0", "", lv],
        ["2026-11-25T12:00:00+05:30", "nifty", "NIFTY 25850 PE", "5m", "TIME", "0.6", "", lv],
        ["2026-11-25T13:00:00+05:30", "nifty", "NIFTY 25800 CE", "5m", "OPEN", "", "", lv],
        ["2026-11-25T13:30:00+05:30", "nifty", "NIFTY 25800 CE", "5m", "STOP", "-1.0", "", ""],
        ["2026-11-25T14:00:00+05:30", "nifty", "NIFTY 25800 CE", "5m", "SKIPPED", "", "Daily limit: x", lv],
        ["2026-11-24T10:00:00+05:30", "nifty", "NIFTY 25800 CE", "5m", "TARGET", "2.0", "", lv],        # another day
    ]
    sizes = lambda i: {"nifty": 65, "sensex": 20}[i]
    off = {"capital": None, "risk_pct": None, "max_loss_r": None, "max_ideas": None}
    money = sc.idea_money_lines(pd.DataFrame(rows, columns=cols), date(2026, 11, 25), sizes, off)
    text = " | ".join(money)
    assert len(money) == 6, text
    assert "- 10:00 NIFTY 25800 CE 5m: PROFIT ₹6,409 Total(1 LOT)" in text, text
    assert "- 11:00 SENSEX 84400 CE 5m: LOSS ₹841 Total(1 LOT)" in text, text
    assert "12:00 NIFTY 25850 PE 5m: PROFIT about ₹1,923 Total(1 LOT)" in text and "13:00 NIFTY 25800 CE 5m: RUNNING" in text, text
    assert "STOP (rupees not known" in text and "SKIPPED" not in text and "10:00 NIFTY 25800 CE 5m: PROFIT ₹6,409" in text
    assert money[-1] == "Day total: PROFIT ₹7,491 (before brokerage and taxes)", money[-1]
    # with the position-size helper: lots that fit the budget (loss per lot: NIFTY 2,733, SENSEX 841); at least 1
    cfg = {"capital": 30000, "risk_pct": 6, "max_loss_r": None, "max_ideas": None}
    sized = sc.idea_money_lines(pd.DataFrame(rows, columns=cols), date(2026, 11, 25), sizes, cfg)
    assert "- 10:00 NIFTY 25800 CE 5m: PROFIT ₹6,409 Total(1 LOT)" in sized, sized          # budget 1,800 < 2,733: still 1
    assert "- 11:00 SENSEX 84400 CE 5m: LOSS ₹1,682 Total(2 LOT)" in sized, sized          # 1,800 / 841 = 2 lots
    assert sc.idea_money_lines(pd.DataFrame(columns=cols), date(2026, 11, 25)) == []
    assert sc.idea_money_lines(pd.DataFrame(rows, columns=cols), date(2026, 12, 1)) == []
    msg = sc.telegram_summary(date(2026, 11, 25), [("Journal", [("INFO", "Ideas: 1 stop")])], 0, money)
    assert "Ideas in rupees:" in msg and "PROFIT" in msg and msg.index("Ideas: 1 stop") < msg.index("Ideas in rupees:") < msg.index("Nothing to check.")
    hm = sc.telegram_summary(date(2026, 11, 25), [("Journal", [("INFO", "a < b")])], 0, money, as_html=True)
    assert "🟢 <b>PROFIT ₹6,409</b> Total(1 LOT)" in hm and "🔴 <b>LOSS ₹841</b> Total(1 LOT)" in hm, hm
    assert "<b>PROFIT about ₹1,923</b>" in hm and "<b>PROFIT ₹7,491</b> (before" in hm and "a &lt; b" in hm, "escaped, only the money is bold"
    assert hm.count("<b>") == hm.count("</b>") == 4 and "RUNNING" in hm and "<b>RUNNING" not in hm
    assert "<b>" not in msg, "the plain text has no markup"
    assert "Ideas in rupees" not in sc.telegram_summary(date(2026, 11, 25), [("Journal", [("INFO", "x")])], 0), "unchanged without ideas"

    print("OK: trading day, data freshness, journal summary, saved-data and collection-run checks, automatic mode")


if __name__ == "__main__":
    main()
