"""
session_check.py
----------------
A read-only end-of-day check. It writes nothing and places no orders. Run it after the market closes:

    python session_check.py                    # today
    python session_check.py --date 2026-11-25  # another day
    python session_check.py --no-live          # skip the live data check (no Angel One / Yahoo request)
    python session_check.py --auto             # what the 16:00 scheduled task runs (see below)

AUTOMATIC MODE (--auto). The Windows task "AI-Trading-Candle-Collect" runs it as its last step, after the candles,
futures volume and option open interest of the day are saved. It checks the most recent session that has closed,
writes the report to reports/session_check_<date>.txt, and sends a short summary to Telegram (when Telegram is set up).
It does nothing on weekends and holidays, never repeats a day whose report already exists (use --force), and if the PC
was off at 16:00 it checks the last closed session the next time the task runs. It never asks the data source for live
candles (after the close the saved data tells the story) and it places no orders.

It reports, in plain lines marked OK / CHECK / INFO:
  1. whether it was a trading day (weekend, holiday from holidays.csv)
  2. whether live candles are fresh (newest finished candle of NIFTY and SENSEX)
  3. the ideas of that day from the journal: results, alerts sent, ideas skipped by a daily limit
  4. what your own database saved that day (candles, futures volume, option open interest) and the last
     collection runs with their notes
  5. your optional risk limits, and whether the settings for alerts and Angel One are in place
"""

import argparse
import html
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

import telegram_notify as tg
from data_sources import IST
from option_pricer import is_holiday, previous_trading_day

REPORT_DIR = "reports"
CLOSED_AFTER_MINUTES = 15 * 60 + 45        # the session counts as closed and saved from 15:45

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
    # Angel One's index feed has no real candles after 15:10 (the 15:15 and 15:20 candles are missing and the 15:25 one
    # is a single-price closing print that the dashboard drops), so the last REAL 5-minute candle starts at 15:10.
    last_expected = datetime.combine(day, datetime.min.time(), IST).replace(hour=15, minute=30) - timedelta(minutes=20)
    if now.date() == day and now < last_expected:
        lag = (now - newest).total_seconds() / 60 - interval_min
        flag = OK if lag <= 3 else CHECK
        return [(flag, f"{index.upper()}: newest finished candle {newest:%H:%M}, about {max(lag, 0):.0f} min behind.")]
    flag = OK if newest >= last_expected else CHECK
    return [(flag, f"{index.upper()}: newest candle {newest:%H:%M} (the feed's last real candle is {last_expected:%H:%M}).")]


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


def idea_money_lines(df: pd.DataFrame, day: date, lot_size=None, cfg: dict = None) -> list:
    """
    The day's ideas one per line for the Telegram summary, the result in RUPEES (not R) for the lots you would have taken:
        - 10:35 SENSEX 84400 CE 5m: LOSS ₹1,601 Total(1 LOT)
    Rupees come from the saved option prices at the target or the stop loss (a time exit is approximate: "about"), times the
    lot size. LOTS = how many lots your risk budget allows (RISK_CAPITAL and RISK_PER_TRADE_PCT, the position-size helper),
    at least 1; without those settings it is 1 lot. Before brokerage, taxes and the bid-ask gap. Ideas skipped by a daily
    limit are left out. lot_size = function(index) -> lot size or None (default: Angel One's list); cfg = risk settings.
    """
    if df is None or df.empty:
        return []
    mine = df[[pd.Timestamp(t).date() == day for t in df["signal_time"]]]
    mine = mine[~mine["note"].astype(str).str.startswith("Daily limit")]
    mine = mine[mine["status"].isin(["TARGET", "STOP", "TIME", "OPEN", "PENDING"])]
    if mine.empty:
        return []
    if lot_size is None:
        from data_sources import option_lot_size as lot_size
    if cfg is None:
        import risk_limits
        cfg = risk_limits.settings()
    import risk_limits
    lines, total, counted = [], 0.0, 0
    for _, row in mine.sort_values("signal_time").iterrows():
        status = row["status"]
        head = f"- {pd.Timestamp(row['signal_time']):%H:%M} {row['option']} {row['interval']}: "
        if status in ("OPEN", "PENDING"):
            lines.append(head + ("RUNNING" if status == "OPEN" else "WAITING"))
            continue
        try:
            levels = json.loads(row["option_levels"]) if row["option_levels"] else None
            size = lot_size(str(row["index"]).lower())
        except Exception:
            levels, size = None, None
        if not levels or not size:
            lines.append(head + f"{status} (rupees not known: no option price or lot size saved)")
            continue
        approx = False
        if status == "TARGET":
            unit = float(levels["profit_per_unit"])
        elif status == "STOP":
            unit = -float(levels["loss_per_unit"])
        else:                                                  # a time exit ended between the two: an approximation
            gross = float(row["r"] or 0.0)
            unit = gross * float(levels["loss_per_unit"]) if gross < 0 else gross * float(levels["profit_per_unit"]) / 2.0
            approx = True
        sizing = risk_limits.position_size(cfg, float(levels["loss_per_unit"]), size)
        lots = max(1, sizing["lots"]) if sizing else 1
        money = unit * size * lots
        total += money
        counted += 1
        lines.append(head + f"{'PROFIT' if money >= 0 else 'LOSS'} {'about ' if approx else ''}₹{abs(money):,.0f} Total({lots} LOT)")
    if counted:
        lines.append(f"Day total: {'PROFIT' if total >= 0 else 'LOSS'} ₹{abs(total):,.0f} (before brokerage and taxes)")
    return lines


def db_lines(con, day: date) -> list:
    where = "date(ts + 19800, 'unixepoch') = ?"
    lines = []
    for idx in ("nifty", "sensex"):
        n = con.execute(f"SELECT COUNT(*) FROM candles WHERE idx=? AND interval='5m' AND {where}", (idx, day.isoformat())).fetchone()[0]
        lines.append((OK if n >= 70 else CHECK, f"{idx.upper()} 5m candles saved for the day: {n} (a full session has 75; the live feed alone gives 73)."))
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


def build_sections(day: date, now: datetime, db: str, live: bool) -> list:
    sections = [("Trading day", trading_day_lines(day))]
    if day.weekday() < 5 and not is_holiday(day):
        if live:
            sections.append(("Live data", live_lines(day, now)))
        import candle_journal
        sections.append(("Journal", journal_lines(candle_journal._load(), day)))
        if os.path.exists(db):
            con = sqlite3.connect(db)
            try:
                sections.append(("Saved data", db_lines(con, day)))
            finally:
                con.close()
        else:
            sections.append(("Saved data", [(CHECK, f"{db} not found. Run: python candle_store.py collect")]))
    sections.append(("Settings", settings_lines()))
    return sections


def render(sections: list) -> tuple:
    """(text, number of CHECK items)."""
    out = []
    for title, lines in sections:
        out.append(f"\n{title}")
        out.extend(f"  {flag} {text}" for flag, text in lines)
    problems = sum(1 for _, lines in sections for flag, _ in lines if flag == CHECK)
    out.append(f"\n{problems} item(s) to CHECK." if problems else "\nNothing to check.")
    return "\n".join(out), problems


def session_day_to_check(now: datetime):
    """The most recent trading day whose session has closed (today after 15:45, else the trading day before)."""
    today = now.date()
    if today.weekday() < 5 and not is_holiday(today) and now.hour * 60 + now.minute >= CLOSED_AFTER_MINUTES:
        return today
    return previous_trading_day(today - timedelta(days=1))


def report_path(day: date, folder: str = REPORT_DIR) -> str:
    return os.path.join(folder, f"session_check_{day.isoformat()}.txt")


def _bold_money(line: str) -> str:
    """HTML for one idea line: a coloured dot and the LOSS / PROFIT word with its amount in bold, so it stands out."""
    line = html.escape(line, quote=False)
    return re.sub(r"(PROFIT|LOSS)( about)? (\u20b9[\d,]+)",
                  lambda m: ("🟢 " if m.group(1) == "PROFIT" else "🔴 ") + f"<b>{m.group(1)}{m.group(2) or ''} {m.group(3)}</b>", line)


def telegram_summary(day: date, sections: list, problems: int, ideas: list = None, as_html: bool = False) -> str:
    """A short message: the day's ideas and results, and every item that needs a look. as_html: LOSS / PROFIT in bold (Telegram HTML)."""
    lines = [f"End-of-day check, {day:%a %d %b %Y}"]
    for title, section in sections:
        if title == "Journal":
            lines += [text for _, text in section]
    if ideas:
        lines += ["", "Ideas in rupees:"] + ideas
        ideas_at = len(lines) - len(ideas)
    flagged = [text for _, section in [(t, ls) for t, ls in sections] for flag, text in section if flag == CHECK]
    lines.append("")
    lines += ([f"{problems} item(s) to check:"] + [f"- {t}" for t in flagged]) if problems else ["Nothing to check."]
    lines.append("Full report: reports/session_check_" + day.isoformat() + ".txt")
    if as_html:
        lines = [_bold_money(t) if ideas and ideas_at <= k < ideas_at + len(ideas) else html.escape(t, quote=False) for k, t in enumerate(lines)]
    return "\n".join(lines)


def _ideas_in_rupees(day: date) -> list:
    """The rupee lines for the Telegram summary. Never stops the summary: on any problem it is simply left out."""
    try:
        import candle_journal
        return idea_money_lines(candle_journal._load(), day)
    except Exception as error:
        print(f"[session check] ideas in rupees not added: {error}")
        return []


def run_auto(now: datetime, db: str = "market_data.db", folder: str = REPORT_DIR, force: bool = False, send=True):
    """
    The automatic end-of-day check. Returns the report path, or None when there was nothing to do
    (not a trading day, or this day was already reported).
    """
    day = session_day_to_check(now)
    if day.weekday() >= 5 or is_holiday(day):
        print(f"{day} is not a trading day: nothing to check.")
        return None
    path = report_path(day, folder)
    if os.path.exists(path) and not force:
        print(f"{day} was already checked ({path}).")
        return None
    sections = build_sections(day, now, db, live=False)
    text, problems = render(sections)
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"End-of-day check for {day:%a %d %b %Y} (made {now:%d %b %H:%M})\n{text}\n")
    print(text)
    if send and tg.is_configured():
        try:
            ideas = _ideas_in_rupees(day)
            try:                                          # LOSS / PROFIT in bold; plain text if Telegram refuses the markup
                tg.send_message(telegram_summary(day, sections, problems, ideas, as_html=True), parse_mode="HTML")
            except Exception:
                tg.send_message(telegram_summary(day, sections, problems, ideas))
        except Exception as error:                      # the report is saved either way
            print(f"[telegram] summary not sent: {error}")
    return path


def main():
    ap = argparse.ArgumentParser(description="Read-only end-of-day check.")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--no-live", action="store_true", help="do not ask the data source for live candles")
    ap.add_argument("--auto", action="store_true",
                    help="automatic mode: check the last closed session, save a report, send a Telegram summary")
    ap.add_argument("--force", action="store_true", help="with --auto: redo a day that was already reported")
    ap.add_argument("--db", default="market_data.db")
    a = ap.parse_args()
    tg.load_env_file()
    now = datetime.now(IST)
    if a.auto:
        run_auto(now, a.db, force=a.force)
        return
    day = date.fromisoformat(a.date) if a.date else now.date()
    text, _ = render(build_sections(day, now, a.db, live=not a.no_live))
    print(text)


if __name__ == "__main__":
    main()
