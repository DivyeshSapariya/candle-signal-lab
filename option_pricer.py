"""
option_pricer.py
----------------
Converts the INDEX levels (entry, stop loss, target) into OPTION PRICE levels (premium),
so you can see: "buy this CE/PE at Rs X, stop loss Rs Y, target Rs Z".

How it works (simple words):
  An option price depends mainly on 4 things: index price, strike, days left, and
  volatility (how fast the market is expected to move). We use the standard
  Black-Scholes formula to ask: "if the index goes to the entry level, what will this
  option cost? What if it goes to the stop loss level? And to the target level?"

Where does volatility come from?
  1. LIVE option price available (Angel One source): we work backwards from the live
     option price. This is the most accurate.
  2. Otherwise: India VIX from Yahoo (free) is used. This is an ESTIMATE.
     Real prices in your Groww app can differ by 10-20% or more. Treat it as a guide.

Limits you must know:
  - Time decay between now and when the index touches entry is not included.
  - A real exit can be worse than the stop price if the market moves fast (slippage).
  - Keep holidays.csv up to date each year (exchange holidays shift weekly expiry dates).
"""

import math
import os
import time as _time
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

RISK_FREE_RATE = 0.065     # approx. Indian interest rate
DEFAULT_IV = 0.14          # used only if India VIX cannot be fetched
TICK = 0.05                # option prices move in steps of 5 paise

# Weekly expiry day (Monday = 0 ... Sunday = 6): NIFTY = Tuesday (NSE), SENSEX = Thursday (BSE)
EXPIRY_WEEKDAY = {"nifty": 1, "sensex": 3}

# Exchange holidays on weekdays. If expiry falls on one, it moves to the previous trading day.
# Keep this list updated from the NSE / BSE holiday calendar.
# They are read from holidays.csv (see read_holidays); the two dates below are used only if that file is missing.
_BUILTIN_HOLIDAYS = {
    date(2026, 11, 24),   # Guru Nanak Jayanti
    date(2026, 12, 25),   # Christmas
}
HOLIDAYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holidays.csv")
HOLIDAYS = set(_BUILTIN_HOLIDAYS)
_holiday_stamp = [None]


def read_holidays(path: str):
    """
    Read holidays.csv ("date,name" per line, # = comment). Returns (set of dates, list of problems).
    A bad line is reported and skipped, it never stops the tool.
    """
    found, problems = set(), []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#") or text.lower().startswith("date"):
                continue
            try:
                found.add(date.fromisoformat(text.split(",", 1)[0].strip()))
            except ValueError:
                problems.append(f"line {number}: '{text[:40]}' is not YYYY-MM-DD,name")
    return found, problems


def is_holiday(d: date) -> bool:
    """True if d is an exchange holiday. holidays.csv is re-read whenever the file changes."""
    path = os.environ.get("HOLIDAYS_FILE") or HOLIDAYS_FILE
    try:
        stamp = (path, os.path.getmtime(path))
    except OSError:
        stamp = (path, None)
    if stamp != _holiday_stamp[0]:
        _holiday_stamp[0] = stamp
        found, problems = set(_BUILTIN_HOLIDAYS), []
        if stamp[1] is not None:
            try:
                found, problems = read_holidays(path)
            except OSError as error:
                problems = [f"cannot read {path}: {error}"]
                found = set(_BUILTIN_HOLIDAYS)
        for problem in problems:
            print(f"[holidays] {problem}")
        HOLIDAYS.clear()
        HOLIDAYS.update(found)
    return d in HOLIDAYS

_vix_cache = {"time": 0.0, "value": None}


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------
def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, years: float, sigma: float, side: str,
             rate: float = RISK_FREE_RATE) -> float:
    """Price of a CE (call) or PE (put)."""
    if years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if side == "CE" else max(strike - spot, 0.0)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    if side == "CE":
        return spot * _cdf(d1) - strike * math.exp(-rate * years) * _cdf(d2)
    return strike * math.exp(-rate * years) * _cdf(-d2) - spot * _cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, side: str):
    """Find the volatility that matches a live option price. Returns None if not possible."""
    low, high = 0.01, 3.0
    if not (bs_price(spot, strike, years, low, side) <= price <= bs_price(spot, strike, years, high, side)):
        return None
    for _ in range(80):
        mid = (low + high) / 2
        if bs_price(spot, strike, years, mid, side) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


# ---------------------------------------------------------------------------
# Expiry date
# ---------------------------------------------------------------------------
def _previous_trading_day(d: date) -> date:
    while d.weekday() >= 5 or is_holiday(d):
        d -= timedelta(days=1)
    return d


previous_trading_day = _previous_trading_day


def is_expiry_day(index: str, d: date) -> bool:
    """True if date d is the weekly expiry day of this index (holiday shifts included)."""
    monday = d - timedelta(days=d.weekday())
    candidate = monday + timedelta(days=EXPIRY_WEEKDAY[index])
    return _previous_trading_day(candidate) == d


def previous_monthly_expiry(index: str, expiry: date) -> date:
    """
    The monthly futures expiry before `expiry`: the last expiry-weekday of the previous month
    (NIFTY Tuesday, SENSEX Thursday), moved back past weekends and holidays.
    """
    first = expiry.replace(day=1)
    last_day = first - timedelta(days=1)                  # last day of the previous month
    offset = (last_day.weekday() - EXPIRY_WEEKDAY[index]) % 7
    return _previous_trading_day(last_day - timedelta(days=offset))


def next_expiry(index: str, now: datetime) -> date:
    """Nearest weekly expiry that has not finished yet."""
    weekday = EXPIRY_WEEKDAY[index]
    candidate = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    for _ in range(6):
        expiry = _previous_trading_day(candidate)
        if datetime.combine(expiry, time(15, 30), IST) > now:
            return expiry
        candidate += timedelta(days=7)
    return candidate


def years_to_expiry(expiry: date, now: datetime) -> float:
    end = datetime.combine(expiry, time(15, 30), IST)
    seconds = max((end - now).total_seconds(), 60.0)
    return seconds / (365 * 24 * 3600)


# ---------------------------------------------------------------------------
# India VIX (free, from Yahoo)
# ---------------------------------------------------------------------------
def get_india_vix():
    """Returns India VIX as a fraction (13.5 -> 0.135), or None if it cannot be fetched."""
    if _time.time() - _vix_cache["time"] < 300 and _vix_cache["value"]:
        return _vix_cache["value"]
    try:
        import yfinance as yf
        df = yf.download("^INDIAVIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        close = df["Close"]
        value = float(close.iloc[-1].iloc[0] if hasattr(close.iloc[-1], "iloc") else close.iloc[-1])
        if not (3 < value < 100):
            return None
        _vix_cache.update(time=_time.time(), value=value / 100.0)
        return _vix_cache["value"]
    except Exception:
        return None


_vix_daily_cache = {"time": 0.0, "value": None}


def get_vix_daily(period: str = "6mo"):
    """Daily India VIX closes (in %, e.g. 13.5) as a pandas Series, or None if not available."""
    if _time.time() - _vix_daily_cache["time"] < 3600 and _vix_daily_cache["value"] is not None:
        return _vix_daily_cache["value"]
    try:
        import pandas as pd
        import yfinance as yf
        df = yf.download("^INDIAVIX", period=period, interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        series = df["Close"].dropna()
        series.index = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
        _vix_daily_cache.update(time=_time.time(), value=series)
        return series
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main function used by the dashboard
# ---------------------------------------------------------------------------
def _tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def build_option_levels(index: str, side: str, strike: int, spot: float,
                        entry: float, stop: float, target: float,
                        now: datetime = None, live_price: float = None,
                        expiry: date = None, sigma_override: float = None) -> dict:
    """
    Option price at the entry, stop loss and target index levels.
    live_price: real option price right now (if you have it), otherwise None.
    expiry: real expiry date (if known), otherwise worked out from the weekly rule.
    sigma_override: volatility to use as a fraction (0.156 = 15.6%), for example the India VIX
                    saved with a past idea. Skips the live India VIX download.
    """
    now = now or datetime.now(IST)
    expiry = expiry or next_expiry(index, now)
    years = years_to_expiry(expiry, now)
    days_left = (expiry - now.date()).days

    sigma, source, note = None, "estimated", ""
    if sigma_override:
        sigma = float(sigma_override)
        note = f"Estimated using the India VIX saved with this idea ({sigma * 100:.1f}% volatility)."
    elif live_price:
        sigma = implied_vol(live_price, spot, strike, years, side)
        if sigma:
            source = "live"
    if sigma is None:
        vix = get_india_vix()
        if vix:
            sigma = vix
            note = f"Estimated using India VIX ({vix * 100:.1f}% volatility)."
        else:
            sigma = DEFAULT_IV
            note = f"Estimated using a default {DEFAULT_IV * 100:.0f}% volatility (India VIX not available)."
    elif source == "live":
        note = f"Calculated from the live option price (volatility {sigma * 100:.1f}%)."

    def price_at(index_level):
        return max(_tick(bs_price(index_level, strike, years, sigma, side)), TICK)

    entry_price = price_at(entry)
    stop_price = price_at(stop)
    target_price = price_at(target)
    now_price = _tick(live_price) if live_price else price_at(spot)

    warning = ""
    if expiry == now.date():
        warning = "Today is expiry day. This option loses value very fast and can move wildly. Very risky."
    elif days_left <= 1:
        warning = "Expiry is tomorrow. Time decay is very fast. High risk."

    return {
        "expiry": expiry.strftime("%a %d %b"),
        "days_left": days_left,
        "price_source": source,
        "note": note,
        "now_price": now_price,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "loss_per_unit": round(entry_price - stop_price, 2),
        "profit_per_unit": round(target_price - entry_price, 2),
        "warning": warning,
    }
