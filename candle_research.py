"""
candle_research.py
------------------
Honest checks on the saved candles (market_data.db). It changes no rules.

  1. Walk-forward  : the same rules on 4 separate blocks of days. A real edge shows up in most.
  2. Cost stress   : the result when each trade costs 0.0 / 0.1 / 0.2 / 0.3 R (slippage, spread).
  3. Uncertainty   : a 95% range for the average R per trade (bootstrap). If it includes 0, the
                     result could easily be luck.
  4. Breakdown     : by hour, by pattern, by side. Only for finding ideas to test, never to trust:
                     small groups look good or bad by chance.

Run:   python candle_research.py            (all saved markets)
       python candle_research.py --index nifty --interval 5m
"""

import argparse

import numpy as np
import pandas as pd

import candle_store
from candle_backtest import run_backtest, stats, one_line, COST_R
from candle_signals import compute_features, FILTER_LABELS, REWARD_TO_RISK
from data_sources import INTERVAL_MINUTES

BLOCKS = 4
COSTS = (0.0, 0.1, 0.2, 0.3)
MIN_GROUP = 8                 # smaller groups are not shown in the breakdown


def with_cost(trades: list, cost_r: float) -> list:
    return [{**t, "r_net": t["r"] - cost_r} for t in trades]


def bootstrap_avg_r(trades: list, runs: int = 5000, seed: int = 7):
    """95% range for the average net R per trade, by resampling the trades."""
    if len(trades) < 5:
        return None
    r = np.array([t["r_net"] for t in trades])
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(runs, len(r)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def walk_forward(f, index, interval_min, blocks=BLOCKS, rr=REWARD_TO_RISK, cost_r=COST_R):
    days = list(pd.unique(f["day"]))
    if len(days) < blocks * 3:
        return []
    edges = np.linspace(0, len(days), blocks + 1).astype(int)
    out = []
    for b in range(blocks):
        first_day, last_day = days[edges[b]], days[edges[b + 1] - 1]
        idx = np.where((f["day"].values >= first_day) & (f["day"].values <= last_day))[0]
        s = stats(run_backtest(f, index, interval_min, None, rr, cost_r,
                               i_start=int(idx[0]), i_end=int(idx[-1])), rr, cost_r)
        out.append((f"Block {b + 1}: {first_day} to {last_day}", s))
    return out


def breakdown(trades: list, key):
    groups = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t["r_net"])
    rows = [(k, len(v), float(np.mean(v)), sum(1 for x in v if x > 0) / len(v))
            for k, v in groups.items() if len(v) >= MIN_GROUP]
    return sorted(rows, key=lambda r: -r[2])


def report(index: str, interval: str, db: str):
    df = candle_store.load_candles(index, interval, db)
    if df is None or len(df) < 100:
        print(f"\n{index.upper()} {interval}: not enough saved candles (run: python candle_store.py collect)")
        return
    interval_min = INTERVAL_MINUTES[interval]
    f = compute_features(df, index, interval_min, candle_store.load_vix(db))
    days = len(pd.unique(f["day"]))
    print(f"\n{'=' * 78}\n{index.upper()} {interval}: {len(f)} candles, {days} trading days\n{'=' * 78}")

    base = run_backtest(f, index, interval_min, None, REWARD_TO_RISK, 0.0)
    print(f"\nAll trades with the default rules: {len(base)}")
    if len(base) < 30:
        print("WARNING: fewer than 30 trades. Every number below is weak evidence.")

    print("\n1) WALK-FORWARD (same rules, separate blocks of days)")
    wf = walk_forward(f, index, interval_min)
    if not wf:
        print("   Not enough days.")
    for label, s in wf:
        print("  ", one_line(label, s))
    good = sum(1 for _, s in wf if s["trades"] and s["avg_r"] > 0)
    if wf:
        print(f"   Profitable blocks: {good} of {len(wf)}")

    print("\n2) COST STRESS (what if each trade costs more?)")
    for c in COSTS:
        print("  ", one_line(f"Cost {c:.1f} R per trade", stats(with_cost(base, c), REWARD_TO_RISK, c)))

    print("\n3) UNCERTAINTY (95% range for average R per trade, after 0.1 R cost)")
    t01 = with_cost(base, COST_R)
    ci = bootstrap_avg_r(t01)
    if ci is None:
        print("   Too few trades to say anything.")
    else:
        avg = float(np.mean([t["r_net"] for t in t01]))
        verdict = ("includes zero: could easily be luck" if ci[0] <= 0 <= ci[1]
                   else "all above zero" if ci[0] > 0 else "all below zero")
        print(f"   average {avg:+.2f} R, range {ci[0]:+.2f} to {ci[1]:+.2f} R  ({verdict})")

    print("\n5) EXTRA CHECKS (default rules + one extra check; VWAP is already inside the default rules)")
    print("  ", one_line("Default rules", stats(t01, REWARD_TO_RISK, COST_R)))
    for name in ("volume", "gap"):
        if name == "volume" and not f["vol_ratio"].notna().any():
            print(f"   {FILTER_LABELS[name][:44]:<44} not available: Yahoo gives no index volume")
            continue
        trades = run_backtest(f, index, interval_min, {name: True}, REWARD_TO_RISK, COST_R)
        print("  ", one_line(f"+ {FILTER_LABELS[name]}"[:44], stats(trades, REWARD_TO_RISK, COST_R)))
    if f["vwap_proxy"].iloc[0]:
        print("   (VWAP here is the plain average price of the day, because there is no volume.)")

    print(f"\n4) BREAKDOWN (groups of {MIN_GROUP}+ trades only; for ideas, not proof)")
    for title, key in (
        ("By hour of the day", lambda t: f"{t['context']['start_min'] // 60:02d}:00"),
        ("By pattern", lambda t: t["context"]["pattern"]),
        ("By side", lambda t: t["side"]),
    ):
        rows = breakdown(t01, key)
        print(f"   {title}")
        if not rows:
            print("     (no group is big enough)")
        for k, n, avg, win in rows:
            print(f"     {str(k):<22} trades {n:>3} | wins {win * 100:>3.0f}% | avg {avg:+.2f} R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", choices=["nifty", "sensex"])
    ap.add_argument("--interval", choices=list(INTERVAL_MINUTES))
    ap.add_argument("--db", default=candle_store.DB_FILE)
    a = ap.parse_args()
    for index in ([a.index] if a.index else ["nifty", "sensex"]):
        for interval in ([a.interval] if a.interval else ["5m", "15m"]):
            report(index, interval, a.db)
    print("\nReminder: these are past results on a short history. Small groups and short blocks "
          "are noisy. A real edge needs months of data and a result that holds in most blocks.")


if __name__ == "__main__":
    main()
