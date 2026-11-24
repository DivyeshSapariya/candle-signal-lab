"""
candle_signals.py
-----------------
The "brain" of the tool. It looks at candles and decides: CE, PE or NO TRADE.

It is used by BOTH the live dashboard and the backtest, so the live tool and the
backtest always follow exactly the same rules.

IMPORTANT (no cheating): when it judges candle number i, it only uses information
from candles up to i. It never peeks at the future. (A test in this project checks this.)

Rules in simple words
  The signal candle (S) must show a pattern that leans CE or PE.
  Then these checks are made. A "required" check must pass, or there is no trade.

  Required checks (each can be switched on / off in DEFAULT_FILTERS):
    confirm           The next candle must close beyond the signal candle (proof, not hope)
    htf_trend         15-minute and 60-minute trend must agree with the direction
    adx               Trend strength (ADX) must be at least 20 (skip sideways markets)
    time              Signal candle starts after 9:30 and the trade starts before 15:00
    expiry_afternoon  No new trades on expiry day after 1:00 pm
    vix               Skip when India VIX (previous close) is above 22
    lunch             (off by default) skip 12:00 to 1:30 pm

  Points (need 3 of 4):
    +2 clear pattern, +1 short-term trend agrees (EMA 9 vs EMA 21),
    +1 at a key level (previous day high/low/close, opening range, recent swing)
"""

import numpy as np
import pandas as pd

from candle_patterns import PATTERN_INFO, add_patterns
from option_pricer import EXPIRY_WEEKDAY, is_expiry_day

# ---------------------------------------------------------------------------
# Settings you can change
# ---------------------------------------------------------------------------
STRIKE_STEP = {"nifty": 50, "sensex": 100}
REWARD_TO_RISK = 2.0
MAX_RISK_ATR = 1.5        # stop loss never wider than 1.5 x ATR
MIN_RISK_ATR = 0.25       # stop loss never tighter than 0.25 x ATR
NEAR_LEVEL_ATR = 0.5      # "near a level" = within 0.5 x ATR
SWING_LOOKBACK = 20
OPENING_RANGE_MIN = 30
ADX_PERIOD = 14
VOLUME_AVG_CANDLES = 20
VOLUME_MIN_RATIO = 1.0     # signal candle volume vs the recent average
GAP_BIG_PCT = 0.3          # an opening gap bigger than this (in %) counts as a "big gap"
ADX_MIN = 20
MIN_SCORE = 3
VIX_MAX = 22.0

MARKET_OPEN_MIN = 9 * 60 + 15
MARKET_CLOSE_MIN = 15 * 60 + 30
TRADE_START_MIN = 9 * 60 + 30     # signal candle must start at or after 9:30
TRADE_END_MIN = 15 * 60           # decision candle must end by 3:00 pm
LUNCH_START_MIN = 12 * 60
LUNCH_END_MIN = 13 * 60 + 30
EXPIRY_CUTOFF_MIN = 13 * 60

DEFAULT_FILTERS = {
    "confirm": True,
    "htf_trend": True,
    "adx": True,
    "levels": True,
    "time": True,
    "vix": True,
    "expiry_afternoon": True,
    "lunch": False,
    # extra checks. VWAP is ON (without volume data it is the day's average price).
    # Volume and gap are OFF until the research report shows they help (python candle_research.py)
    "vwap": True,
    "volume": False,
    "gap": False,
}

FILTER_LABELS = {
    "confirm": "Confirmation candle",
    "htf_trend": "Bigger-timeframe trend (15m + 60m)",
    "adx": "ADX trend strength",
    "levels": "Key levels",
    "time": "Time of day (skip first 15 min / after 3 pm)",
    "vix": "India VIX limit",
    "expiry_afternoon": "No expiry-day afternoon trades",
    "lunch": "Skip lunch time",
    "vwap": "Price on the right side of VWAP",
    "volume": "Volume above its recent average",
    "gap": "Not trading against a big opening gap",
}


def pretty(name: str) -> str:
    return name.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["atr"] = _true_range(df).rolling(14).mean()
    return df


def add_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.DataFrame:
    """ADX = how strong the trend is (0 to 100). Under 20 usually means sideways market."""
    df = df.copy()
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    alpha = 1.0 / period
    tr_avg = _true_range(df).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / tr_avg
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / tr_avg
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return df


def higher_timeframe_trend(df: pd.DataFrame, minutes: int, base_minutes: int) -> pd.Series:
    """
    Trend of a bigger candle size (for example 15 or 60 minutes) as +1 (up), -1 (down), 0 (unknown).
    A bigger candle is used only AFTER it has fully finished, so there is no peeking.
    """
    tmp = df[["open", "high", "low", "close"]].copy()
    tmp["_end"] = df.index + pd.Timedelta(minutes=base_minutes)
    bars = tmp.resample(f"{minutes}min", offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "_end": "max"}
    ).dropna(subset=["close"])

    start = pd.Series(bars.index, index=bars.index)
    nominal_end = start + pd.Timedelta(minutes=minutes)
    cap = start.dt.normalize() + pd.Timedelta(minutes=MARKET_CLOSE_MIN)
    end = nominal_end.where(nominal_end <= cap, cap)
    bars = bars[bars["_end"] >= end]          # keep only fully finished bars
    end = end[bars.index]

    ema9 = bars["close"].ewm(span=9, adjust=False, min_periods=21).mean()
    ema21 = bars["close"].ewm(span=21, adjust=False, min_periods=21).mean()
    trend = pd.Series(np.sign(ema9 - ema21), index=bars.index).fillna(0.0)

    def _utc(x):
        # same UTC values as the old `.values`, without the pandas deprecation warning
        x = pd.DatetimeIndex(x)
        return (x.tz_convert(None) if x.tz is not None else x).to_numpy()

    right = pd.DataFrame({"end": _utc(end), "trend": trend.to_numpy()}).sort_values("end")
    left = pd.DataFrame({"end": _utc(df.index + pd.Timedelta(minutes=base_minutes))})
    merged = pd.merge_asof(left, right, on="end", direction="backward")
    return pd.Series(merged["trend"].fillna(0.0).values, index=df.index)


def add_levels(f: pd.DataFrame) -> pd.DataFrame:
    """Previous day high / low / close, opening range high / low, recent swing high / low."""
    f = f.copy()
    day = pd.Series(f.index.normalize(), index=f.index)

    daily = f.groupby(day).agg(pdh=("high", "max"), pdl=("low", "min"), pdc=("close", "last"))
    prev = daily.shift(1)
    for col in ("pdh", "pdl", "pdc"):
        f[col] = day.map(prev[col])

    since_open = f.index.hour * 60 + f.index.minute - MARKET_OPEN_MIN
    in_or = np.asarray(since_open < OPENING_RANGE_MIN)
    or_df = f[in_or].groupby(day[in_or]).agg(or_high=("high", "max"), or_low=("low", "min"))
    f["or_high"] = day.map(or_df["or_high"])
    f["or_low"] = day.map(or_df["or_low"])
    f.loc[in_or, ["or_high", "or_low"]] = np.nan      # opening range is not final yet

    f["swing_low"] = f["low"].shift(1).rolling(SWING_LOOKBACK).min()
    f["swing_high"] = f["high"].shift(1).rolling(SWING_LOOKBACK).max()
    return f


def add_vix(f: pd.DataFrame, vix_daily) -> pd.DataFrame:
    """vix_prev = India VIX close of the previous trading day (known before the market opens)."""
    f = f.copy()
    if vix_daily is None or len(vix_daily) == 0:
        f["vix_prev"] = np.nan
        return f
    s = vix_daily.copy()
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).tz_localize(None).normalize()
    s = s[~s.index.duplicated()].sort_index()
    days = pd.DatetimeIndex(f.index.tz_localize(None).normalize())
    pos = s.index.searchsorted(days, side="left") - 1
    values = np.where(pos >= 0, s.values[np.clip(pos, 0, None)], np.nan)
    f["vix_prev"] = values.astype(float)
    return f


def add_vwap_volume_gap(f: pd.DataFrame, fut: pd.DataFrame = None) -> pd.DataFrame:
    """
    vwap       : today's volume-weighted average price up to and including each candle.
                 The weight is the volume of the index FUTURE (fut) when it is given, because the
                 index itself has no volume. For a day without any volume every candle weighs the
                 same (a plain average of the day's prices) and vwap_proxy is True for that day.
    vol_ratio  : future volume of the candle / average of the last 20 candles (NaN without volume).
    gap_pct    : today's opening price vs yesterday's close, in %. Known from the first candle.
    Only past and current candles are used, so there is no look-ahead.
    """
    f = f.copy()
    day = pd.Series(f.index.normalize(), index=f.index)
    typical = (f["high"] + f["low"] + f["close"]) / 3

    vol = f["volume"].fillna(0).astype(float)
    if fut is not None and len(fut):
        fv = fut["volume"].astype(float)
        fv = fv[~fv.index.duplicated(keep="last")]
        vol = fv.reindex(f.index).fillna(0.0)
    day_volume = vol.groupby(day).transform("sum")
    has_volume = day_volume > 0                               # per day: is there real volume?
    weight = vol.where(has_volume, 1.0)
    num = (typical * weight).groupby(day).cumsum()
    den = weight.groupby(day).cumsum()
    f["vwap"] = num / den
    f["vwap_proxy"] = ~has_volume

    known = vol.where(vol > 0)                                # missing / zero volume is "unknown"
    avg = known.shift(1).rolling(VOLUME_AVG_CANDLES, min_periods=VOLUME_AVG_CANDLES // 2).mean()
    f["vol_ratio"] = known / avg.replace(0, np.nan)

    day_open = f["open"].groupby(day).transform("first")
    f["gap_pct"] = (day_open - f["pdc"]) / f["pdc"] * 100
    return f


def compute_features(df: pd.DataFrame, index: str, interval_min: int, vix_daily=None,
                     fut: pd.DataFrame = None) -> pd.DataFrame:
    """
    Everything the engine needs for every candle. Input: finished candles only.
    fut = candles of the index future (same candle size) used only for its volume.
    """
    f = add_patterns(df)
    f = add_indicators(f)
    f = add_adx(f)
    f["htf15"] = higher_timeframe_trend(df, 15, interval_min)
    f["htf60"] = higher_timeframe_trend(df, 60, interval_min)
    f = add_levels(f)
    f = add_vwap_volume_gap(f, fut)
    f = add_vix(f, vix_daily)

    f["day"] = f.index.date
    f["start_min"] = f.index.hour * 60 + f.index.minute
    expiry = {d: is_expiry_day(index, d) for d in pd.unique(f["day"])}
    f["is_expiry"] = [expiry[d] for d in f["day"]]
    return f


# ---------------------------------------------------------------------------
# Evaluate one candle
# ---------------------------------------------------------------------------
def _none(message: str, reasons=None, score=0) -> dict:
    return {"signal": "NONE", "score": score, "max_score": 4,
            "reasons": reasons or [], "message": message}


def _level_check(S, atr: float, side: str):
    """Is the signal candle at (or breaking through) an important price level?"""
    near = NEAR_LEVEL_ATR * atr
    wide = 1.5 * atr
    low, high, open_, close = S["low"], S["high"], S["open"], S["close"]

    if side == "CE":
        supports = [("previous day low", S["pdl"]), ("previous day close", S["pdc"]),
                    ("opening range low", S["or_low"]), ("recent swing low", S["swing_low"])]
        for name, lvl in supports:
            if pd.notna(lvl) and lvl - wide <= low <= lvl + near and close > lvl - near:
                return True, f"touched {name} ({lvl:,.0f})"
        for name, lvl in [("previous day high", S["pdh"]), ("opening range high", S["or_high"])]:
            if pd.notna(lvl) and open_ <= lvl < close:
                return True, f"broke above {name} ({lvl:,.0f})"
        return False, "is not at any key level"

    resistances = [("previous day high", S["pdh"]), ("previous day close", S["pdc"]),
                   ("opening range high", S["or_high"]), ("recent swing high", S["swing_high"])]
    for name, lvl in resistances:
        if pd.notna(lvl) and lvl - near <= high <= lvl + wide and close < lvl + near:
            return True, f"touched {name} ({lvl:,.0f})"
    for name, lvl in [("previous day low", S["pdl"]), ("opening range low", S["or_low"])]:
        if pd.notna(lvl) and open_ >= lvl > close:
            return True, f"broke below {name} ({lvl:,.0f})"
    return False, "is not at any key level"


def _patterns_of(row) -> list:
    return [p for p in PATTERN_INFO if bool(row[p])]


def evaluate(f: pd.DataFrame, i: int, index: str, interval_min: int, filters: dict = None) -> dict:
    """
    Judge decision candle number i (using only candles up to i).
    Returns a dict with signal = 'CE', 'PE' or 'NONE', the checklist, and the levels.
    """
    flt = {**DEFAULT_FILTERS, **(filters or {})}
    if i < 30 or i >= len(f):
        return _none("Not enough candles yet to build a plan.")

    si = i - 1 if flt["confirm"] else i
    S, C = f.iloc[si], f.iloc[i]
    if S["day"] != C["day"]:
        return _none("A new day just started. Waiting for a fresh setup.")
    atr = C["atr"]
    if pd.isna(atr) or atr <= 0:
        return _none("Not enough candles yet to build a plan.")

    found = _patterns_of(S)
    leans = {PATTERN_INFO[p]["leans"] for p in found} - {"WAIT"}
    if not found or not leans:
        watch = ""
        if flt["confirm"]:
            found_c = _patterns_of(C)
            leans_c = {PATTERN_INFO[p]["leans"] for p in found_c} - {"WAIT"}
            if len(leans_c) == 1:
                names_c = " + ".join(pretty(p) for p in found_c)
                watch = (f"Possible setup: {names_c} on the last candle leans {next(iter(leans_c))}. "
                         "Wait for the next candle to confirm it.")
        return _none(watch or "No clear CE / PE pattern. Wait.")
    if len(leans) > 1:
        return _none("Patterns on the signal candle disagree (CE and PE). Wait.")

    side = next(iter(leans))
    sign = 1 if side == "CE" else -1
    names = " + ".join(pretty(p) for p in found)
    reasons = [{"key": "pattern", "text": f"{names} leans {side}", "ok": True, "required": True}]

    # --- confirmation candle ---
    if flt["confirm"]:
        if side == "CE":
            ok = bool(C["close"] > S["high"] and C["green"])
            text = ("Next candle closed above the signal candle high - confirmed" if ok
                    else "Next candle did not close above the signal candle high")
        else:
            ok = bool(C["close"] < S["low"] and C["red"])
            text = ("Next candle closed below the signal candle low - confirmed" if ok
                    else "Next candle did not close below the signal candle low")
        reasons.append({"key": "confirm", "text": text, "ok": ok, "required": True})

    # --- bigger timeframe trend ---
    if flt["htf_trend"]:
        h15, h60 = C["htf15"], C["htf60"]
        word = lambda v: "UP" if v > 0 else "DOWN" if v < 0 else "unknown"
        ok = bool(h15 == sign and h60 == sign)
        reasons.append({
            "key": "htf_trend",
            "text": f"15 min trend {word(h15)}, 60 min trend {word(h60)} - "
                    + ("agrees" if ok else "does not agree"),
            "ok": ok, "required": True})

    # --- ADX trend strength ---
    if flt["adx"]:
        adx = C["adx"]
        ok = bool(pd.notna(adx) and adx >= ADX_MIN)
        shown = "n/a" if pd.isna(adx) else f"{adx:.0f}"
        reasons.append({
            "key": "adx",
            "text": f"Trend strength (ADX) {shown} - " + ("strong enough" if ok else "weak, market is sideways"),
            "ok": ok, "required": True})

    # --- time of day ---
    if flt["time"]:
        ok = bool(S["start_min"] >= TRADE_START_MIN and C["start_min"] + interval_min <= TRADE_END_MIN)
        reasons.append({
            "key": "time",
            "text": "Time of day is fine" if ok else "Time of day is bad (first 15 min or after 3 pm)",
            "ok": ok, "required": True})
    if flt["expiry_afternoon"] and bool(S["is_expiry"]):
        ok = bool(S["start_min"] < EXPIRY_CUTOFF_MIN)
        reasons.append({
            "key": "expiry_afternoon",
            "text": "Expiry day, before 1 pm" if ok else "Expiry day afternoon - too wild, skip",
            "ok": ok, "required": True})
    if flt["lunch"]:
        ok = not (LUNCH_START_MIN <= S["start_min"] < LUNCH_END_MIN)
        reasons.append({"key": "lunch", "text": "Not lunch time" if ok else "Lunch time - market is slow",
                        "ok": ok, "required": True})

    # --- India VIX ---
    if flt["vix"]:
        vix = C["vix_prev"]
        if pd.notna(vix):
            ok = bool(vix <= VIX_MAX)
            reasons.append({
                "key": "vix",
                "text": f"India VIX {vix:.1f} - " + ("normal" if ok else f"above {VIX_MAX:.0f}, too wild"),
                "ok": ok, "required": True})

    # --- optional extra checks (all off by default) ---
    if flt["vwap"] and pd.notna(C["vwap"]):
        above = bool(C["close"] > C["vwap"])
        ok = above if side == "CE" else not above
        label = "average price of the day" if C["vwap_proxy"] else "VWAP"
        reasons.append({
            "key": "vwap",
            "text": f"Price is {'above' if above else 'below'} the {label} ({C['vwap']:,.0f}) - "
                    + ("agrees" if ok else "does not agree"),
            "ok": ok, "required": True})
    if flt["volume"] and pd.notna(C["vol_ratio"]):
        ok = bool(C["vol_ratio"] >= VOLUME_MIN_RATIO)
        reasons.append({
            "key": "volume",
            "text": f"Volume is {C['vol_ratio']:.1f}x its recent average - "
                    + ("busy enough" if ok else "quiet, weak move"),
            "ok": ok, "required": True})
    if flt["gap"] and pd.notna(C["gap_pct"]):
        against = (side == "CE" and C["gap_pct"] <= -GAP_BIG_PCT) or                   (side == "PE" and C["gap_pct"] >= GAP_BIG_PCT)
        reasons.append({
            "key": "gap",
            "text": f"Opening gap {C['gap_pct']:+.2f}% - "
                    + (f"this trade goes against a big gap" if against else "no big gap against this trade"),
            "ok": not against, "required": True})

    # --- points: short-term trend and key level ---
    ema_ok = bool(C["ema9"] > C["ema21"]) if side == "CE" else bool(C["ema9"] < C["ema21"])
    reasons.append({
        "key": "ema",
        "text": ("Short-term trend (EMA 9 vs EMA 21) agrees" if ema_ok
                 else "Short-term trend (EMA 9 vs EMA 21) does not agree"),
        "ok": ema_ok, "required": False})

    level_ok, level_text = True, ""
    if flt["levels"]:
        level_ok, level_text = _level_check(S, atr, side)
        reasons.append({"key": "levels", "text": f"Signal candle {level_text}", "ok": level_ok, "required": False})

    score = 2 + int(ema_ok) + int(level_ok)

    failed = [r for r in reasons if r["required"] and not r["ok"]]
    if failed:
        return _none("Not a good setup: " + failed[0]["text"].lower() + ". Wait.", reasons, score)
    if score < MIN_SCORE:
        return _none("Pattern found, but not enough things agree. Wait.", reasons, score)

    # --- levels: entry, stop loss, target ---
    if flt["confirm"]:
        entry = float(C["close"])
        raw_stop = float(S["low"]) if side == "CE" else float(S["high"])
    else:
        entry = float(S["high"]) if side == "CE" else float(S["low"])
        raw_stop = float(S["low"]) if side == "CE" else float(S["high"])
    risk = abs(entry - raw_stop)
    risk = min(max(risk, MIN_RISK_ATR * atr), MAX_RISK_ATR * atr)
    stop = entry - sign * risk
    target = entry + sign * REWARD_TO_RISK * risk
    step = STRIKE_STEP[index]
    strike = int(round(entry / step) * step)

    # Facts about the market at the time of the idea. The memory learns from these.
    adx_v, vix_v = C["adx"], C["vix_prev"]
    level_kind = ("touch" if level_text.startswith("touched") else
                  "breakout" if level_text.startswith("broke") else
                  "none" if flt["levels"] else "unchecked")
    context = {
        "patterns": found,
        "pattern": found[0],
        "adx": None if pd.isna(adx_v) else round(float(adx_v), 1),
        "htf15": int(C["htf15"]),
        "htf60": int(C["htf60"]),
        "level": level_text,
        "level_kind": level_kind,
        "vix": None if pd.isna(vix_v) else round(float(vix_v), 1),
        "start_min": int(S["start_min"]),
        "is_expiry": bool(S["is_expiry"]),
        "ema_ok": bool(ema_ok),
        "atr": round(float(atr), 2),
        "gap_pct": None if pd.isna(C["gap_pct"]) else round(float(C["gap_pct"]), 2),
        "above_vwap": None if pd.isna(C["vwap"]) else bool(C["close"] > C["vwap"]),
    }

    return {
        "signal": side,
        "score": score,
        "max_score": 4,
        "reasons": reasons,
        "message": "",
        "mode": "confirm" if flt["confirm"] else "breakout",
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_points": round(risk, 2),
        "reward_points": round(REWARD_TO_RISK * risk, 2),
        "strike": strike,
        "option": f"{index.upper()} {strike} {side}",
        "signal_time": S.name.isoformat(),
        "decision_time": C.name.isoformat(),
        "decision_index": int(i),
        "context": context,
    }
