"""
data_sources.py
---------------
Gets NIFTY50 / SENSEX candles from FREE sources.

Source 1: "yahoo"  -> no account, no API key. Easiest. Data can be a little delayed.
Source 2: "angel"  -> Angel One SmartAPI. Free, but needs an Angel One account.
                      Better for live use. Set these environment variables first:
                        ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET

Install:
    pip install pandas yfinance                 (for Yahoo)
    pip install smartapi-python pyotp logzero   (only if you use Angel One)

Every function returns a pandas table with columns:
    open, high, low, close, volume     and a time index in Indian time (IST)
"""

import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15}

YAHOO_SYMBOLS = {"nifty": "^NSEI", "sensex": "^BSESN"}

# Angel One index tokens. If data comes back empty, check these against
# Angel's official instrument list (OpenAPIScripMaster.json).
ANGEL_INDEX = {"nifty": ("NSE", "99926000"), "sensex": ("BSE", "99919000")}
ANGEL_INTERVALS = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_ist_index(df: pd.DataFrame) -> pd.DataFrame:
    """Make the time index timezone-aware in IST and name it 'datetime'."""
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df = df.copy()
    df.index = idx.tz_convert(IST)
    df.index.name = "datetime"
    return df


def drop_forming_candle(df: pd.DataFrame, interval: str, now: datetime = None) -> pd.DataFrame:
    """
    The last candle from the API may still be running (not finished).
    Patterns on an unfinished candle are wrong, so we remove it.
    """
    if df.empty:
        return df
    # Yahoo often sends a half-made candle with a single price (open = high = low = close).
    # That is not a real finished candle, so remove it.
    last = df.iloc[-1]
    if last["open"] == last["high"] == last["low"] == last["close"]:
        return df.iloc[:-1]
    now = now or datetime.now(IST)
    candle_end = df.index[-1] + timedelta(minutes=INTERVAL_MINUTES[interval])
    if now < candle_end:
        return df.iloc[:-1]
    return df


# ---------------------------------------------------------------------------
# Source 1: Yahoo Finance (no account needed)
# ---------------------------------------------------------------------------
# How much history to load by default (Yahoo limits: 1m = 7 days, 5m / 15m = 60 days)
DEFAULT_PERIOD = {"1m": "5d", "5m": "15d", "15m": "30d"}


def fetch_yahoo(index: str = "nifty", interval: str = "5m", period: str = None) -> pd.DataFrame:
    import yfinance as yf

    ticker = YAHOO_SYMBOLS[index]
    df = yf.download(ticker, period=period or DEFAULT_PERIOD[interval], interval=interval,
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("Yahoo returned no data. Market may be closed or internet is down.")

    # Newer yfinance versions return 2-level column names; flatten them.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "close"])
    return _to_ist_index(df)


# ---------------------------------------------------------------------------
# Source 2: Angel One SmartAPI (free with an Angel One account)
# ---------------------------------------------------------------------------
_angel_client = None


def _angel_login():
    global _angel_client
    if _angel_client is not None:
        return _angel_client

    from SmartApi import SmartConnect
    import logging
    import logzero
    import pyotp

    # The Angel One library prints request headers (including your API key) when a call fails.
    logzero.loglevel(logging.CRITICAL)

    missing = [k for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Set these environment variables first: {', '.join(missing)}")

    client = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    _angel_generate_session(client)
    _angel_client = client
    return client


def _angel_generate_session(client) -> None:
    """Log in (or log in again) on this client with a fresh one-time code. Raises RuntimeError if Angel One refuses."""
    import pyotp

    otp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
    session = client.generateSession(os.environ["ANGEL_CLIENT_ID"], os.environ["ANGEL_PIN"], otp)
    if not session or not session.get("status"):
        raise RuntimeError(f"Angel One login failed: {session}")


def angel_token_invalid(res=None, error=None) -> bool:
    """True when Angel One says the login token is no longer valid (it expires every day, or when you log in elsewhere)."""
    if error is not None:
        text = str(error).lower()
        return "invalid token" in text or "ag8001" in text or "ag8002" in text
    return (isinstance(res, dict) and not res.get("status", res.get("success", True))
            and (str(res.get("errorCode", res.get("errorcode", ""))).upper() in ("AG8001", "AG8002")
                 or "invalid token" in str(res.get("message", "")).lower()))


def _angel_relogin() -> bool:
    """A fresh login on the SAME client object (every caller keeps using it). Returns False if it did not work."""
    if _angel_client is None:
        return False
    try:
        _angel_generate_session(_angel_client)
        print("[angel] the login had expired: logged in again")
        return True
    except Exception as error:
        print(f"[angel] could not log in again: {error}")
        return False


ANGEL_CALL_GAP_SECONDS = 2.0        # Angel One refuses requests that come too fast
ANGEL_RETRY_WAITS = (3, 6, 12)      # seconds to wait before each retry after "exceeding access rate"
_angel_last_call = [0.0]
_angel_lock = threading.RLock()     # the dashboard serves several requests at once; Angel One must see ONE at a time
# Requests made for a page you are looking at go before background ones (the alert loop, refreshes that only
# top up a cache). A background request waits while a page request is queued, but never longer than the limit below.
BACKGROUND_MAX_WAIT_SECONDS = 10.0
_angel_priority = threading.local()
# counters for diagnosing slowness (shown in the page data as "timing")
angel_stats = {"calls": 0, "rate_limited": 0, "wait_seconds": 0.0}
angel_recent = []                      # the last calls: [clock time, outcome, seconds the call took, thread kind]
_angel_page_waiting = [0]
_angel_waiting_guard = threading.Lock()


class background_requests:
    """with background_requests(): ...   marks the Angel One calls made by this thread as low priority."""

    def __enter__(self):
        self._before = getattr(_angel_priority, "low", False)
        _angel_priority.low = True

    def __exit__(self, *exc):
        _angel_priority.low = self._before


def angel_too_fast(res=None, error=None) -> bool:
    """True when Angel One answered 'too many requests' / 'exceeding access rate'."""
    if error is not None:
        return "access rate" in str(error).lower() or "too many requests" in str(error).lower()
    return (isinstance(res, dict) and not res.get("status")
            and ("too many requests" in str(res.get("message", "")).lower()
                 or res.get("errorcode") == "AB1021"))


def _angel_request(call):
    """
    Run ONE Angel One request (candles, price, ...) one at a time, with a pause after the previous
    one, and a few retries when Angel One says 'too fast'. All Angel One calls should go through here.
    """
    import time

    entered = time.time()
    low = getattr(_angel_priority, "low", False)
    if low:
        give_up = time.time() + BACKGROUND_MAX_WAIT_SECONDS
        while _angel_page_waiting[0] > 0 and time.time() < give_up:
            time.sleep(0.1)                               # a page request is queued: let it go first
    else:
        with _angel_waiting_guard:
            _angel_page_waiting[0] += 1
    try:
        with _angel_lock:
            waits = ANGEL_RETRY_WAITS[:1] if low else ANGEL_RETRY_WAITS     # background work gives up early and tries again later
            relogged = False
            for attempt in range(len(waits) + 1):
                wait = ANGEL_CALL_GAP_SECONDS - (time.time() - _angel_last_call[0])
                if wait > 0:
                    time.sleep(wait)
                _angel_last_call[0] = time.time()
                angel_stats["calls"] += 1
                began = time.time()
                try:
                    res = call()
                    if angel_token_invalid(res=res) and not relogged:
                        relogged = True
                        if _angel_relogin():
                            res = call()                            # once more with the fresh token
                    if not angel_too_fast(res=res):
                        angel_recent.append([round(began % 1000, 1), "ok", round(time.time() - began, 2), "bg" if low else "page"])
                        del angel_recent[:-40]
                        return res
                    error = RuntimeError(str(res))
                except Exception as raised:
                    error = raised
                    if angel_token_invalid(error=raised) and not relogged:
                        relogged = True
                        if _angel_relogin():
                            continue
                    if not angel_too_fast(error=raised):
                        raise
                angel_recent.append([round(began % 1000, 1), "LIMITED", round(time.time() - began, 2), "bg" if low else "page"])
                del angel_recent[:-40]
                angel_stats["rate_limited"] += 1
                if attempt == len(waits):
                    raise error
                time.sleep(waits[attempt])
    finally:
        angel_stats["wait_seconds"] += time.time() - entered
        if not low:
            with _angel_waiting_guard:
                _angel_page_waiting[0] -= 1


def _angel_get_candles(client, params: dict):
    """getCandleData through the one-at-a-time, retrying request wrapper."""
    return _angel_request(lambda: client.getCandleData(params))


def fetch_angel_range(index: str, interval: str, start: datetime, end: datetime,
                      allow_empty: bool = False, instrument: tuple = None) -> pd.DataFrame:
    """
    Candles between two times (IST). Angel One limits how long one request may be (see candle_store).
    instrument = (exchange, token) to fetch something other than the index, such as a future.
    """
    client = _angel_login()
    exchange, token = instrument or ANGEL_INDEX[index]
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": ANGEL_INTERVALS[interval],
        "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
    }
    res = _angel_get_candles(client, params)
    rows = (res or {}).get("data")
    if not rows:
        if allow_empty and (res or {}).get("status"):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], tz=IST, name="datetime"))
        raise RuntimeError(f"Angel One returned no candles: {res}")

    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    return _to_ist_index(df)


def fetch_angel(index: str = "nifty", interval: str = "5m", days: int = None) -> pd.DataFrame:
    days = days or {"1m": 5, "5m": 15, "15m": 30}[interval]
    now = datetime.now(IST)
    return fetch_angel_range(index, interval, now - timedelta(days=days), now)


# ---------------------------------------------------------------------------
# Angel One: real option price (LTP) for one strike
# ---------------------------------------------------------------------------
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
OPTION_SEGMENT = {"nifty": ("NFO", "NIFTY"), "sensex": ("BFO", "SENSEX")}
_scrip_master = {"date": None, "rows": None}


def _load_scrip_master():
    """Angel One's list of all instruments. Downloaded once a day and saved in .cache/"""
    import json
    import urllib.request

    today = datetime.now(IST).strftime("%Y%m%d")
    if _scrip_master["date"] == today and _scrip_master["rows"]:
        return _scrip_master["rows"]

    os.makedirs(".cache", exist_ok=True)
    path = os.path.join(".cache", f"scrip_master_{today}.json")
    if not os.path.exists(path):
        urllib.request.urlretrieve(SCRIP_MASTER_URL, path)
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    _scrip_master.update(date=today, rows=rows)
    return rows


def angel_futures(index: str, now: datetime = None) -> list:
    """Futures contracts of the index that have not expired yet, nearest first."""
    segment, name = OPTION_SEGMENT[index]
    now = now or datetime.now(IST)
    out = []
    for row in _load_scrip_master():
        if (row.get("name") != name or row.get("exch_seg") != segment
                or row.get("instrumenttype") != "FUTIDX"):
            continue
        try:
            expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
        except (KeyError, ValueError):
            continue
        if expiry < now.date() or (expiry == now.date() and now.hour * 60 + now.minute >= 15 * 60 + 30):
            continue
        out.append({"expiry": expiry, "symbol": row["symbol"], "token": row["token"], "exchange": segment})
    return sorted(out, key=lambda c: c["expiry"])


def fetch_angel_future(index: str, interval: str, start: datetime, end: datetime,
                       contract: dict, allow_empty: bool = False) -> pd.DataFrame:
    """Candles (with real volume) of one futures contract."""
    return fetch_angel_range(index, interval, start, end, allow_empty,
                             instrument=(contract["exchange"], contract["token"]))


def fetch_angel_option(index: str, strike: int, side: str) -> dict:
    """
    Finds the nearest-expiry option contract and returns its live price.
    Returns: {"ltp": float, "symbol": str, "expiry": date}
    """
    client = _angel_login()
    segment, name = OPTION_SEGMENT[index]
    now = datetime.now(IST)

    best = None
    for row in _load_scrip_master():
        if (row.get("name") != name or row.get("exch_seg") != segment
                or row.get("instrumenttype") != "OPTIDX"
                or not str(row.get("symbol", "")).endswith(side)):
            continue
        try:
            if abs(float(row["strike"]) / 100.0 - strike) > 0.01:
                continue
            expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
        except (KeyError, ValueError):
            continue
        # skip contracts that have already expired (expiry day after 3:30 pm counts as expired)
        if expiry < now.date() or (expiry == now.date() and now.hour * 60 + now.minute >= 15 * 60 + 30):
            continue
        if best is None or expiry < best[0]:
            best = (expiry, row)

    if best is None:
        raise RuntimeError(f"No {name} {strike} {side} contract found in Angel One list.")

    expiry, row = best
    res = _angel_request(lambda: client.ltpData(segment, row["symbol"], row["token"]))
    ltp = (res or {}).get("data", {}).get("ltp")
    if not ltp:
        raise RuntimeError(f"Angel One returned no option price: {res}")
    return {"ltp": float(ltp), "symbol": row["symbol"], "expiry": expiry}


def angel_option_contracts(index: str, spot: float, strikes: int = 5, now: datetime = None) -> list:
    """
    The option contracts of the nearest expiry whose strike is within `strikes` steps of `spot`
    (both calls and puts). Judged from 9:00 am of today so a contract that expires TODAY is still
    included after the close. Returns [{expiry, symbol, token, exchange, strike, side}], sorted by strike.
    """
    segment, name = OPTION_SEGMENT[index]
    now = (now or datetime.now(IST)).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = []
    for row in _load_scrip_master():
        if (row.get("name") != name or row.get("exch_seg") != segment
                or row.get("instrumenttype") != "OPTIDX"):
            continue
        symbol = str(row.get("symbol", ""))
        if not symbol.endswith(("CE", "PE")):
            continue
        try:
            expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
            strike = float(row["strike"]) / 100.0
        except (KeyError, ValueError):
            continue
        if expiry < now.date():
            continue
        rows.append({"expiry": expiry, "symbol": symbol, "token": row["token"], "exchange": segment,
                     "strike": strike, "side": symbol[-2:]})
    if not rows:
        return []
    nearest = min(r["expiry"] for r in rows)
    rows = [r for r in rows if r["expiry"] == nearest]
    levels = sorted({r["strike"] for r in rows})
    atm = min(range(len(levels)), key=lambda i: abs(levels[i] - spot))
    keep = set(levels[max(0, atm - strikes): atm + strikes + 1])
    return sorted((r for r in rows if r["strike"] in keep), key=lambda r: (r["strike"], r["side"]))


def option_lot_size(index: str):
    """Lot size of the index options from Angel One's public instrument list (no login). None if unknown."""
    segment, name = OPTION_SEGMENT[index]
    try:
        for row in _load_scrip_master():
            if row.get("name") == name and row.get("exch_seg") == segment and row.get("instrumenttype") == "OPTIDX":
                return int(float(row["lotsize"]))
    except Exception as error:
        print(f"[lot size] not available: {error}")
    return None


def fetch_angel_option_oi(contracts: list) -> tuple:
    """
    Current open interest of option contracts (one read-only request for all of them).
    Returns ({token: oi}, "dd-Mon-yyyy HH:MM:SS" time of the newest trade seen or "").
    """
    client = _angel_login()
    by_exchange = {}
    for c in contracts:
        by_exchange.setdefault(c["exchange"], []).append(c["token"])
    res = _angel_request(lambda: client.getMarketData("FULL", by_exchange))
    if not (res or {}).get("status"):
        raise RuntimeError(f"Angel One option OI failed: {str(res)[:200]}")
    rows = (res.get("data") or {}).get("fetched") or []
    oi = {str(r["symbolToken"]): float(r["opnInterest"]) for r in rows if r.get("opnInterest") is not None}
    times = [str(r.get("exchTradeTime") or "") for r in rows if r.get("exchTradeTime")]
    return oi, (max(times, key=lambda t: datetime.strptime(t, "%d-%b-%Y %H:%M:%S")) if times else "")


def fetch_angel_option_history(contract: dict, start: datetime, end: datetime) -> pd.DataFrame:
    """
    5-minute history of ONE option contract: close, volume (from the candles) and open interest
    (from the OI history). Read-only. Returns a table with an IST time index, columns close/volume/oi.
    """
    client = _angel_login()
    params = {
        "exchange": contract["exchange"], "symboltoken": contract["token"], "interval": "FIVE_MINUTE",
        "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
        "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
    }
    candles = _angel_request(lambda: client.getCandleData(dict(params)))
    oi = _angel_request(lambda: client.getOIData(dict(params)))
    for res in (candles, oi):
        if not (res or {}).get("status"):
            raise RuntimeError(f"Angel One option history failed: {str(res)[:200]}")
    c = pd.DataFrame((candles.get("data") or []), columns=["datetime", "open", "high", "low", "close", "volume"])
    o = pd.DataFrame((oi.get("data") or []), columns=["time", "oi"]) if oi.get("data") else pd.DataFrame(columns=["time", "oi"])
    if c.empty:
        return pd.DataFrame(columns=["close", "volume", "oi"], index=pd.DatetimeIndex([], tz=IST, name="datetime"))
    c["datetime"] = pd.to_datetime(c["datetime"], utc=True).dt.tz_convert(IST)
    o["datetime"] = pd.to_datetime(o["time"], utc=True).dt.tz_convert(IST)
    df = c.set_index("datetime")[["close", "volume"]].join(o.set_index("datetime")[["oi"]], how="left")
    df.index.name = "datetime"
    return df


# ---------------------------------------------------------------------------
# One function to use everywhere
# ---------------------------------------------------------------------------
def get_candles(source: str = "yahoo", index: str = "nifty", interval: str = "5m",
                include_forming: bool = False) -> pd.DataFrame:
    """Returns only FINISHED candles (set include_forming=True to also get the running one)."""
    if interval not in INTERVAL_MINUTES:
        raise ValueError(f"interval must be one of {list(INTERVAL_MINUTES)}")
    if index not in YAHOO_SYMBOLS:
        raise ValueError("index must be 'nifty' or 'sensex'")

    if source == "yahoo":
        df = fetch_yahoo(index, interval)
    elif source == "angel":
        df = fetch_angel(index, interval)
    else:
        raise ValueError("source must be 'yahoo' or 'angel'")

    return df if include_forming else drop_forming_candle(df, interval)
