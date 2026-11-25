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

THE ROOM-CHECK EXPERIMENT (python strategy_lab.py --room-check)
  Question: should an idea be skipped when a key level sits between the entry and its 2 R target? Written down before
  looking at any result: "room" = distance to the nearest opposing key level ahead (CE: previous day high, opening
  range high, recent swing high; PE: the low versions), in units of the stop distance. The check passes when there is
  no level ahead or the level is at least 2.0 R away (the target distance). The threshold and the levels are fixed.
  Two questions per market and pooled:
     a) Does it discriminate? Average result of the trades it keeps minus the ones it removes, with a 95% bootstrap range.
     b) Do the rules WITH the check pass the same gate as everything else (section above)?
  The check is only a candidate if the pooled difference is clearly above 0 (95% range above 0) AND the rules with the
  check have a better average than without it in at least 3 of the 4 markets. Even then it must pass the gate before use.

THE SHORT-TARGET EXPERIMENT (python strategy_lab.py --short-targets)
  Same entries and stops as the CURRENT candle-pattern rules (default filters), but a smaller target, written down
  before looking at any result and never tuned:
     r0.5, r1.0     the target is 0.5 / 1.0 times the stop distance on the index (today it is 2.0)
     rs10, rs20     the target is where an at-the-money option has gained Rs 10 / Rs 20 per share
     pct30, pct50   the target is where an at-the-money option has gained 30% / 50% of its price
  The option variants turn the gain into an index distance with Black-Scholes (the option's price at entry from the
  previous day's India VIX and the time to the nearest weekly expiry). That distance divided by the stop distance is
  the reward : risk of that trade (kept between 0.1 and 6). Time decay after the entry is NOT included, so these
  variants are a little EASIER than reality. Random entries with the same stops and the same reward : risk pairs are
  the baseline, and the same gate applies (so a short target must prove itself like anything else).

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
import option_pricer as op
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
    """
    Average net R of `runs` random-entry sets (random candle, random side) shaped like the strategy.
    rr=None: every trade has its own reward : risk (trade["rr"]); each random entry copies the stop size AND the
    reward : risk of one real trade, so the baseline has the same shape.
    """
    ok = np.flatnonzero(_window(f, interval_min))
    risks = np.array([abs(t["fill"] - t["stop"]) / t["context"]["atr"] for t in trades])
    variable = rr is None
    rrs = np.array([t["rr"] for t in trades]) if variable else None
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
            if variable:
                k = int(rng.integers(len(risks)))
                risk_points, rr_i = float(risks[k]) * atr[i], float(rrs[k])
            else:
                risk_points, rr_i = float(rng.choice(risks)) * atr[i], rr
            plan = {"signal": "CE" if sign == 1 else "PE", "entry": float(close[i]),
                    "risk_points": risk_points, "mode": "confirm"}
            res = bt.simulate_trade(f, int(i), plan, interval_min, rr_i)
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
def blocks_positive(f, interval_min, name, blocks=rs.BLOCKS, run=None):
    run = run or (lambda a, z: run_strategy(f, interval_min, name, i_start=a, i_end=z))
    days = list(pd.unique(f["day"]))
    edges = np.linspace(0, len(days), blocks + 1).astype(int)
    out = []
    for b in range(blocks):
        a, z = days[edges[b]], days[edges[b + 1] - 1]
        idx = np.where((f["day"].values >= a) & (f["day"].values <= z))[0]
        t = run(int(idx[0]), int(idx[-1]))
        out.append((len(t), float(np.mean([x["r_net"] for x in t])) if t else 0.0))
    return out


def judge(f, index, interval, name, runs, seed=11, run=None, rr=False):
    """
    run: optional function(i_start, i_end) -> trades (used by the short-target experiment);
    rr: the reward : risk of the baseline (False = the strategy's own, None = each trade has its own).
    """
    interval_min = INTERVAL_MINUTES[interval]
    if rr is False:
        rr = STRATEGIES[name][1]
    run_all = run or (lambda a, z: run_strategy(f, interval_min, name, i_start=a, i_end=z))
    trades = run_all(30, None)
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
    blocks = blocks_positive(f, interval_min, name, run=run)
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


# ---------------------------------------------------------------------------
# The short-target experiment: today's entries and stops, a smaller target
# ---------------------------------------------------------------------------
SHORT_TARGETS = {
    "r0.5": ("r", 0.5, "index target 0.5 x the stop distance"),
    "r1.0": ("r", 1.0, "index target 1.0 x the stop distance"),
    "rs10": ("rupees", 10.0, "option gains Rs 10 per share"),
    "rs20": ("rupees", 20.0, "option gains Rs 20 per share"),
    "pct30": ("percent", 30.0, "option gains 30% of its price"),
    "pct50": ("percent", 50.0, "option gains 50% of its price"),
}
RR_MIN, RR_MAX = 0.1, 6.0


def option_target_distance(index: str, side: str, spot: float, when, vix_pct: float, kind: str, amount: float):
    """
    Index points the market must move for the at-the-money option to gain `amount` (rupees per share, or
    percent of its price). Black-Scholes with the given India VIX (in %) and the time to the nearest weekly
    expiry; no time decay after the entry. None when it cannot be worked out.
    """
    if vix_pct is None or not np.isfinite(vix_pct) or vix_pct <= 0 or spot <= 0:
        return None
    step = cs.STRIKE_STEP[index]
    strike = round(spot / step) * step
    now = when.to_pydatetime() if hasattr(when, "to_pydatetime") else when
    years = op.years_to_expiry(op.next_expiry(index, now), now)
    sigma = vix_pct / 100.0
    price = op.bs_price(spot, strike, years, sigma, side)
    gain = amount if kind == "rupees" else price * amount / 100.0
    if price <= 0 or gain <= 0:
        return None
    want = price + gain
    lo, hi = (spot, spot * 1.5) if side == "CE" else (spot * 0.5, spot)
    reachable = op.bs_price(hi if side == "CE" else lo, strike, years, sigma, side) >= want
    if not reachable:
        return abs((hi if side == "CE" else lo) - spot)                  # not reachable within 50%: a huge distance
    for _ in range(60):
        mid = (lo + hi) / 2
        value = op.bs_price(mid, strike, years, sigma, side)
        if side == "CE":
            lo, hi = (mid, hi) if value < want else (lo, mid)
        else:
            lo, hi = (lo, mid) if value < want else (mid, hi)
    return abs((lo + hi) / 2 - spot)


def main_entries(f, index, interval_min):
    """The current candle-pattern rules (default filters): +1 / -1 on the decision candle and the stop distance."""
    sides, risks = np.zeros(len(f), dtype=int), np.zeros(len(f))
    for i in range(30, len(f)):
        plan = cs.evaluate(f, i, index, interval_min)
        if plan["signal"] != "NONE":
            sides[i] = 1 if plan["signal"] == "CE" else -1
            risks[i] = float(plan["risk_points"])
    return sides, risks


def target_rr(f, index, i, side, risk, variant):
    """Reward : risk of the trade decided at candle i for this short-target variant (None = no trade)."""
    kind, amount, _ = SHORT_TARGETS[variant]
    if kind == "r":
        return amount
    spot = float(f["close"].values[i])
    vix = f["vix_prev"].values[i]
    d = option_target_distance(index, "CE" if side == 1 else "PE", spot, f.index[i], vix, kind, amount)
    if d is None or risk <= 0:
        return None
    return float(min(max(d / risk, RR_MIN), RR_MAX))


def run_short_target(f, index, interval_min, variant, entries, cost_r=bt.COST_R, i_start=30, i_end=None):
    """The main backtest's trades (same simulator, one at a time), with this variant's target."""
    sides, risks = entries
    i_end = len(f) - 1 if i_end is None else min(i_end, len(f) - 1)
    atr, close = f["atr"].values, f["close"].values
    trades, i = [], max(i_start, 30)
    while i < i_end:
        if sides[i] == 0:
            i += 1
            continue
        rr = target_rr(f, index, i, int(sides[i]), float(risks[i]), variant)
        if rr is None:
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
            "stop": res["stop"], "status": res["status"], "r": res["r"], "rr": rr,
            "r_net": round(res["r"] - cost_r, 3), "bars": res["bars"],
            "context": {"atr": float(atr[i]), "start_min": int(f["start_min"].values[i]), "pattern": variant},
        })
        i = res["exit_idx"] + 1
    return trades


def judge_short(f, index, interval, variant, runs, entries, seed=11):
    interval_min = INTERVAL_MINUTES[interval]
    run = lambda a, z: run_short_target(f, index, interval_min, variant, entries, i_start=a, i_end=z)
    res = judge(f, index, interval, variant, runs, seed, run=run, rr=None)
    trades = run(30, None)
    if trades:
        res["avg_rr"] = float(np.mean([t["rr"] for t in trades]))
        res["clamped"] = sum(1 for t in trades if t["rr"] in (RR_MIN, RR_MAX)) / len(trades)
    return res


# ---------------------------------------------------------------------------
# The room-check experiment
# ---------------------------------------------------------------------------
def diff_range(kept, removed, runs=5000, seed=7):
    """Mean(kept) - mean(removed) and its 95% bootstrap range (each group resampled on its own). None if too few trades."""
    if len(kept) < 5 or len(removed) < 5:
        return None
    rng = np.random.default_rng(seed)
    kept, removed = np.asarray(kept), np.asarray(removed)
    a = rng.choice(kept, size=(runs, len(kept)), replace=True).mean(axis=1)
    b = rng.choice(removed, size=(runs, len(removed)), replace=True).mean(axis=1)
    d = a - b
    return float(kept.mean() - removed.mean()), float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def room_split(f, index, interval_min):
    """The current rules' trades, each with its room (R to the nearest key level ahead); room_ok = the check would keep it."""
    base = bt.run_backtest(f, index, interval_min, None, cs.REWARD_TO_RISK, bt.COST_R)
    out = []
    for t in base:
        row = f.loc[pd.Timestamp(t["time"])]
        found = cs.room_ahead(row, t["side"], float(t["entry"]), abs(t["fill"] - t["stop"]))
        room = None if found is None else found[0]
        out.append({"r_net": t["r_net"], "target": t["status"] == "TARGET", "room": room,
                    "room_ok": room is None or room >= cs.ROOM_MIN_R})
    return out


def judge_room(f, index, interval, runs):
    """The rules WITH the room check, through the same gate as the strategies."""
    im = INTERVAL_MINUTES[interval]
    run = lambda a, z: bt.run_backtest(f, index, im, {"room": True}, cs.REWARD_TO_RISK, bt.COST_R, i_start=a, i_end=z)
    return judge(f, index, interval, "room", runs, run=run, rr=cs.REWARD_TO_RISK)


def room_check_main(a):
    pooled_kept, pooled_removed, improved, markets = [], [], 0, 0
    for index in ([a.index] if a.index else ["nifty", "sensex"]):
        for interval in ([a.interval] if a.interval else ["5m", "15m"]):
            df = candle_store.load_candles(index, interval, a.db)
            if len(df) < 500:
                print(f"{index.upper()} {interval}: not enough saved candles")
                continue
            fut = candle_store.load_futures(index, interval, a.db)
            f = cs.compute_features(df, index, INTERVAL_MINUTES[interval], candle_store.load_vix(a.db),
                                    fut if len(fut) else None)
            if a.since:
                f = f[f.index.date >= pd.Timestamp(a.since).date()]            # features were worked out on all the history
                if len(pd.unique(f["day"])) < 12:
                    print(f"\n{index.upper()} {interval}: only {len(pd.unique(f['day']))} trading days since {a.since}: too few to judge")
                    continue
            trades = room_split(f, index, INTERVAL_MINUTES[interval])
            kept = [t["r_net"] for t in trades if t["room_ok"]]
            removed = [t["r_net"] for t in trades if not t["room_ok"]]
            pooled_kept += kept
            pooled_removed += removed
            res = judge_room(f, index, interval, a.runs)
            base_avg = float(np.mean([t["r_net"] for t in trades])) if trades else float("nan")
            markets += 1
            print(f"\n{'=' * 96}\n{index.upper()} {interval}")
            print(f"  current rules          : {len(trades):>3} trades, target hit {np.mean([t['target'] for t in trades]) * 100:>3.0f}%, avg {base_avg:+.3f} R")
            print(f"  check keeps / removes  : {len(kept):>3} trades avg {np.mean(kept) if kept else float('nan'):+.3f} R  /  "
                  f"{len(removed):>3} trades avg {np.mean(removed) if removed else float('nan'):+.3f} R")
            d = diff_range(kept, removed)
            print("  kept minus removed     : " + (f"{d[0]:+.3f} R (95%: {d[1]:+.3f}..{d[2]:+.3f})" if d else "too few trades"))
            if res["avg"] is None:
                print("  rules WITH the check   : no trades")
                continue
            lo, hi = res["ci"] if res["ci"] else (float("nan"), float("nan"))
            bl = " ".join(f"{x:+.2f}" for _, x in res["blocks"])
            print(f"  rules WITH the check   : {res['trades']:>3} trades, avg {res['avg']:+.3f} R (95%: {lo:+.2f}..{hi:+.2f}), "
                  f"blocks [{bl}], p vs random {res['p']:.3f}")
            print(f"                           GATE: {'PASS' if res['passed'] else 'fail'}{'' if res['passed'] else ' - ' + res['why']}")
            improved += int(res["avg"] > base_avg)
    if not markets:
        return
    d = diff_range(pooled_kept, pooled_removed)
    print(f"\n{'=' * 96}\nPOOLED over {markets} markets (the markets share the same days, so these are not independent trades):")
    print(f"  kept {len(pooled_kept)} trades avg {np.mean(pooled_kept):+.3f} R | removed {len(pooled_removed)} trades avg {np.mean(pooled_removed):+.3f} R")
    if d:
        print(f"  kept minus removed: {d[0]:+.3f} R (95%: {d[1]:+.3f}..{d[2]:+.3f})")
    print(f"  the rules with the check beat the rules without it in {improved} of {markets} markets")
    candidate = bool(d and d[1] > 0 and improved >= min(3, markets))
    print("  VERDICT: " + ("a CANDIDATE (still has to pass the gate before it is used)" if candidate
                            else "NOT a candidate: keep the check OFF"))


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
    ap.add_argument("--room-check", action="store_true",
                    help="test the optional 'room to the next key level' check on the current rules")
    ap.add_argument("--since", help="with --room-check: only trades from this date on, YYYY-MM-DD "
                                    "(the check was first judged on data up to 2026-11-25, so use --since 2026-11-26 for a fair test)")
    ap.add_argument("--short-targets", action="store_true",
                    help="run the short-target experiment (today's entries, smaller targets) instead of the four strategies")
    ap.add_argument("--variant", choices=list(SHORT_TARGETS), help="with --short-targets: only this variant")
    ap.add_argument("--runs", type=int, default=300, help="random runs per test (default 300)")
    ap.add_argument("--db", default=candle_store.DB_FILE)
    a = ap.parse_args()

    if a.room_check:
        room_check_main(a)
        return
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
            names = (([a.variant] if a.variant else list(SHORT_TARGETS)) if a.short_targets
                     else ([a.strategy] if a.strategy else list(STRATEGIES)))
            entries = main_entries(f, index, INTERVAL_MINUTES[interval]) if a.short_targets else None
            for name in names:
                r = (judge_short(f, index, interval, name, a.runs, entries) if a.short_targets
                     else judge(f, index, interval, name, a.runs))
                results.append(r)
                if r["avg"] is None:
                    print(f"  {name:<11} no trades")
                    continue
                lo, hi = r["ci"] if r["ci"] else (float("nan"), float("nan"))
                bl = " ".join(f"{x:+.2f}" for _, x in r["blocks"])
                extra = f" | target {r['avg_rr']:.2f}R" if "avg_rr" in r else ""
                print(f"  {name:<11} trades {r['trades']:>3} | win {r['win'] * 100:>3.0f}%{extra} | avg {r['avg']:+.2f}R "
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
