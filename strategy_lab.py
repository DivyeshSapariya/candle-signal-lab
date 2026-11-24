"""
strategy_lab.py
---------------
Tests OTHER strategy types on your saved candles, with the same honest rules as the main strategy.
It is research only: it changes no live rule, and writes nothing.

The four strategies are written down here BEFORE looking at any result, with fixed numbers. They
are never tuned afterwards (tuning on the same data is how you fool yourself).

  1. orb        Opening-range breakout. First close outside the first-30-minute range. Stop = the other side
                of the range (kept between 0.25 and 1.5 ATR). Reward : risk = 2.
  2. vwap_pull  VWAP pullback in a trend. 15m and 60m trend agree; the candle dips to VWAP and closes back on
                the trend side, in the trend colour. Stop = the candle's extreme. Reward : risk = 2.
  3. trend_pull Trend continuation. EMA 9 above EMA 21 (or below), ADX 20+, 15m trend agrees; the price
                closes back through EMA 9 after a candle on the wrong side. Stop = the pullback extreme. R:R = 2.
  4. mean_rev   Mean reversion on a quiet day (ADX under 20). Price closes 1.5 ATR away from VWAP, and the
                candle turns back. Stop = the candle's extreme. R:R = 1 (a short target, the natural one here).

Same trade simulation, costs, trading hours and one-trade-at-a-time rule as the main backtest.

THE GATE (a strategy counts as a real candidate only if ALL are true, for that market and candle size)
  - at least 60 trades in the year
  - average result above 0 after a 0.1 R cost
  - positive in at least 3 of 4 walk-forward blocks
  - the 95% bootstrap range of the average is entirely above 0
  - beats random entries (same stops, same reward : risk, same count) with p-value below 0.01
    (strict on purpose: 16 tests are run, so a few "wins" appear by luck at 0.05)

Run:   python strategy_lab.py                       (all four strategies, NIFTY + SENSEX, 5m + 15m)
       python strategy_lab.py --index nifty --interval 5m --runs 500
"""

import argparse
import warnings

import numpy as np
import pandas as pd

import candle_backtest as bt
import candle_research as rs
import candle_signals as cs
import candle_store
from data_sources import INTERVAL_MINUTES

warnings.simplefilter("ignore")

MIN_TRADES = 60
GATE_BLOCKS_POSITIVE = 3
GATE_P_VALUE = 0.01
MEAN_REV_ATR = 1.5            # how far from VWAP counts as "stretched", in ATR
QUIET_ADX = 20                # mean reversion only when ADX is below this


# ---------------------------------------------------------------------------
# Signals: decided at the CLOSE of candle i, using only candles up to i
# ---------------------------------------------------------------------------
def _risk(entry: float, extreme: float, atr: float) -> float:
    """Distance to the stop, kept between 0.25 and 1.5 ATR (same as the main strategy)."""
    return float(min(max(abs(entry - extreme), cs.MIN_RISK_ATR * atr), cs.MAX_RISK_ATR * atr))


def _window(f: pd.DataFrame, interval_min: int) -> np.ndarray:
    """Candles where a trade may be decided: not in the first minutes, finished before 3 pm."""
    start = f["start_min"].values
    return ((np.arange(len(f)) >= 30) & (start >= cs.TRADE_START_MIN + interval_min)
            & (start + interval_min <= cs.TRADE_END_MIN) & f["atr"].notna().values)


def signals_orb(f, interval_min):
    sides, risks = np.zeros(len(f), dtype=int), np.zeros(len(f))
    ok = _window(f, interval_min)
    close, high, low, atr = (f[k].values for k in ("close", "high", "low", "atr"))
    orh, orl, day = f["or_high"].values, f["or_low"].values, f["day"].values
    broke_today = None
    for i in range(len(f)):
        if i == 0 or day[i] != day[i - 1]:
            broke_today = False
        if np.isnan(orh[i]) or np.isnan(orl[i]):
            continue                                         # the opening range is not finished yet
        outside = close[i] > orh[i] or close[i] < orl[i]
        if outside and not broke_today:
            broke_today = True                               # only the FIRST breakout of the day counts
            if ok[i]:
                if close[i] > orh[i]:
                    sides[i], risks[i] = 1, _risk(close[i], orl[i], atr[i])
                else:
                    sides[i], risks[i] = -1, _risk(close[i], orh[i], atr[i])
    return sides, risks


def signals_vwap_pull(f, interval_min):
    sides, risks = np.zeros(len(f), dtype=int), np.zeros(len(f))
    ok = _window(f, interval_min)
    o, h, l, c = (f[k].values for k in ("open", "high", "low", "close"))
    atr, vwap = f["atr"].values, f["vwap"].values
    h15, h60 = f["htf15"].values, f["htf60"].values
    for i in np.flatnonzero(ok & ~np.isnan(vwap)):
        if h15[i] == 1 and h60[i] == 1 and l[i] <= vwap[i] < c[i] and c[i] > o[i]:
            sides[i], risks[i] = 1, _risk(c[i], l[i], atr[i])
        elif h15[i] == -1 and h60[i] == -1 and h[i] >= vwap[i] > c[i] and c[i] < o[i]:
            sides[i], risks[i] = -1, _risk(c[i], h[i], atr[i])
    return sides, risks


def signals_trend_pull(f, interval_min):
    sides, risks = np.zeros(len(f), dtype=int), np.zeros(len(f))
    ok = _window(f, interval_min)
    o, h, l, c = (f[k].values for k in ("open", "high", "low", "close"))
    atr, adx = f["atr"].values, f["adx"].values
    e9, e21, h15 = f["ema9"].values, f["ema21"].values, f["htf15"].values
    for i in np.flatnonzero(ok & (np.nan_to_num(adx) >= cs.ADX_MIN)):
        if e9[i] > e21[i] and h15[i] == 1 and c[i - 1] < e9[i - 1] and c[i] > e9[i] and c[i] > o[i]:
            sides[i], risks[i] = 1, _risk(c[i], min(l[i], l[i - 1]), atr[i])
        elif e9[i] < e21[i] and h15[i] == -1 and c[i - 1] > e9[i - 1] and c[i] < e9[i] and c[i] < o[i]:
            sides[i], risks[i] = -1, _risk(c[i], max(h[i], h[i - 1]), atr[i])
    return sides, risks


def signals_mean_rev(f, interval_min):
    sides, risks = np.zeros(len(f), dtype=int), np.zeros(len(f))
    ok = _window(f, interval_min)
    o, h, l, c = (f[k].values for k in ("open", "high", "low", "close"))
    atr, adx, vwap = f["atr"].values, f["adx"].values, f["vwap"].values
    for i in np.flatnonzero(ok & ~np.isnan(vwap) & (np.nan_to_num(adx, nan=99) < QUIET_ADX)):
        stretch = (c[i] - vwap[i]) / atr[i]
        if stretch <= -MEAN_REV_ATR and c[i] > o[i]:
            sides[i], risks[i] = 1, _risk(c[i], l[i], atr[i])
        elif stretch >= MEAN_REV_ATR and c[i] < o[i]:
            sides[i], risks[i] = -1, _risk(c[i], h[i], atr[i])
    return sides, risks


STRATEGIES = {
    "orb": (signals_orb, 2.0, "Opening-range breakout"),
    "vwap_pull": (signals_vwap_pull, 2.0, "VWAP pullback in a trend"),
    "trend_pull": (signals_trend_pull, 2.0, "Trend continuation after a pullback"),
    "mean_rev": (signals_mean_rev, 1.0, "Mean reversion on a quiet day"),
}


# ---------------------------------------------------------------------------
# Run a strategy (same simulator and one-trade-at-a-time rule as the main backtest)
# ---------------------------------------------------------------------------
def run_strategy(f, interval_min, name, cost_r=bt.COST_R, i_start=30, i_end=None):
    fn, rr, _ = STRATEGIES[name]
    sides, risks = fn(f, interval_min)
    i_end = len(f) - 1 if i_end is None else min(i_end, len(f) - 1)
    atr, close = f["atr"].values, f["close"].values
    trades, i = [], max(i_start, 30)
    while i < i_end:
        if sides[i] == 0:
            i += 1
            continue
        plan = {"signal": "CE" if sides[i] == 1 else "PE", "entry": float(close[i]),
                "risk_points": float(risks[i]), "mode": "confirm"}
        res = bt.simulate_trade(f, i, plan, interval_min, rr)
        if res["status"] == "SKIPPED":
            i += 1
            continue
        if res["status"] in ("OPEN", "PENDING"):
            break
        trades.append({
            "time": f.index[i].isoformat(), "side": plan["signal"], "fill": res["fill"],
            "stop": res["stop"], "status": res["status"], "r": res["r"],
            "r_net": round(res["r"] - cost_r, 3), "bars": res["bars"],
            "context": {"atr": float(atr[i]), "start_min": int(f["start_min"].values[i]),
                        "pattern": name},
        })
        i = res["exit_idx"] + 1
    return trades


# ---------------------------------------------------------------------------
# Random entries with the same stops, reward : risk and number of trades
# ---------------------------------------------------------------------------
def random_baseline(f, interval_min, trades, rr, runs, seed, cost_r=bt.COST_R):
    """Average net R of `runs` random-entry sets (random candle, random side) shaped like the strategy."""
    ok = np.flatnonzero(_window(f, interval_min))
    risks = np.array([abs(t["fill"] - t["stop"]) / t["context"]["atr"] for t in trades])
    n = len(trades)
    if len(ok) < 50 or n == 0:
        return None
    rng = np.random.default_rng(seed)
    close, atr = f["close"].values, f["atr"].values
    means = []
    for _ in range(runs):
        pick = np.sort(rng.choice(ok, size=min(len(ok), n * 4), replace=False))
        out, blocked = [], -1
        for i in pick:
            if i <= blocked:
                continue
            sign = int(rng.choice([1, -1]))
            plan = {"signal": "CE" if sign == 1 else "PE", "entry": float(close[i]),
                    "risk_points": float(rng.choice(risks)) * atr[i], "mode": "confirm"}
            res = bt.simulate_trade(f, int(i), plan, interval_min, rr)
            if res["status"] in ("OPEN", "PENDING"):
                break
            if res["status"] == "SKIPPED":
                continue
            out.append(res["r"] - cost_r)
            blocked = res["exit_idx"]
        if len(out) > n:
            out = list(rng.choice(out, size=n, replace=False))
        if out:
            means.append(float(np.mean(out)))
    return np.array(means)


# ---------------------------------------------------------------------------
# Judge one strategy on one market
# ---------------------------------------------------------------------------
def blocks_positive(f, interval_min, name, blocks=rs.BLOCKS):
    days = list(pd.unique(f["day"]))
    edges = np.linspace(0, len(days), blocks + 1).astype(int)
    out = []
    for b in range(blocks):
        a, z = days[edges[b]], days[edges[b + 1] - 1]
        idx = np.where((f["day"].values >= a) & (f["day"].values <= z))[0]
        t = run_strategy(f, interval_min, name, i_start=int(idx[0]), i_end=int(idx[-1]))
        out.append((len(t), float(np.mean([x["r_net"] for x in t])) if t else 0.0))
    return out


def judge(f, index, interval, name, runs, seed=11):
    interval_min = INTERVAL_MINUTES[interval]
    rr = STRATEGIES[name][1]
    trades = run_strategy(f, interval_min, name)
    res = {"strategy": name, "market": f"{index.upper()} {interval}", "trades": len(trades)}
    if not trades:
        res.update(avg=None, passed=False, why="no trades")
        return res
    net = np.array([t["r_net"] for t in trades])
    wins = sum(1 for t in trades if t["status"] == "TARGET")
    res.update(avg=float(net.mean()), win=wins / len(trades),
               avg_cost2=float(np.mean([t["r"] - 0.2 for t in trades])))
    ci = rs.bootstrap_avg_r(trades)
    res["ci"] = ci
    blocks = blocks_positive(f, interval_min, name)
    res["blocks"] = blocks
    res["blocks_pos"] = sum(1 for n, a in blocks if n and a > 0)
    dist = random_baseline(f, interval_min, trades, rr, runs, seed)
    res["p"] = None if dist is None else float((dist >= net.mean()).mean())

    fails = []
    if len(trades) < MIN_TRADES:
        fails.append(f"only {len(trades)} trades")
    if net.mean() <= 0:
        fails.append("average not above 0")
    if res["blocks_pos"] < GATE_BLOCKS_POSITIVE:
        fails.append(f"positive in {res['blocks_pos']} of {len(blocks)} blocks")
    if ci is None or ci[0] <= 0:
        fails.append("95% range includes 0")
    if res["p"] is None or res["p"] >= GATE_P_VALUE:
        fails.append("does not beat random entries")
    res["passed"], res["why"] = not fails, "; ".join(fails)
    return res


def reference_row(f, index, interval):
    """The current candle-pattern rules, for comparison (same data, same costs)."""
    t = bt.run_backtest(f, index, INTERVAL_MINUTES[interval])
    s = bt.stats(t)
    return {"trades": s["trades"], "avg": s.get("avg_r"), "win": s.get("win_rate")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", choices=["nifty", "sensex"])
    ap.add_argument("--interval", choices=["5m", "15m"])
    ap.add_argument("--strategy", choices=list(STRATEGIES))
    ap.add_argument("--runs", type=int, default=300, help="random runs per test (default 300)")
    ap.add_argument("--db", default=candle_store.DB_FILE)
    a = ap.parse_args()

    results = []
    for index in ([a.index] if a.index else ["nifty", "sensex"]):
        for interval in ([a.interval] if a.interval else ["5m", "15m"]):
            df = candle_store.load_candles(index, interval, a.db)
            if len(df) < 500:
                print(f"{index.upper()} {interval}: not enough saved candles")
                continue
            fut = candle_store.load_futures(index, interval, a.db)
            f = cs.compute_features(df, index, INTERVAL_MINUTES[interval], candle_store.load_vix(a.db),
                                    fut if len(fut) else None)
            days = len(pd.unique(f["day"]))
            ref = reference_row(f, index, interval)
            print(f"\n{'=' * 96}\n{index.upper()} {interval}: {days} trading days. "
                  f"Current candle-pattern rules for comparison: {ref['trades']} trades, avg {ref['avg']:+.2f}R, "
                  f"win {ref['win'] * 100:.0f}%")
            for name in ([a.strategy] if a.strategy else STRATEGIES):
                r = judge(f, index, interval, name, a.runs)
                results.append(r)
                if r["avg"] is None:
                    print(f"  {name:<11} no trades")
                    continue
                lo, hi = r["ci"] if r["ci"] else (float("nan"), float("nan"))
                bl = " ".join(f"{x:+.2f}" for _, x in r["blocks"])
                print(f"  {name:<11} trades {r['trades']:>3} | win {r['win'] * 100:>3.0f}% | avg {r['avg']:+.2f}R "
                      f"(95%: {lo:+.2f}..{hi:+.2f}) | at 0.2R cost {r['avg_cost2']:+.2f} | "
                      f"blocks [{bl}] | p vs random {r['p']:.3f}")
                print(f"              GATE: {'PASS' if r['passed'] else 'fail'}"
                      f"{'' if r['passed'] else ' - ' + r['why']}")

    if results:
        done = [r for r in results if r["avg"] is not None]
        passed = [r for r in done if r["passed"]]
        print(f"\n{'=' * 96}\nSUMMARY: {len(done)} tests, {len(passed)} passed the gate.")
        for r in passed:
            print(f"  PASSED: {r['strategy']} on {r['market']} ({r['trades']} trades, avg {r['avg']:+.2f}R)")
        print("A pass is a CANDIDATE, not a proof. Check it again on new data before using it live.")


if __name__ == "__main__":
    main()
