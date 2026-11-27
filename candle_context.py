"""
candle_context.py
-----------------
Plain information about the market around the current candle. Nothing here changes a decision: no rule, check or
alert uses any of it. It is shown on the dashboard so you can see the bigger picture:

  - market_context(...)  the daily trend, the last week's range, today's gap, yesterday's candle, today's range so
                         far, the 1-year range and the India VIX trend
  - rsi(...), macd(...)  the two classic momentum indicators, for the chart panels
  - pivot_levels(...)    the classic floor pivots and the central pivot range (CPR) from the previous day

All the rules are fixed and simple (written below), and every number uses only days that are already finished,
except "today so far".
"""

import numpy as np
import pandas as pd

GAP_BIG_PCT = 0.3            # a gap bigger than this (in %) is a "gap up / down"
TREND_EMA_DAYS = 20
TREND_MIN_DAYS = 25          # fewer finished days than this: no trend is claimed
WEEK_DAYS = 5
VIX_FLAT_PCT = 5.0           # the VIX counts as rising / falling when it is this far (in %) from its 5-day average


def daily_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Daily open / high / low / close from intraday candles (index = date). The last day may be unfinished."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    day = pd.Series(df.index.date, index=df.index)
    g = df.groupby(day)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    out.index = pd.Index(out.index, name="day")
    return out


def _pct(a, b):
    return None if not b else round((a / b - 1.0) * 100.0, 2)


def market_context(daily: pd.DataFrame, today, price: float, vix_daily=None) -> dict:
    """
    daily: daily candles (daily_candles), index = date, may include today's unfinished day.
    today: the date being looked at. price: the latest price. vix_daily: optional Series of daily India VIX closes.
    Any part that cannot be worked out is None.
    """
    prior = daily[daily.index < today]
    now_row = daily.loc[today] if today in daily.index else None
    out = {"trend": None, "week": None, "gap": None, "prev_day": None, "today": None, "year": None, "vix": None}
    if len(prior) < 2:
        return out
    pdo, pdh, pdl, pdc = (float(prior.iloc[-1][c]) for c in ("open", "high", "low", "close"))

    # --- daily trend: close vs its 20-day EMA, and whether the EMA is rising ---
    if len(prior) >= TREND_MIN_DAYS:
        ema = prior["close"].ewm(span=TREND_EMA_DAYS, adjust=False).mean()
        e_now, e_5 = float(ema.iloc[-1]), float(ema.iloc[-6])
        label = "Up" if pdc > e_now and e_now > e_5 else "Down" if pdc < e_now and e_now < e_5 else "Sideways"
        out["trend"] = {"label": label, "ema20": round(e_now, 2), "close": round(pdc, 2),
                        "vs_ema_pct": _pct(pdc, e_now), "change_5d_pct": _pct(pdc, float(prior["close"].iloc[-6])),
                        "change_20d_pct": _pct(pdc, float(prior["close"].iloc[-21]))}

    # --- the last week: the last 5 finished days and the previous calendar week ---
    last5 = prior.tail(WEEK_DAYS)
    h5, l5 = float(last5["high"].max()), float(last5["low"].min())
    week = {"high_5d": round(h5, 2), "low_5d": round(l5, 2),
            "position_pct": None if h5 == l5 else round((price - l5) / (h5 - l5) * 100.0, 0)}
    this_week = pd.Timestamp(today).isocalendar()[:2]
    weeks = prior.index.map(lambda d: pd.Timestamp(d).isocalendar()[:2])
    earlier = prior[[w < this_week for w in weeks]]
    if len(earlier):
        last_week = pd.Timestamp(earlier.index[-1]).isocalendar()[:2]
        wk = earlier[[pd.Timestamp(d).isocalendar()[:2] == last_week for d in earlier.index]]
        week.update(prev_week_high=round(float(wk["high"].max()), 2), prev_week_low=round(float(wk["low"].min()), 2),
                    prev_week_close=round(float(wk["close"].iloc[-1]), 2))
    out["week"] = week

    # --- yesterday ---
    rng = pdh - pdl
    out["prev_day"] = {"color": "green" if pdc >= pdo else "red", "range": round(rng, 1),
                       "close_location_pct": None if rng == 0 else round((pdc - pdl) / rng * 100.0, 0),
                       "high": round(pdh, 2), "low": round(pdl, 2), "close": round(pdc, 2)}

    # --- today's gap and range so far ---
    if now_row is not None:
        o, h, l = float(now_row["open"]), float(now_row["high"]), float(now_row["low"])
        gap = _pct(o, pdc)
        label = "Gap up" if gap is not None and gap >= GAP_BIG_PCT else "Gap down" if gap is not None and gap <= -GAP_BIG_PCT else "Flat open"
        filled = None if label == "Flat open" else (l <= pdc if label == "Gap up" else h >= pdc)
        out["gap"] = {"pct": gap, "label": label, "filled": filled, "open": round(o, 2), "prev_close": round(pdc, 2),
                      "open_vs_prev": "above the previous high" if o > pdh else "below the previous low" if o < pdl
                      else "inside the previous day's range"}
        tr = pd.concat([prior["high"] - prior["low"], (prior["high"] - prior["close"].shift()).abs(),
                        (prior["low"] - prior["close"].shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean()) if len(tr) >= 5 else None
        out["today"] = {"range": round(h - l, 1), "atr14": None if atr is None else round(atr, 1),
                        "range_vs_atr": None if not atr else round((h - l) / atr, 2),
                        "position_pct": None if h == l else round((price - l) / (h - l) * 100.0, 0)}

    # --- the saved year ---
    year = prior.tail(250)
    yh, yl = float(year["high"].max()), float(year["low"].min())
    out["year"] = {"high": round(yh, 2), "low": round(yl, 2), "from_high_pct": _pct(price, yh), "from_low_pct": _pct(price, yl),
                   "days": int(len(year))}

    # --- India VIX: the last close against its 5-day average ---
    if vix_daily is not None and len(vix_daily) >= 6:
        v = vix_daily.dropna()
        last, avg5 = float(v.iloc[-1]), float(v.iloc[-6:-1].mean())
        change = _pct(last, avg5)
        out["vix"] = {"last": round(last, 2), "avg5": round(avg5, 2), "change_pct": change,
                      "label": "rising" if change is not None and change >= VIX_FLAT_PCT
                      else "falling" if change is not None and change <= -VIX_FLAT_PCT else "flat"}
    return out


# ---------------------------------------------------------------------------
# Indicators for the chart panels
# ---------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (0 to 100). Uses only the candles up to each point."""
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    value = 100.0 - 100.0 / (1.0 + gain / loss.replace(0, np.nan))
    return value.where(~((loss == 0) & (gain > 0)), 100.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line (fast EMA minus slow EMA), its signal line and the histogram. Only past candles are used."""
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    out = pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})
    out.iloc[: slow - 1] = np.nan                                     # too early to be meaningful
    return out


def pivot_levels(high: float, low: float, close: float) -> dict:
    """
    Classic floor pivots from the PREVIOUS day, and the central pivot range (CPR).
    P = (H+L+C)/3, R1 = 2P-L, S1 = 2P-H, R2 = P+(H-L), S2 = P-(H-L), R3 = H+2(P-L), S3 = L-2(H-P).
    CPR: BC = (H+L)/2, TC = 2P-BC (so TC and BC may be either way round; "top" is the higher one).
    """
    p = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * p - bc
    top, bottom = max(tc, bc), min(tc, bc)
    levels = {"P": p, "R1": 2 * p - low, "R2": p + (high - low), "R3": high + 2 * (p - low),
              "S1": 2 * p - high, "S2": p - (high - low), "S3": low - 2 * (high - p)}
    out = {k: round(v, 2) for k, v in levels.items()}
    out.update(cpr_top=round(top, 2), cpr_bottom=round(bottom, 2),
               cpr_width_pct=round((top - bottom) / p * 100.0, 3) if p else None)
    return out
