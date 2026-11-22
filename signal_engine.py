"""
signal_engine.py
----------------
Shared candle features and trade rules used by the dashboard and backtest.
"""

from datetime import time, timedelta

import pandas as pd

from candle_patterns import PATTERN_INFO, add_patterns
from option_pricer import is_expiry_day


DEFAULT_FILTERS = {
    "confirmation": True,
    "higher_timeframe": True,
    "adx": True,
    "time_of_day": True,
    "expiry_afternoon": True,
    "vix": True,
    "lunch": False,
}

ADX_MINIMUM = 20.0
VIX_MAXIMUM = 22.0
MIN_RISK_ATR = 0.25
MAX_RISK_ATR = 1.5
REWARD_RISK_RATIO = 2.0

_BULLISH_PATTERNS = {
    name for name, info in PATTERN_INFO.items() if info["leans"] == "CE"
}
_BEARISH_PATTERNS = {
    name for name, info in PATTERN_INFO.items() if info["leans"] == "PE"
}


def _ema_trend(close: pd.Series) -> pd.Series:
    fast = close.ewm(span=9, adjust=False, min_periods=9).mean()
    slow = close.ewm(span=21, adjust=False, min_periods=21).mean()
    trend = pd.Series("SIDEWAYS", index=close.index, dtype="object")
    trend.loc[fast > slow] = "UP"
    trend.loc[fast < slow] = "DOWN"
    return trend


def _higher_timeframe_trend(df: pd.DataFrame, interval_min: int,
                            minutes: int) -> pd.Series:
    candle_ends = df.index + timedelta(minutes=interval_min)
    bars = df[["open", "high", "low", "close"]].copy()
    bars.index = candle_ends
    higher = bars.resample(
        f"{minutes}min", origin="start_day", offset="9h15min",
        label="right", closed="right",
    ).agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    higher = higher.dropna(subset=["close"])
    trend = _ema_trend(higher["close"])
    return trend.reindex(candle_ends, method="ffill").set_axis(df.index)


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_change = df["high"].diff()
    low_change = -df["low"].diff()
    plus_dm = high_change.where((high_change > low_change) & (high_change > 0), 0.0)
    minus_dm = low_change.where((low_change > high_change) & (low_change > 0), 0.0)

    previous_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    smoothing = lambda values: values.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    atr = smoothing(true_range)
    plus_di = 100 * smoothing(plus_dm) / atr
    minus_di = 100 * smoothing(minus_dm) / atr
    directional_index = (
        100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    ).fillna(0.0)
    return smoothing(directional_index)


def _previous_day_vix(index: pd.DatetimeIndex, vix_daily) -> pd.Series:
    result = pd.Series(float("nan"), index=index, dtype="float64")
    if vix_daily is None or len(vix_daily) == 0:
        return result

    values = pd.Series(vix_daily).dropna().sort_index()
    vix_index = pd.DatetimeIndex(values.index)
    if vix_index.tz is not None:
        vix_index = vix_index.tz_localize(None)
    vix_dates = vix_index.normalize()
    daily = pd.Series(values.to_numpy(), index=vix_dates)
    daily = daily[~daily.index.duplicated(keep="last")].sort_index()
    daily_dates = daily.index

    for timestamp in index:
        day = pd.Timestamp(timestamp)
        if day.tzinfo is not None:
            day = day.tz_localize(None)
        position = daily_dates.searchsorted(day.normalize(), side="left") - 1
        if position >= 0:
            result.loc[timestamp] = float(daily.iloc[position])
    return result


def compute_features(candles: pd.DataFrame, index: str, interval_min: int,
                     vix_daily=None) -> pd.DataFrame:
    """Add candle patterns, trend, volatility, and prior-day VIX features."""
    required = {"open", "high", "low", "close"}
    missing = required.difference(candles.columns)
    if missing:
        raise ValueError(f"candles must contain columns: {sorted(required)}")
    if interval_min <= 0:
        raise ValueError("interval_min must be positive")

    features = add_patterns(candles)
    features["ema_9"] = features["close"].ewm(
        span=9, adjust=False, min_periods=9
    ).mean()
    features["ema_21"] = features["close"].ewm(
        span=21, adjust=False, min_periods=21
    ).mean()
    features["atr"] = pd.concat(
        [
            features["high"] - features["low"],
            (features["high"] - features["close"].shift(1)).abs(),
            (features["low"] - features["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1).rolling(14, min_periods=14).mean()
    features["adx"] = _adx(features)
    features["short_trend"] = _ema_trend(features["close"])
    features["trend_15"] = _higher_timeframe_trend(features, interval_min, 15)
    features["trend_60"] = _higher_timeframe_trend(features, interval_min, 60)
    features["vix_previous"] = _previous_day_vix(features.index, vix_daily)
    return features


def _key_level_touched(features: pd.DataFrame, signal_position: int,
                       interval_min: int) -> bool:
    row = features.iloc[signal_position]
    atr = row["atr"]
    if pd.isna(atr) or atr <= 0:
        return False
    tolerance = 0.25 * atr
    timestamp = features.index[signal_position]
    day = timestamp.date()
    dates = pd.Index(features.index.date)
    previous = features.loc[dates < day]
    today_before = features.iloc[:signal_position]
    today_before = today_before.loc[pd.Index(today_before.index.date) == day]

    levels = []
    if not previous.empty:
        previous_day = previous.loc[pd.Index(previous.index.date) == previous.index[-1].date()]
        levels.extend([
            float(previous_day["high"].max()),
            float(previous_day["low"].min()),
            float(previous_day["close"].iloc[-1]),
        ])

    opening_end = time(9, 45)
    opening = today_before.loc[
        [(ts.hour, ts.minute) < (opening_end.hour, opening_end.minute)
         for ts in today_before.index]
    ]
    if not opening.empty:
        levels.extend([float(opening["high"].max()), float(opening["low"].min())])

    earlier = features.iloc[max(0, signal_position - 20):signal_position]
    if not earlier.empty:
        levels.extend([float(earlier["high"].max()), float(earlier["low"].min())])

    low, high = float(row["low"]), float(row["high"])
    return any(low - tolerance <= level <= high + tolerance for level in levels)


def _empty_plan(message: str, score: int = 0, reasons=None) -> dict:
    return {
        "signal": "NONE",
        "score": score,
        "max_score": 4,
        "reasons": reasons or [],
        "message": message,
    }


def evaluate(features: pd.DataFrame, position: int, index: str,
             interval_min: int, filters: dict = None) -> dict:
    """Evaluate the latest finished candle against the documented trade rules."""
    if position < 1 or position >= len(features):
        return _empty_plan("A signal candle and its confirmation candle are required.")

    settings = DEFAULT_FILTERS.copy()
    if filters:
        settings.update(filters)

    signal_position = position - 1
    signal_candle = features.iloc[signal_position]
    confirmation = features.iloc[position]
    bullish = [name for name in _BULLISH_PATTERNS if bool(signal_candle.get(name, False))]
    bearish = [name for name in _BEARISH_PATTERNS if bool(signal_candle.get(name, False))]
    if bullish and bearish:
        return _empty_plan("The signal candle has conflicting bullish and bearish patterns.")
    if not bullish and not bearish:
        return _empty_plan("No directional candle pattern on the preceding candle.")
    side = "CE" if bullish else "PE"

    breaks_signal = (
        float(confirmation["close"]) > float(signal_candle["high"])
        if side == "CE"
        else float(confirmation["close"]) < float(signal_candle["low"])
    )
    signal_time = features.index[signal_position]
    decision_time = features.index[position]
    decision_minutes = decision_time.hour * 60 + decision_time.minute
    signal_minutes = signal_time.hour * 60 + signal_time.minute

    expected_trend = "UP" if side == "CE" else "DOWN"
    higher_trend_ok = (
        signal_candle.get("trend_15") == expected_trend
        and signal_candle.get("trend_60") == expected_trend
    )
    adx_value = signal_candle.get("adx")
    adx_ok = pd.notna(adx_value) and float(adx_value) >= ADX_MINIMUM
    time_ok = signal_minutes >= 9 * 60 + 30 and decision_minutes < 15 * 60
    expiry_ok = not (
        decision_minutes >= 13 * 60
        and is_expiry_day(index.lower(), decision_time.date())
    )
    vix_value = signal_candle.get("vix_previous")
    vix_ok = pd.isna(vix_value) or float(vix_value) <= VIX_MAXIMUM
    lunch_ok = not (12 * 60 <= signal_minutes < 13 * 60 + 30)

    reasons = [
        {"ok": bool(breaks_signal), "text": "Next candle confirmed the pattern"},
        {"ok": bool(higher_trend_ok), "text": "15-minute and 60-minute trends agree"},
        {"ok": bool(adx_ok), "text": f"ADX is at least {ADX_MINIMUM:.0f}"},
        {"ok": bool(time_ok), "text": "Signal and entry are within trading hours"},
        {"ok": bool(expiry_ok), "text": "Not after 1:00 pm on expiry day"},
        {
            "ok": bool(vix_ok),
            "text": "Previous-day VIX is not above 22"
            if pd.notna(vix_value) else "VIX unavailable; filter skipped",
        },
    ]
    checks = [
        ("confirmation", breaks_signal),
        ("higher_timeframe", higher_trend_ok),
        ("adx", adx_ok),
        ("time_of_day", time_ok),
        ("expiry_afternoon", expiry_ok),
        ("vix", vix_ok),
        ("lunch", lunch_ok),
    ]
    if any(settings[name] and not ok for name, ok in checks):
        return _empty_plan("One or more required checks did not pass.", reasons=reasons)

    score = 2
    short_trend = signal_candle.get("short_trend")
    if short_trend == expected_trend:
        score += 1
        reasons.append({"ok": True, "text": "Short-term trend agrees"})
    else:
        reasons.append({"ok": False, "text": "Short-term trend does not agree"})

    key_level_ok = _key_level_touched(features, signal_position, interval_min)
    if key_level_ok:
        score += 1
    reasons.append({"ok": key_level_ok, "text": "Signal candle is near a key level"})
    if score < 3:
        return _empty_plan("Agreement score is below the required 3 of 4.", score, reasons)

    entry = float(confirmation["close"])
    stop = float(signal_candle["low"] if side == "CE" else signal_candle["high"])
    risk_points = abs(entry - stop)
    atr = signal_candle.get("atr")
    if pd.isna(atr) or atr <= 0 or not MIN_RISK_ATR * atr <= risk_points <= MAX_RISK_ATR * atr:
        return _empty_plan("Stop distance must be between 0.25 and 1.5 ATR.", score, reasons)

    target = entry + risk_points * REWARD_RISK_RATIO * (1 if side == "CE" else -1)
    strike_step = 50 if index.lower() == "nifty" else 100
    strike = int(round(entry / strike_step) * strike_step)
    name = "NIFTY" if index.lower() == "nifty" else "SENSEX"
    return {
        "signal": side,
        "score": score,
        "max_score": 4,
        "reasons": reasons,
        "message": "Confirmation received. Check the real option price before acting.",
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_points": risk_points,
        "reward_points": risk_points * REWARD_RISK_RATIO,
        "strike": strike,
        "option": f"{name} {strike} {side}",
        "decision_time": decision_time.isoformat(),
        "mode": "confirm",
    }
