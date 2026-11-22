"""
candle_patterns.py
------------------
Step 1 of the NIFTY50 / SENSEX signal tool: the "candle dictionary".

What it does
  - Reads candles (Open, High, Low, Close, Volume)
  - Detects 6 candle patterns using simple rules
  - Checks the trend BEFORE the pattern (a hammer only matters after a fall)
  - Gives every detected pattern a plain-English explanation

What it does NOT do (yet)
  - No live data, no entry / stop loss / target. Those come in the next steps.

Needs:  pip install pandas
Run:    python candle_patterns.py                (runs demo with sample candles)
        python candle_patterns.py my_candles.csv (CSV columns: datetime,open,high,low,close,volume)
"""

import sys
import pandas as pd

# ---------------------------------------------------------------------------
# SETTINGS - you can change these numbers to make rules stricter or looser
# ---------------------------------------------------------------------------
WICK_TO_BODY = 2.0        # hammer / shooting star: long wick must be >= 2x body
DOJI_BODY_PCT = 0.10      # doji: body is <= 10% of the full candle range
MARUBOZU_BODY_PCT = 0.90  # marubozu: body is >= 90% of the full candle range
TREND_LOOKBACK = 3        # how many candles before the pattern we check for trend


# ---------------------------------------------------------------------------
# THE DICTIONARY: meaning of each pattern in simple words
# ---------------------------------------------------------------------------
PATTERN_INFO = {
    "HAMMER": {
        "leans": "CE",
        "meaning": "Sellers pushed the price down, but buyers brought it back up. "
                   "Like a ball pressed into water that jumps back up.",
        "needs": "Works only after a fall (downtrend).",
    },
    "SHOOTING_STAR": {
        "leans": "PE",
        "meaning": "Buyers pushed the price up, but sellers brought it back down. "
                   "Like a ball thrown up that falls straight back.",
        "needs": "Works only after a rise (uptrend).",
    },
    "BULLISH_ENGULFING": {
        "leans": "CE",
        "meaning": "A big green candle fully covers the previous red candle. "
                   "Buyers took control from sellers.",
        "needs": "Stronger after a fall.",
    },
    "BEARISH_ENGULFING": {
        "leans": "PE",
        "meaning": "A big red candle fully covers the previous green candle. "
                   "Sellers took control from buyers.",
        "needs": "Stronger after a rise.",
    },
    "DOJI": {
        "leans": "WAIT",
        "meaning": "Open and close are almost the same. Buyers and sellers are "
                   "equal, market is confused. Wait for the next candle.",
        "needs": "Not a signal by itself.",
    },
    "BULLISH_MARUBOZU": {
        "leans": "CE",
        "meaning": "Big green candle with almost no wick. Buyers were in full "
                   "control the whole time.",
        "needs": "Stronger when volume is high.",
    },
    "BEARISH_MARUBOZU": {
        "leans": "PE",
        "meaning": "Big red candle with almost no wick. Sellers were in full "
                   "control the whole time.",
        "needs": "Stronger when volume is high.",
    },
}


# ---------------------------------------------------------------------------
# STEP 1: measure each candle
# ---------------------------------------------------------------------------
def add_candle_measurements(df: pd.DataFrame) -> pd.DataFrame:
    """Adds body size, wicks, range and colour to every candle."""
    df = df.copy()
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["green"] = df["close"] > df["open"]
    df["red"] = df["close"] < df["open"]
    return df


# ---------------------------------------------------------------------------
# STEP 2: trend before the candle (simple: compare close now vs N candles ago)
# ---------------------------------------------------------------------------
def add_trend_context(df: pd.DataFrame) -> pd.DataFrame:
    """prior_trend = 'DOWN', 'UP' or 'SIDEWAYS' looking at candles BEFORE this one."""
    df = df.copy()
    past_close = df["close"].shift(1)                  # previous candle close
    older_close = df["close"].shift(TREND_LOOKBACK)    # close N candles back
    change = past_close - older_close

    df["prior_trend"] = "SIDEWAYS"
    df.loc[change < 0, "prior_trend"] = "DOWN"
    df.loc[change > 0, "prior_trend"] = "UP"
    return df


# ---------------------------------------------------------------------------
# STEP 3: pattern rules
# ---------------------------------------------------------------------------
def add_patterns(df: pd.DataFrame) -> pd.DataFrame:
    df = add_candle_measurements(df)
    df = add_trend_context(df)

    safe_range = df["range"].replace(0, float("nan"))
    body_pct = df["body"] / safe_range

    # Hammer: long lower wick, small upper wick, after a fall
    df["HAMMER"] = (
        (df["lower_wick"] >= WICK_TO_BODY * df["body"])
        & (df["upper_wick"] <= df["body"])
        & (df["body"] > 0)
        & (df["prior_trend"] == "DOWN")
    )

    # Shooting star: long upper wick, small lower wick, after a rise
    df["SHOOTING_STAR"] = (
        (df["upper_wick"] >= WICK_TO_BODY * df["body"])
        & (df["lower_wick"] <= df["body"])
        & (df["body"] > 0)
        & (df["prior_trend"] == "UP")
    )

    prev_open = df["open"].shift(1)
    prev_close = df["close"].shift(1)
    prev_green = df["green"].shift(1, fill_value=False)
    prev_red = df["red"].shift(1, fill_value=False)

    # Bullish engulfing: red candle, then green candle covering it completely
    df["BULLISH_ENGULFING"] = (
        prev_red & df["green"]
        & (df["open"] <= prev_close)
        & (df["close"] >= prev_open)
    )

    # Bearish engulfing: green candle, then red candle covering it completely
    df["BEARISH_ENGULFING"] = (
        prev_green & df["red"]
        & (df["open"] >= prev_close)
        & (df["close"] <= prev_open)
    )

    # Doji: tiny body compared to full range
    # (if it is also a hammer / shooting star, the long wick matters more, so skip doji)
    df["DOJI"] = ((body_pct <= DOJI_BODY_PCT) & (df["range"] > 0)
                  & ~df["HAMMER"] & ~df["SHOOTING_STAR"])

    # Marubozu: body is almost the full candle
    df["BULLISH_MARUBOZU"] = (body_pct >= MARUBOZU_BODY_PCT) & df["green"]
    df["BEARISH_MARUBOZU"] = (body_pct >= MARUBOZU_BODY_PCT) & df["red"]

    return df


# ---------------------------------------------------------------------------
# STEP 4: explain what was found
# ---------------------------------------------------------------------------
def explain_candle(row: pd.Series) -> list:
    """Returns a list of dicts: pattern name, leaning (CE/PE/WAIT) and meaning."""
    found = []
    for name, info in PATTERN_INFO.items():
        if bool(row.get(name, False)):
            found.append({
                "pattern": name,
                "leans": info["leans"],
                "meaning": info["meaning"],
                "needs": info["needs"],
            })
    return found


def latest_candle_report(df: pd.DataFrame) -> str:
    """Plain-text report for the LAST candle in the data."""
    last = df.iloc[-1]
    lines = []
    lines.append(f"Candle time : {df.index[-1]}")
    lines.append(f"O {last['open']:.2f}  H {last['high']:.2f}  "
                 f"L {last['low']:.2f}  C {last['close']:.2f}")
    lines.append(f"Colour      : {'GREEN' if last['green'] else 'RED' if last['red'] else 'FLAT'}")
    lines.append(f"Trend before: {last['prior_trend']}")

    found = explain_candle(last)
    if not found:
        lines.append("Pattern     : none (no clear candle signal)")
    for f in found:
        lines.append(f"Pattern     : {f['pattern']}  -> leans {f['leans']}")
        lines.append(f"  Meaning   : {f['meaning']}")
        lines.append(f"  Note      : {f['needs']}")
    lines.append("")
    lines.append("Reminder: one candle alone is weak. Use it with support/resistance, "
                 "volume and trend.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loading candles
# ---------------------------------------------------------------------------
def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(df.columns):
        raise ValueError(f"CSV must have columns: {sorted(needed)} (volume optional)")
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
    return df


def sample_candles() -> pd.DataFrame:
    """Small made-up data so you can test without any API. NOT real market data."""
    rows = [
        # open,  high,   low,   close
        (100.0, 101.0, 98.5, 99.0),    # red
        (99.0, 99.5, 97.0, 97.5),      # red
        (97.5, 98.0, 95.5, 96.0),      # red (market falling)
        (96.0, 96.0, 92.0, 95.9),      # hammer after a fall
        (95.8, 99.5, 95.5, 99.2),      # bullish engulfing
        (99.2, 103.0, 99.2, 103.0),    # bullish marubozu
        (103.0, 103.5, 102.5, 103.05), # doji
        (103.05, 106.5, 103.0, 103.3), # shooting star after a rise
    ]
    idx = pd.date_range("2026-01-01 09:15", periods=len(rows), freq="5min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1:
        candles = load_csv(sys.argv[1])
    else:
        print("(No CSV given - using sample candles)\n")
        candles = sample_candles()

    result = add_patterns(candles)

    print("=== ALL CANDLES: patterns found ===")
    pattern_cols = list(PATTERN_INFO.keys())
    for ts, row in result.iterrows():
        names = [p for p in pattern_cols if bool(row[p])]
        print(f"{ts}  C={row['close']:.2f}  trend_before={row['prior_trend']:<8} "
              f"{', '.join(names) if names else '-'}")

    print("\n=== LATEST CANDLE REPORT ===")
    print(latest_candle_report(result))
