"""
candle_store.py
---------------
Collects candles into a local database so you build your OWN long history.
(Yahoo only keeps about 60 days of 5-minute candles and 7 days of 1-minute candles.)

Uses SQLite from Python's standard library (no extra install). File: market_data.db

Run (once a day, after the market closes, is enough):
    python candle_store.py collect                    # NIFTY + SENSEX, 1m / 5m / 15m, plus India VIX
    python candle_store.py collect --index nifty --interval 5m
    python candle_store.py status                     # what is stored
    python candle_store.py export --index nifty --interval 5m --out nifty_5m.csv

Safe to run many times: a candle that is already saved is simply updated, never duplicated.
Only FINISHED candles are saved (the running candle is dropped).

Use stored history in your own code:
    from candle_store import load_candles
    df = load_candles("nifty", "5m")      # same format as data_sources.get_candles()

Note: 1-minute candles only reach back 7 days on Yahoo, so run this at least once a week
(daily is better) or you will lose minutes that cannot be downloaded later.

Longer history from Angel One (needs the ANGEL_* values in .env, read-only, no orders):
    python candle_store.py backfill --days 365                 # NIFTY + SENSEX, 5m and 15m
    python candle_store.py backfill --days 365 --index nifty --interval 5m
It downloads in small pieces with pauses (Angel One refuses fast requests), saves each piece
straight away, and can be run again safely (it re-downloads the period and updates candles, never duplicates).
"""

import argparse
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from data_sources import IST, INTERVAL_MINUTES, drop_forming_candle, fetch_angel_range, fetch_yahoo

DB_FILE = "market_data.db"
INDEXES = ("nifty", "sensex")
# Longest time one Angel One candle request may cover (their limits are larger; this keeps a margin)
ANGEL_CHUNK_DAYS = {"1m": 25, "5m": 60, "15m": 100}
BACKFILL_INTERVALS = ("5m", "15m")      # 1m is only downloaded when you ask for it with --interval 1m
EMPTY_CHUNKS_TO_STOP = 3                # this many empty pieces in a row = Angel One has no older data
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    idx      TEXT    NOT NULL,      -- nifty / sensex
    interval TEXT    NOT NULL,      -- 1m / 5m / 15m
    ts       INTEGER NOT NULL,      -- candle START time, seconds since 1970 (UTC)
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (idx, interval, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vix_daily (
    day   TEXT PRIMARY KEY,         -- YYYY-MM-DD
    close REAL
);
CREATE TABLE IF NOT EXISTS collect_log (
    run_at TEXT, idx TEXT, interval TEXT, fetched INTEGER, first_ts TEXT, last_ts TEXT, note TEXT
);
"""


def connect(path: str = DB_FILE) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _epoch(index: pd.DatetimeIndex) -> list:
    return [int(ts.timestamp()) for ts in index]


def save_candles(con: sqlite3.Connection, index: str, interval: str, df: pd.DataFrame,
                 replace: bool = True) -> int:
    """
    Save finished candles. Returns the number of rows given.
    replace=True updates a candle that is already saved. replace=False keeps the saved one
    (Yahoo uses this, so it never overwrites the Angel One history).
    """
    if df.empty:
        return 0
    rows = list(zip(
        [index] * len(df), [interval] * len(df), _epoch(df.index),
        df["open"].astype(float), df["high"].astype(float), df["low"].astype(float),
        df["close"].astype(float), df["volume"].fillna(0).astype(float),
    ))
    with con:
        con.executemany(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def collect_one(con, index: str, interval: str) -> str:
    note = ""
    try:
        # the longest history Yahoo allows for this candle size
        period = {"1m": "7d", "5m": "58d", "15m": "58d"}[interval]
        df = drop_forming_candle(fetch_yahoo(index, interval, period=period), interval)
        before = con.execute("SELECT COUNT(*) FROM candles WHERE idx=? AND interval=?",
                             (index, interval)).fetchone()[0]
        save_candles(con, index, interval, df, replace=False)
        after = con.execute("SELECT COUNT(*) FROM candles WHERE idx=? AND interval=?",
                            (index, interval)).fetchone()[0]
        first, last = (df.index[0].isoformat(), df.index[-1].isoformat()) if len(df) else ("", "")
        note = f"fetched {len(df)}, new {after - before}, total {after}, up to {last[:16]}"
        con.execute("INSERT INTO collect_log VALUES (?,?,?,?,?,?,?)",
                    (datetime.now(IST).isoformat(timespec="seconds"), index, interval,
                     len(df), first, last, "ok"))
        con.commit()
    except Exception as error:                     # one failure must not stop the others
        note = f"FAILED: {error}"
        con.execute("INSERT INTO collect_log VALUES (?,?,?,?,?,?,?)",
                    (datetime.now(IST).isoformat(timespec="seconds"), index, interval,
                     0, "", "", f"error: {error}"[:300]))
        con.commit()
    return note


def backfill_one(con, index: str, interval: str, days: int, fetch=None, pause=None,
                 now: datetime = None, log=print) -> str:
    """
    Download the last `days` days from Angel One, newest piece first, saving each piece at once.
    Stops early after a few empty pieces in a row (no older data exists). Safe to run again.
    """
    fetch = fetch or fetch_angel_range
    now = now or datetime.now(IST)
    step = timedelta(days=ANGEL_CHUNK_DAYS[interval])
    oldest = now - timedelta(days=days)
    before = con.execute("SELECT COUNT(*) FROM candles WHERE idx=? AND interval=?", (index, interval)).fetchone()[0]

    end, pieces, empty_run, fetched, note = now, 0, 0, 0, "ok"
    while end > oldest:
        start = max(oldest, end - step)
        try:
            df = fetch(index, interval, start, end, allow_empty=True)
        except Exception as error:                         # keep what is saved, say what stopped it
            note = f"stopped at {start:%d %b %Y}: {error}"[:300]
            break
        pieces += 1
        if len(df):
            df = drop_forming_candle(df, interval)
            fetched += save_candles(con, index, interval, df, replace=True)
            empty_run = 0
        else:
            empty_run += 1
        log(f"    {index.upper()} {interval} {start:%d %b %Y} -> {end:%d %b %Y}: {len(df)} candles")
        if empty_run >= EMPTY_CHUNKS_TO_STOP:
            note = "no older data available"
            break
        end = start
        if pause:
            pause()

    after = con.execute("SELECT COUNT(*) FROM candles WHERE idx=? AND interval=?", (index, interval)).fetchone()[0]
    con.execute("INSERT INTO collect_log VALUES (?,?,?,?,?,?,?)",
                (datetime.now(IST).isoformat(timespec="seconds"), index, interval, fetched,
                 oldest.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), f"backfill {note}"[:300]))
    con.commit()
    return f"{pieces} requests, fetched {fetched}, new {after - before}, total {after} ({note})"


def collect_vix(con) -> str:
    from option_pricer import get_vix_daily
    series = get_vix_daily(period="2y")
    if series is None or len(series) == 0:
        return "India VIX: not available right now"
    rows = [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in series.items()]
    with con:
        con.executemany("INSERT OR REPLACE INTO vix_daily VALUES (?,?)", rows)
    total = con.execute("SELECT COUNT(*) FROM vix_daily").fetchone()[0]
    return f"India VIX: fetched {len(rows)} days, total {total}"


def load_candles(index: str, interval: str, path: str = DB_FILE) -> pd.DataFrame:
    """Stored candles as a table (open/high/low/close/volume, IST time index)."""
    con = connect(path)
    try:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE idx=? AND interval=? ORDER BY ts", con, params=(index, interval))
    finally:
        con.close()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), unit="s", utc=True)).tz_convert(IST)
    df.index.name = "datetime"
    return df


def load_vix(path: str = DB_FILE):
    """Stored daily India VIX closes as a Series (same shape as option_pricer.get_vix_daily)."""
    con = connect(path)
    try:
        df = pd.read_sql_query("SELECT day, close FROM vix_daily ORDER BY day", con)
    finally:
        con.close()
    if df.empty:
        return None
    return pd.Series(df["close"].values, index=pd.to_datetime(df["day"]))


def status(con) -> None:
    rows = con.execute(
        "SELECT idx, interval, COUNT(*), MIN(ts), MAX(ts) FROM candles "
        "GROUP BY idx, interval ORDER BY idx, interval").fetchall()
    if not rows:
        print("Nothing stored yet. Run:  python candle_store.py collect")
    for idx, interval, n, lo, hi in rows:
        a = datetime.fromtimestamp(lo, timezone.utc).astimezone(IST)
        b = datetime.fromtimestamp(hi, timezone.utc).astimezone(IST)
        days = con.execute("SELECT COUNT(DISTINCT date(ts + 19800, 'unixepoch')) FROM candles "
                           "WHERE idx=? AND interval=?", (idx, interval)).fetchone()[0]
        print(f"{idx.upper():<7}{interval:<4} {n:>7} candles, {days:>3} days  "
              f"{a:%d %b %Y %H:%M} -> {b:%d %b %Y %H:%M}")
    v = con.execute("SELECT COUNT(*), MIN(day), MAX(day) FROM vix_daily").fetchone()
    if v[0]:
        print(f"INDIAVIX daily {v[0]:>5} days    {v[1]} -> {v[2]}")
    last = con.execute("SELECT run_at, idx, interval, note FROM collect_log "
                       "ORDER BY rowid DESC LIMIT 1").fetchone()
    if last:
        print(f"Last run: {last[0]}  ({last[1]} {last[2]}: {last[3]})")


def main():
    p = argparse.ArgumentParser(description="Collect candles into a local SQLite database.")
    p.add_argument("action", choices=["collect", "backfill", "status", "export"])
    p.add_argument("--days", type=int, default=365, help="backfill: how many days back (default 365)")
    p.add_argument("--index", choices=list(INDEXES), help="only this index (default: both)")
    p.add_argument("--interval", choices=list(INTERVAL_MINUTES), help="only this candle size (default: all)")
    p.add_argument("--no-vix", action="store_true", help="do not collect India VIX")
    p.add_argument("--db", default=DB_FILE)
    p.add_argument("--out", help="export: CSV file to write")
    args = p.parse_args()

    con = connect(args.db)
    if args.action == "status":
        status(con)
    elif args.action == "export":
        if not (args.index and args.interval and args.out):
            p.error("export needs --index, --interval and --out")
        df = load_candles(args.index, args.interval, args.db)
        df.to_csv(args.out)
        print(f"Wrote {len(df)} candles to {args.out}")
    elif args.action == "backfill":
        import telegram_notify
        telegram_notify.load_env_file()          # reads the ANGEL_* values from the private .env
        for idx in ([args.index] if args.index else INDEXES):
            for interval in ([args.interval] if args.interval else BACKFILL_INTERVALS):
                print(f"{idx.upper()} {interval}: downloading up to {args.days} days from Angel One...")
                print(f"  -> {backfill_one(con, idx, interval, args.days)}")
        print()
        status(con)
    else:
        for idx in ([args.index] if args.index else INDEXES):
            for interval in ([args.interval] if args.interval else INTERVAL_MINUTES):
                print(f"{idx.upper():<7}{interval:<4} {collect_one(con, idx, interval)}")
        if not args.no_vix:
            print(collect_vix(con))
        print()
        status(con)
    con.close()


if __name__ == "__main__":
    main()
