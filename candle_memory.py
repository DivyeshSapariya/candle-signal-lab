"""
candle_memory.py
----------------
The tool's memory. It works like a person who keeps a trading diary and reads it later.

What it remembers
  Every finished idea (from the live journal, and optionally from a backtest) is stored with
  the facts of that moment: candle pattern, CE / PE, time of day, trend strength (ADX),
  key level, India VIX, and what happened (target / stop loss / time exit, result in R).

What it does with it
  1. RECALL: when a new idea appears, it finds similar past ideas and shows how they ended.
     "Last 12 times a Bearish Engulfing PE appeared before 10:30, 3 won."
  2. LESSONS: it finds which kinds of setups worked best and which failed most.

Honest limits (please read)
  - This is statistics from remembered results, not human understanding.
  - With few examples, results are mostly luck. Below 8 similar ideas the memory says "not
    enough experience". Lessons also need at least 8 examples.
  - Backtest memory comes from the same past data the rules were tuned on, so it can look better
    than the future will be. Live journal results are the more honest teacher.
  - The market changes. Old lessons can stop working.
  - By default the memory only ADVISES. It does not block ideas. To let it block setups with a
    bad record, set BLOCK_BAD_SETUPS = True below (only after 20+ similar examples).

Files: trade_memory.csv (created automatically)
"""

import os
import threading

import pandas as pd

MEMORY_FILE = "trade_memory.csv"
COLUMNS = ["key", "source", "index", "interval", "time", "side", "pattern", "score",
           "adx_bucket", "time_bucket", "level_kind", "vix_bucket", "expiry",
           "status", "r_net"]

MIN_SAMPLES = 8              # fewer similar ideas than this = "not enough experience"
MIN_SAMPLES_TO_BLOCK = 20    # the memory may only block a setup after this many examples
BLOCK_BAD_SETUPS = False     # False = advise only (recommended)
COST_R = 0.10
REWARD_TO_RISK = 2.0
BREAKEVEN = (1 + COST_R) / (REWARD_TO_RISK + 1)      # about 37% wins needed

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Buckets: turn numbers into simple groups, so similar situations can be matched
# ---------------------------------------------------------------------------
def adx_bucket(adx) -> str:
    if adx is None or pd.isna(adx):
        return "n/a"
    return "20-25" if adx < 25 else "25-35" if adx < 35 else "35+"


def time_bucket(start_min) -> str:
    if start_min < 10 * 60 + 30:
        return "09:30-10:30"
    if start_min < 12 * 60:
        return "10:30-12:00"
    if start_min < 13 * 60 + 30:
        return "12:00-13:30"
    return "13:30-15:00"


def vix_bucket(vix) -> str:
    if vix is None or pd.isna(vix):
        return "n/a"
    return "under 13" if vix < 13 else "13-17" if vix < 17 else "17+"


def pretty(name: str) -> str:
    return str(name).replace("_", " ").title()


def make_experience(key: str, source: str, index: str, interval: str, time: str, side: str,
                    score: int, ctx: dict, status: str, r_net: float) -> dict:
    return {
        "key": key, "source": source, "index": index, "interval": interval, "time": time,
        "side": side, "pattern": ctx.get("pattern", ""), "score": score,
        "adx_bucket": adx_bucket(ctx.get("adx")),
        "time_bucket": time_bucket(int(ctx.get("start_min", 0))),
        "level_kind": ctx.get("level_kind", ""),
        "vix_bucket": vix_bucket(ctx.get("vix")),
        "expiry": "expiry day" if ctx.get("is_expiry") else "normal day",
        "status": status, "r_net": round(float(r_net), 3),
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def load() -> pd.DataFrame:
    if not os.path.exists(MEMORY_FILE):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(MEMORY_FILE, dtype=str, keep_default_na=False)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["r_net"] = pd.to_numeric(df["r_net"], errors="coerce")
    return df[COLUMNS]


def _save(df: pd.DataFrame):
    df.to_csv(MEMORY_FILE, index=False)


def add_experiences(rows: list) -> int:
    """Add new experiences (duplicates are ignored). Returns how many were new."""
    if not rows:
        return 0
    with _lock:
        df = load()
        known = set(df["key"].astype(str))
        fresh = [r for r in rows if str(r["key"]) not in known]
        if fresh:
            new_df = pd.DataFrame(fresh, columns=COLUMNS).astype(str)
            _save(pd.concat([df.astype(str), new_df], ignore_index=True))
        return len(fresh)


def replace_backtest(index: str, interval: str, trades: list) -> int:
    """Store backtest trades as experience. Older backtest memory of the same index / interval is replaced."""
    rows = []
    for n, t in enumerate(trades):
        ctx = t.get("context") or {}
        if not ctx:
            continue
        rows.append(make_experience(f"bt|{index}|{interval}|{t['time']}|{t['side']}", "backtest",
                                    index, interval, t["time"], t["side"], int(t["score"]),
                                    ctx, t["status"], t["r_net"]))
    with _lock:
        df = load()
        keep = df[~((df["source"] == "backtest") & (df["index"] == index) & (df["interval"] == interval))]
        new_df = pd.DataFrame(rows, columns=COLUMNS)
        _save(pd.concat([keep.astype(str), new_df.astype(str)], ignore_index=True))
    return len(rows)


# ---------------------------------------------------------------------------
# Numbers for a group of experiences
# ---------------------------------------------------------------------------
def group_stats(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "smooth_win_rate": None, "avg_r": None}
    wins = int((df["status"] == "TARGET").sum())
    # Smoothed win rate: starts from the break-even guess and moves towards the real rate as examples grow.
    smooth = (wins + 1.0) / (n + 3.0)
    return {"n": n, "wins": wins, "win_rate": wins / n, "smooth_win_rate": smooth,
            "avg_r": float(df["r_net"].mean())}


def _verdict(g: dict) -> str:
    if g["n"] < MIN_SAMPLES:
        return "unsure"
    if g["smooth_win_rate"] >= BREAKEVEN + 0.05 and g["avg_r"] > 0:
        return "agrees"
    if g["smooth_win_rate"] < BREAKEVEN - 0.03 or g["avg_r"] < -0.1:
        return "warns"
    return "unsure"


# ---------------------------------------------------------------------------
# RECALL: how did similar past ideas end?
# ---------------------------------------------------------------------------
def recall(plan: dict, index: str, interval_min: int) -> dict:
    ctx = plan.get("context") or {}
    df = load()
    df = df[df["interval"] == f"{interval_min}m"]
    if df.empty or not ctx:
        return {"verdict": "unsure", "n": 0, "win_rate": None, "avg_r": None,
                "text": "No past experience saved yet. The memory grows as ideas finish "
                        "(run candle_backtest.py --learn to give it a head start)."}

    side = plan["signal"]
    mine = {
        "pattern": ctx.get("pattern", ""), "time_bucket": time_bucket(int(ctx.get("start_min", 0))),
        "adx_bucket": adx_bucket(ctx.get("adx")),
    }
    tiers = [
        ("same pattern, same side, same time of day and similar trend strength",
         (df["side"] == side) & (df["pattern"] == mine["pattern"])
         & (df["time_bucket"] == mine["time_bucket"]) & (df["adx_bucket"] == mine["adx_bucket"])),
        ("same pattern, same side and same time of day",
         (df["side"] == side) & (df["pattern"] == mine["pattern"]) & (df["time_bucket"] == mine["time_bucket"])),
        ("same pattern and same side",
         (df["side"] == side) & (df["pattern"] == mine["pattern"])),
        ("same side (any pattern)", (df["side"] == side)),
    ]
    found_counts = []
    for label, mask in tiers:
        sub = df[mask]
        g = group_stats(sub)
        found_counts.append(g["n"])
        if g["n"] >= MIN_SAMPLES:
            verdict = _verdict(g)
            win_text = f"{g['wins']} of {g['n']} hit the target ({g['win_rate'] * 100:.0f}%), average {g['avg_r']:+.2f}R"
            tail = {"agrees": "The memory agrees with this idea.",
                    "warns": "The memory WARNS: this kind of setup has not worked well so far.",
                    "unsure": "The memory is neutral about this setup."}[verdict]
            return {"verdict": verdict, "n": g["n"], "win_rate": g["win_rate"], "avg_r": g["avg_r"],
                    "tier": label, "text": f"Similar past ideas ({label}): {win_text}. {tail}"}
    best = max(found_counts) if found_counts else 0
    return {"verdict": "unsure", "n": best, "win_rate": None, "avg_r": None,
            "text": f"Not enough experience yet (only {best} similar past ideas, need {MIN_SAMPLES})."}


def should_block(mem: dict) -> bool:
    return bool(BLOCK_BAD_SETUPS and mem and mem.get("verdict") == "warns"
                and mem.get("n", 0) >= MIN_SAMPLES_TO_BLOCK)


# ---------------------------------------------------------------------------
# LESSONS: what worked, what failed
# ---------------------------------------------------------------------------
DIMENSIONS = [
    ("pattern", "Pattern", pretty),
    ("side", "Direction", str),
    ("time_bucket", "Time of day", str),
    ("adx_bucket", "Trend strength (ADX)", str),
    ("level_kind", "Key level", lambda v: {"touch": "touching a level", "breakout": "breaking a level"}.get(v, v)),
    ("vix_bucket", "India VIX", str),
    ("expiry", "Day type", str),
]


def lessons(source: str = None) -> dict:
    df = load()
    if source:
        df = df[df["source"] == source]
    overall = group_stats(df)
    items = []
    for column, title, fmt in DIMENSIONS:
        for value, sub in df.groupby(column):
            if value in ("", "n/a", "unchecked"):
                continue
            g = group_stats(sub)
            if g["n"] < MIN_SAMPLES:
                continue
            items.append({"title": f"{title}: {fmt(value)}", "n": g["n"], "wins": g["wins"],
                          "win_rate": g["win_rate"], "avg_r": g["avg_r"],
                          "text": f"{title}: {fmt(value)} - {g['wins']} of {g['n']} hit target "
                                  f"({g['win_rate'] * 100:.0f}%), average {g['avg_r']:+.2f}R"})
    strengths = sorted([i for i in items if i["avg_r"] > 0 and i["win_rate"] > BREAKEVEN],
                       key=lambda i: i["avg_r"], reverse=True)[:4]
    weaknesses = sorted([i for i in items if i["avg_r"] < 0], key=lambda i: i["avg_r"])[:4]
    return {
        "experiences": int(len(df)),
        "live": int((load()["source"] == "live").sum()),
        "backtest": int((load()["source"] == "backtest").sum()),
        "overall": None if overall["n"] == 0 else {
            "n": overall["n"], "win_rate": overall["win_rate"], "avg_r": overall["avg_r"]},
        "strengths": [i["text"] for i in strengths],
        "weaknesses": [i["text"] for i in weaknesses],
        "breakeven": BREAKEVEN,
        "min_samples": MIN_SAMPLES,
    }
