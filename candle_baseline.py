"""
candle_baseline.py
------------------
Is the strategy better than CHANCE?  Compares its backtest with RANDOM entries that use the
exact same trade simulator, costs, time window and stop-loss sizes.

Two baselines:
  A  "pure random":   random candle, random side (CE or PE), inside the allowed trading hours.
  B  "trend + ADX":   random candle, but only in the direction both the 15 min and 60 min trend
                      agree on, and only when ADX >= 20. No candle pattern, no confirmation
                      candle. If the strategy does not beat B, the patterns add nothing.

Stop-loss size of every random trade is drawn from the strategy's own trades (in ATR units),
so the comparison is fair. Target is always reward : risk = 2 : 1 like the strategy.

Reads your saved candles (market_data.db). Writes nothing.

Run:
    python candle_baseline.py                          # NIFTY + SENSEX, 5m + 15m
    python candle_baseline.py --index nifty --interval 5m --runs 1000

When two or more sets are run (the default), a POOLED summary is printed at the end: all strategy
trades together against all the random runs together. It is the biggest sample, but the sets
overlap (NIFTY and SENSEX move together, 5m and 15m use the same days), so it flatters the strategy.

How to read the answer
  "p-value" = share of random runs that did AS WELL OR BETTER than the strategy.
  Small (below 0.05) = the strategy beat chance. Large = it is no better than luck.
  With few trades even a p-value below 0.05 is weak evidence. Collect more data.
"""

import argparse
import warnings

import numpy as np
import pandas as pd

import candle_backtest as bt
import candle_signals as cs
import candle_store
from data_sources import INTERVAL_MINUTES

warnings.simplefilter("ignore")


def strategy_risk_atr(trades: list) -> np.ndarray:
    """Stop-loss distance of the strategy's trades, in units of ATR."""
    out = [abs(t["fill"] - t["stop"]) / t["context"]["atr"]
           for t in trades if t.get("context", {}).get("atr")]
    return np.array(out if out else [0.75])


def eligible(f: pd.DataFrame, interval_min: int, baseline: str):
    """Candles where a random trade may be decided, with the side to trade."""
    start = f["start_min"].values
    ok = (np.arange(len(f)) >= 30) & (start >= cs.TRADE_START_MIN + interval_min) \
        & (start + interval_min <= cs.TRADE_END_MIN) & f["atr"].notna().values
    if baseline == "A":
        idx = np.flatnonzero(ok)
        return idx, None
    h15, h60, adx = f["htf15"].values, f["htf60"].values, f["adx"].values
    ok &= (h15 == h60) & (h15 != 0) & (np.nan_to_num(adx) >= cs.ADX_MIN)
    idx = np.flatnonzero(ok)
    return idx, np.where(h15[idx] > 0, 1, -1)


def one_run(f, interval_min, idx, sides, risks_atr, n_pick, rng, cost_r):
    pick = np.sort(rng.choice(len(idx), size=min(n_pick, len(idx)), replace=False))
    close, atr = f["close"].values, f["atr"].values
    results, blocked = [], -1
    for p in pick:
        i = int(idx[p])
        if i <= blocked:
            continue
        sign = int(rng.choice([1, -1])) if sides is None else int(sides[p])
        risk = float(rng.choice(risks_atr)) * atr[i]
        plan = {"signal": "CE" if sign == 1 else "PE", "entry": float(close[i]),
                "risk_points": risk, "mode": "confirm"}
        res = bt.simulate_trade(f, i, plan, interval_min)
        if res["status"] in ("OPEN", "PENDING"):
            break
        if res["status"] == "SKIPPED":
            continue
        results.append(res["r"] - cost_r)
        blocked = res["exit_idx"]
    return results


def baseline_distribution(f, interval_min, baseline, risks_atr, n_trades, runs, seed, cost_r):
    idx, sides = eligible(f, interval_min, baseline)
    if len(idx) < 50:
        return None
    rng = np.random.default_rng(seed)
    n_pick = max(n_trades * 4, 40)     # draw extra candidates, then sample down to n_trades
    means, counts = [], []
    for _ in range(runs):
        r = one_run(f, interval_min, idx, sides, risks_atr, n_pick, rng, cost_r)
        if len(r) > n_trades:      # same number of trades as the strategy, so the spread is fair
            r = list(rng.choice(r, size=n_trades, replace=False))
        if r:
            means.append(np.mean(r))
            counts.append(len(r))
    return np.array(means), float(np.mean(counts)), len(idx)


def report(f, index, interval, runs, seed, cost_r=bt.COST_R):
    interval_min = INTERVAL_MINUTES[interval]
    trades = bt.run_backtest(f, index, interval_min)
    s = bt.stats(trades)
    print("\n" + "=" * 86)
    print(f"{index.upper()} {interval}  |  {len(f)} candles, {len(set(f['day']))} days")
    if s["trades"] == 0:
        print("Strategy took no trades.")
        return None
    print(f"STRATEGY   : {s['trades']:>3} trades | win {s['win_rate'] * 100:>3.0f}% | "
          f"avg {s['avg_r']:+.3f}R | PF {s['profit_factor']:.2f}")
    risks = strategy_risk_atr(trades)
    dists = {}                                   # the random runs, kept for the pooled summary
    for name, label in (("A", "A pure random      "), ("B", "B trend + ADX only ")):
        out = baseline_distribution(f, interval_min, name, risks, s["trades"], runs, seed, cost_r)
        if out is None:
            print(f"{label}: not enough eligible candles")
            continue
        means, avg_n, n_elig = out
        dists[name] = means
        p = float((means >= s["avg_r"]).mean())
        lo, hi = np.percentile(means, [5, 95])
        verdict = ("strategy BEAT chance" if p < 0.05 else
                   "no clear edge over chance" if p > 0.20 else "borderline")
        print(f"{label}: ~{avg_n:>3.0f} trades/run | mean {means.mean():+.3f}R | "
              f"90% of runs between {lo:+.2f} and {hi:+.2f}R | p-value {p:.3f}  -> {verdict}")
    if s["trades"] < 30:
        print("WARNING: fewer than 30 strategy trades. Treat this as a rough hint only.")
    return {"name": f"{index.upper()} {interval}", "n": s["trades"], "avg": s["avg_r"], "dists": dists}


def pooled_report(results: list):
    """
    One combined answer over all the markets / candle sizes that were run: all strategy trades
    together against the random runs of every set added together (weighted by trade count).
    This is the biggest sample, but NIFTY and SENSEX move together, and 5m and 15m use the same
    days, so the p-values here look better than the evidence really is.
    """
    results = [r for r in results if r]
    if len(results) < 2:
        return
    total = sum(r["n"] for r in results)
    avg = sum(r["avg"] * r["n"] for r in results) / total
    print("\n" + "=" * 86)
    print(f"POOLED over {len(results)} sets ({', '.join(r['name'] for r in results)})")
    print(f"STRATEGY   : {total:>3} trades | avg {avg:+.3f}R")
    for name, label in (("A", "A pure random      "), ("B", "B trend + ADX only ")):
        if not all(name in r["dists"] for r in results):
            print(f"{label}: not available for every set")
            continue
        k = min(len(r["dists"][name]) for r in results)
        mixed = sum(r["n"] * r["dists"][name][:k] for r in results) / total
        p = float((mixed >= avg).mean())
        lo, hi = np.percentile(mixed, [5, 95])
        verdict = ("strategy BEAT chance" if p < 0.05 else
                   "no clear edge over chance" if p > 0.20 else "borderline")
        print(f"{label}: mean {mixed.mean():+.3f}R | 90% of runs between {lo:+.2f} and {hi:+.2f}R | "
              f"p-value {p:.3f}  -> {verdict}")
    print("CAREFUL: the sets overlap (NIFTY and SENSEX move together; 5m and 15m use the same days),")
    print("so the pooled p-values look better than the real evidence. Do not trust a small one.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", choices=["nifty", "sensex"])
    ap.add_argument("--interval", choices=list(INTERVAL_MINUTES))
    ap.add_argument("--runs", type=int, default=400, help="random runs per baseline (default 400)")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--db", default=candle_store.DB_FILE)
    a = ap.parse_args()

    vix = candle_store.load_vix(a.db)
    results = []
    for index in ([a.index] if a.index else ["nifty", "sensex"]):
        for interval in ([a.interval] if a.interval else ["5m", "15m"]):
            df = candle_store.load_candles(index, interval, a.db)
            if df.empty:
                print(f"No saved candles for {index} {interval}. Run: python candle_store.py collect")
                continue
            f = cs.compute_features(df, index, INTERVAL_MINUTES[interval], vix)
            results.append(report(f, index, interval, a.runs, a.seed))
    pooled_report(results)                       # only prints when two or more sets were run
    print("\nReminder: past results do not promise future results. Index results in R, not rupees.")


if __name__ == "__main__":
    main()
