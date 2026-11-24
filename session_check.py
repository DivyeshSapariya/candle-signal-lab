"""
session_check.py
----------------
A read-only end-of-day check. It writes nothing and places no orders. Run it after the market closes:

    python session_check.py                    # today
    python session_check.py --date 2026-11-25  # another day
    python session_check.py --no-live          # skip the live data check (no Angel One / Yahoo request)

It reports, in plain lines marked OK / CHECK / INFO:
  1. whether it was a trading day (weekend, holiday from holidays.csv)
  2. whether live candles are fresh (newest finished candle of NIFTY and SENSEX)
  3. the ideas of that day from the journal: results, alerts sent, ideas skipped by a daily limit
  4. what your own database saved that day (candles, futures volume, option open interest) and the last
     collection runs with their notes
  5. your optional risk limits, and whether the settings for alerts and Angel One are in place
"""

import argparse
import os
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

import telegram_notify as tg
from data_sources import IST
from option_pricer import is_holiday

OK, CHECK, INFO = "OK   ", "CHECK", "INFO "


def trading_day_lines(day: date) -> list:
    if day.weekday() >= 5:
        return [(INFO, f"{day:%a %d %b %Y} is a weekend: no session, nothing to check.")]
    if is_holiday(day):
        return [(INFO, f"{day:%a %d %b %Y} is a holiday in holidays.csv: no session, nothing to check.")]
    return [(OK, f"{day:%a %d %b %Y} is a trading day.")]


def freshness_lines(index: str, newest, now: datetime, day: date, interval_min: int = 5) -> list:
    """newest = start time of the newest finished candle (or None)."""
    if newest is None:
        return [(CHECK, f"{index.upper()}: no candles came back.")]
    newest = newest.astimezone(IST)
    if newest.date() != day:
        return [(CHECK, f"{index.upper()}: newest candle is from {newest:%d %b %H:%M}, not {day:%d %b}.")]
    last_expected = datetime.combine(day, datetime.min.time(), IST).replace(hour=15, minute=30) - timedelta(minutes=interval_min)
    if now.date() == day and now < last_expected:
        lag = (now - newest).total_seconds() / 60 - interval_min
        flag = OK if lag <= 3 else CHECK
        return [(flag, f"{index.upper()}: newest finished candle {newest:%H:%M}, about {max(lag, 0):.0f} min behind.")]
    flag = OK if newest >= last_expected else CHECK
    return [(flag, f"{index.upper()}: newest candle {newest:%H:%M} (the last one of the session is {last_expected:%H:%M}).")]


def journal_lines(df: pd.DataFrame, day: date) -> list:
    if df is None or df.empty:
        return [(INFO, "Journal: no ideas saved yet.")]
    mine = df[[pd.Timestamp(t).date() == day for t in df["signal_time"]]]
    if mine.empty:
        return [(INFO, "Journal: no ideas that day (normal: the filters are strict).")]
    lines = []
    by = mine["status"].value_counts().to_dict()
    lines.append((INFO, "Ideas: " + ", ".join(f"{n} {s.lower()}" for s, n in sorted(by.items()))))
    finished = mine[mine["status"].isin(["TARGET", "STOP", "TIME"])]
    if len(finished):
        net = sum(float(r) for r in finished["r"]) - 0.1 * len(finished)
        lines.append((INFO, f"Net result of finished ideas: {net:+.2f}R (after 0.1R cost each)."))
    alerts = mine["alerts"].astype(str)
    sent_new, sent_result = int(alerts.str.contains("new").sum()), int(alerts.str.contains("result").sum())
    lines.append((INFO, f"Telegram alerts recorded: {sent_new} new-idea, {sent_result} result."))
    limit_skipped = int(mine["note"].astype(str).str.startswith("Daily limit").sum())
    if limit_skipped:
        lines.append((INFO, f"{limit_skipped} idea(s) were skipped by a daily risk limit."))
    waiting = int(mine["status"].isin(["PENDING", "OPEN"]).sum())
    if waiting:
        lines.append((CHECK, f"{waiting} idea(s) still waiting / running: results are checked while the dashboard is open."))
    return lines


def db_lines(con, day: date) -> list:
    where = "date(ts + 19800, 'unixepoch') = ?"
    lines = []
    for idx in ("nifty", "sensex"):
        n = con.execute(f"SELECT COUNT(*) FROM candles WHERE idx=? AND interval='5m' AND {where}", (idx, day.isoformat())).fetchone()[0]
        lines.append((OK if n >= 70 else CHECK, f"{idx.upper()} 5m candles saved for the day: {n} (a full session has 75)."))
    for idx in ("nifty_fut", "sensex_fut"):
        n = con.execute(f"SELECT COUNT(*) FROM candles WHERE idx=? AND interval='5m' AND {where}", (idx, day.isoformat())).fetchone()[0]
        lines.append((OK if n else INFO, f"{idx.upper()} (futures volume) 5m candles saved: {n}."))
    for idx in ("nifty", "sensex"):
        n = con.execute(f"SELECT COUNT(*) FROM option_oi WHERE idx=? AND {where}", (idx, day.isoformat())).fetchone()[0]
        lines.append((OK if n else INFO, f"{idx.upper()} option open-interest rows saved: {n}."))
    runs = con.execute("SELECT run_at, idx, interval, fetched, note FROM collect_log WHERE run_at LIKE ? "
                       "ORDER BY rowid DESC LIMIT 6", (day.isoformat() + "%",)).fetchall()
    if not runs:
        lines.append((INFO, "No collection run recorded that day (the 16:00 task, or run candle_store.py by hand)."))
    for run_at, idx, interval, fetched, note in runs:
        bad = any(w in str(note).lower() for w in ("fail", "error", "stopped"))
        lines.append((CHECK if bad else INFO, f"Collection {run_at[11:19]} {idx} {interval}: {fetched} rows, {note}"))
    return lines


def settings_lines() -> list:
    import risk_limits
    lines = [(OK if tg.is_configured() else INFO, "Telegram alerts are set up." if tg.is_configured()
              else "Telegram alerts are OFF (no token / chat id in .env).")]
    angel = all(os.environ.get(k) for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))
    lines.append((OK if angel else CHECK, "Angel One values are in .env." if angel
                  else "Angel One values are missing: the dashboard uses delayed Yahoo data."))
    cfg = risk_limits.settings()
    lines.append((INFO, "Daily limits: " + (f"max loss {cfg['max_loss_r']}R, max ideas {cfg['max_ideas']}" if risk_limits.limits_on(cfg) else "off")
                  + "; position-size helper: " + ("on" if risk_limits.sizing_on(cfg) else "off") + "."))
    return lines


def live_lines(day: date, now: datetime) -> list:
    from data_sources import get_candles
    keys = ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
    source = "angel" if all(os.environ.get(k) for k in keys) else "yahoo"      # same rule as the dashboard
    lines = [(INFO, f"Live data source: {source}.")]
    for index in ("nifty", "sensex"):
        try:
            df = get_candles(source, index, "5m", include_forming=False)
            lines += freshness_lines(index, df.index[-1] if len(df) else None, now, day)
        except Exception as error:
            lines.append((CHECK, f"{index.upper()}: live candles failed: {str(error)[:120]}"))
    return lines


def main():
    ap = argparse.ArgumentParser(description="Read-only end-of-day check.")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--no-live", action="store_true", help="do not ask the data source for live candles")
    ap.add_argument("--db", default="market_data.db")
    a = ap.parse_args()
    tg.load_env_file()
    now = datetime.now(IST)
    day = date.fromisoformat(a.date) if a.date else now.date()

    sections = [("Trading day", trading_day_lines(day))]
    if day.weekday() < 5 and not is_holiday(day):
        if not a.no_live:
            sections.append(("Live data", live_lines(day, now)))
        import candle_journal
        sections.append(("Journal", journal_lines(candle_journal._load(), day)))
        if os.path.exists(a.db):
            con = sqlite3.connect(a.db)
            try:
                sections.append(("Saved data", db_lines(con, day)))
            finally:
                con.close()
        else:
            sections.append(("Saved data", [(CHECK, f"{a.db} not found. Run: python candle_store.py collect")]))
    sections.append(("Settings", settings_lines()))
    for title, lines in sections:
        print(f"\n{title}")
        for flag, text in lines:
            print(f"  {flag} {text}")
    problems = sum(1 for _, lines in sections for flag, _ in lines if flag == CHECK)
    print(f"\n{problems} item(s) to CHECK." if problems else "\nNothing to check.")


if __name__ == "__main__":
    main()
