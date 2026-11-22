"""
candle_backtest.py
-------------------
Tests the trade rules on PAST candles to see how often they really work.

Run:
    python candle_backtest.py                         # NIFTY, 5 min candles, last 60 days (Yahoo, free)
    python candle_backtest.py --index sensex
    python candle_backtest.py --interval 15m
    python candle_backtest.py --csv my_candles.csv --index nifty --interval 5m
    python candle_backtest.py --save                  # also saves every trade to backtest_trades.csv
    python candle_backtest.py --learn                 # also gives the results to the tool's memory

What it prints
  1. Result with all filters ON
  2. A comparison: each filter removed one by one (shows which filters really help)
  3. First part of the data vs last part (a good rule works in BOTH parts)

How a trade is simulated (honest and slightly pessimistic):
  - Entry after the confirmation candle closes: filled at the NEXT candle's open.
  - Stop loss = -1R, target = +2R  (R = the risk distance).
  - If one candle touches BOTH stop and target, it counts as a LOSS.
  - If price gaps past the stop, the loss is bigger than 1R (like real life).
  - Trade is closed at the end of the day, or after 2 hours (time decay hurts option buyers).
  - A cost of 0.10R per trade is subtracted (brokerage, taxes, slippage - a rough guess).
  - Only one trade at a time.

Results are on the INDEX, in R (R = the amount you risk per trade). They are not rupees.
"""

import argparse

import numpy as np
import pandas as pd

from candle_signals import (DEFAULT_FILTERS, FILTER_LABELS, MARKET_CLOSE_MIN,
                           REWARD_TO_RISK, compute_features, evaluate)

MAX_HOLD_MIN = 120
COST_R = 0.10


# ---------------------------------------------------------------------------
# Simulate one trade
# ---------------------------------------------------------------------------
def simulate_trade(f: pd.DataFrame, i: int, plan: dict, interval_min: int,
                   rr: float = REWARD_TO_RISK, max_hold_min: int = MAX_HOLD_MIN) -> dict:
    """
    plan comes from evaluate() at decision candle i.
    status: STOP / TARGET / TIME (finished), OPEN (running), PENDING (waiting for candles),
            SKIPPED (never entered).
    r is the gross result in R (before costs).
    """
    sign = 1 if plan["signal"] == "CE" else -1
    risk = float(plan["risk_points"])
    entry = float(plan["entry"])
    n = len(f)
    o, h, l, c = (f[k].values for k in ("open", "high", "low", "close"))
    days = f["day"].values
    start_min = f["start_min"].values

    def done(status, fill=None, k=None, price=None, stop=None, target=None, note="",
             bars=0, peak=0.0, dd=0.0):
        # peak / dd = best and worst move (in R) of the candles BEFORE the exit candle
        out = {"status": status, "fill": fill, "stop": stop, "target": target,
               "exit_idx": k, "exit_price": price, "exit_time": None, "r": None, "note": note,
               "bars": bars, "peak_r": round(peak / risk, 2), "dd_r": round(dd / risk, 2)}
        if k is not None:
            out["exit_time"] = f.index[k].isoformat()
        if price is not None and fill is not None:
            out["r"] = round((price - fill) * sign / risk, 3)
        return out

    j = i + 1
    if j >= n:
        return done("PENDING", note="waiting for the next candle")
    if days[j] != days[i]:
        return done("SKIPPED", note="no more candles today")

    if plan.get("mode") == "confirm":
        fill, k0 = float(o[j]), j
    else:
        fill, k0 = None, None
        for k in (j, j + 1):
            if k >= n:
                return done("PENDING", note="waiting for entry level")
            if days[k] != days[i]:
                break
            if (h[k] >= entry) if sign == 1 else (l[k] <= entry):
                fill = max(entry, float(o[k])) if sign == 1 else min(entry, float(o[k]))
                k0 = k
                break
        if fill is None:
            return done("SKIPPED", note="entry level not reached in 2 candles")

    if (fill - entry) * sign > 0.5 * risk:
        return done("SKIPPED", note="price already ran too far (chasing)")

    stop = fill - sign * risk
    target = fill + sign * rr * risk
    max_hold = max(1, max_hold_min // interval_min)

    peak = dd = 0.0
    bars = 0
    for k in range(k0, n):
        if days[k] != days[k0]:
            break
        bars = k - k0 + 1
        if k > k0 and ((o[k] <= stop) if sign == 1 else (o[k] >= stop)):     # gap through stop
            return done("STOP", fill, k, float(o[k]), stop, target, bars=bars, peak=peak, dd=dd)
        stop_hit = (l[k] <= stop) if sign == 1 else (h[k] >= stop)
        target_hit = (h[k] >= target) if sign == 1 else (l[k] <= target)
        if stop_hit:                                  # also when both are hit: assume the worst
            return done("STOP", fill, k, stop, stop, target, bars=bars, peak=peak, dd=dd)
        if target_hit:
            return done("TARGET", fill, k, target, stop, target, bars=bars, peak=peak, dd=dd)
        favour = (h[k] - fill) if sign == 1 else (fill - l[k])
        against = (fill - l[k]) if sign == 1 else (h[k] - fill)
        peak, dd = max(peak, favour), max(dd, against)
        last_of_day = start_min[k] + interval_min >= MARKET_CLOSE_MIN
        if last_of_day or bars >= max_hold:
            return done("TIME", fill, k, float(c[k]), stop, target, bars=bars, peak=peak, dd=dd)

    return done("OPEN", fill, None, None, stop, target, note="trade is still running",
                bars=bars, peak=peak, dd=dd)


# ---------------------------------------------------------------------------
# Run over many candles
# ---------------------------------------------------------------------------
def run_backtest(f: pd.DataFrame, index: str, interval_min: int, filters: dict = None,
                 rr: float = REWARD_TO_RISK, cost_r: float = COST_R,
                 i_start: int = 30, i_end: int = None) -> list:
    trades = []
    i_end = len(f) - 1 if i_end is None else min(i_end, len(f) - 1)
    i = max(i_start, 30)
    while i < i_end:
        plan = evaluate(f, i, index, interval_min, filters)
        if plan["signal"] == "NONE":
            i += 1
            continue
        res = simulate_trade(f, i, plan, interval_min, rr)
        if res["status"] in ("SKIPPED",):
            i += 1
            continue
        if res["status"] in ("OPEN", "PENDING"):
            break
        trades.append({
            "time": f.index[i].isoformat(), "side": plan["signal"], "score": plan["score"],
            "entry": plan["entry"], "fill": res["fill"], "stop": res["stop"],
            "target": res["target"], "exit_time": res["exit_time"],
            "exit_price": res["exit_price"], "status": res["status"],
            "r": res["r"], "r_net": round(res["r"] - cost_r, 3),
            "bars": res["bars"], "peak_r": res["peak_r"], "dd_r": res["dd_r"],
            "context": plan["context"],
        })
        i = res["exit_idx"] + 1
    return trades


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def stats(trades: list, rr: float = REWARD_TO_RISK, cost_r: float = COST_R) -> dict:
    n = len(trades)
    breakeven = (1 + cost_r) / (rr + 1)
    if n == 0:
        return {"trades": 0, "breakeven": breakeven}
    net = np.array([t["r_net"] for t in trades])
    wins = sum(1 for t in trades if t["status"] == "TARGET")
    stops = sum(1 for t in trades if t["status"] == "STOP")
    times = sum(1 for t in trades if t["status"] == "TIME")
    gains, losses = net[net > 0].sum(), -net[net < 0].sum()
    curve = np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    streak = best = 0
    for value in net:
        streak = streak + 1 if value < 0 else 0
        best = max(best, streak)
    return {
        "trades": n, "targets": wins, "stops": stops, "time_exits": times,
        "win_rate": wins / n, "breakeven": breakeven,
        "avg_r": float(net.mean()), "total_r": float(net.sum()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "max_drawdown_r": float((peak - curve).max()),
        "max_losing_streak": best,
    }


def one_line(label: str, s: dict) -> str:
    if s["trades"] == 0:
        return f"{label:<44} no trades"
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (f"{label:<44} trades {s['trades']:>3} | wins {s['win_rate'] * 100:>4.0f}% | "
            f"avg {s['avg_r']:+.2f}R | total {s['total_r']:+6.1f}R | PF {pf:>5} | "
            f"max drawdown {s['max_drawdown_r']:.1f}R")


def print_detail(title: str, s: dict, rr: float):
    print(f"\n=== {title} ===")
    if s["trades"] == 0:
        print("No trades were taken.")
        return
    print(f"Trades              : {s['trades']}  (target {s['targets']}, stop loss {s['stops']}, time exit {s['time_exits']})")
    print(f"Win rate            : {s['win_rate'] * 100:.1f}%   (break-even needs about {s['breakeven'] * 100:.0f}% with 1:{rr:g} and costs)")
    print(f"Average result      : {s['avg_r']:+.3f} R per trade (after costs)")
    print(f"Total result        : {s['total_r']:+.1f} R")
    pf = "no losing trades" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    print(f"Profit factor       : {pf}   (above 1.0 = makes money)")
    print(f"Worst drawdown      : {s['max_drawdown_r']:.1f} R")
    print(f"Longest losing run  : {s['max_losing_streak']} trades in a row")
    if s["trades"] < 30:
        print("WARNING: fewer than 30 trades. This is too small to trust. Use more data.")


# ---------------------------------------------------------------------------
# Compare filters, and first-part vs second-part
# ---------------------------------------------------------------------------
def compare_filters(f, index, interval_min, rr=REWARD_TO_RISK, cost_r=COST_R) -> list:
    rows = []
    # "levels" stays ON: it is a scoring point, not a filter. Turning it off gives every
    # pattern a free point, which made this row mean something other than its label.
    none = {k: (k == "levels") for k in DEFAULT_FILTERS}
    rows.append(("NO filters (patterns + points only)",
                 stats(run_backtest(f, index, interval_min, none, rr, cost_r), rr, cost_r)))
    rows.append(("ALL default filters",
                 stats(run_backtest(f, index, interval_min, None, rr, cost_r), rr, cost_r)))
    for name in DEFAULT_FILTERS:
        if not DEFAULT_FILTERS[name]:
            continue
        flt = {name: False}
        rows.append((f"All filters EXCEPT {FILTER_LABELS[name]}"[:44],
                     stats(run_backtest(f, index, interval_min, flt, rr, cost_r), rr, cost_r)))
    return rows


def split_periods(f, index, interval_min, rr=REWARD_TO_RISK, cost_r=COST_R):
    days = pd.unique(f["day"])
    if len(days) < 6:
        return None
    cut_day = days[int(len(days) * 0.6)]
    cut = int(np.argmax(f["day"].values == cut_day))
    first = stats(run_backtest(f, index, interval_min, None, rr, cost_r, i_start=30, i_end=cut), rr, cost_r)
    second = stats(run_backtest(f, index, interval_min, None, rr, cost_r, i_start=cut, i_end=len(f) - 1), rr, cost_r)
    return first, second


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main():
    from candle_patterns import load_csv
    from data_sources import INTERVAL_MINUTES, IST, drop_forming_candle, fetch_yahoo
    from option_pricer import get_vix_daily

    parser = argparse.ArgumentParser()
    parser.add_argument("--index", choices=["nifty", "sensex"], default="nifty")
    parser.add_argument("--interval", choices=list(INTERVAL_MINUTES), default="5m")
    parser.add_argument("--days", type=int, default=58, help="days of history (Yahoo gives at most 60 for 5m / 15m)")
    parser.add_argument("--csv", help="use your own candle file instead of Yahoo")
    parser.add_argument("--rr", type=float, default=REWARD_TO_RISK, help="reward : risk (default 2)")
    parser.add_argument("--cost", type=float, default=COST_R, help="cost per trade in R (default 0.10)")
    parser.add_argument("--no-vix", action="store_true", help="do not use India VIX")
    parser.add_argument("--save", action="store_true", help="save all trades to backtest_trades.csv")
    parser.add_argument("--learn", action="store_true",
                        help="give these results to the tool's memory (trade_memory.csv)")
    args = parser.parse_args()

    interval_min = INTERVAL_MINUTES[args.interval]
    if args.csv:
        df = load_csv(args.csv)
        idx = pd.DatetimeIndex(df.index)
        df.index = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)   # CSV times are IST
    else:
        print(f"Downloading {args.days} days of {args.index.upper()} {args.interval} candles from Yahoo...")
        df = fetch_yahoo(args.index, args.interval, period=f"{args.days}d")
    df = drop_forming_candle(df, args.interval)

    vix = None if args.no_vix else get_vix_daily(period="9mo")
    if not args.no_vix and vix is None:
        print("(India VIX could not be downloaded, so the VIX filter is skipped.)")

    f = compute_features(df, args.index, interval_min, vix)
    days = len(pd.unique(f["day"]))
    print(f"Loaded {len(f)} candles over {days} trading days "
          f"({f.index[0]:%d %b %Y} to {f.index[-1]:%d %b %Y}).")

    trades = run_backtest(f, args.index, interval_min, None, args.rr, args.cost)
    print_detail("RESULT WITH ALL FILTERS", stats(trades, args.rr, args.cost), args.rr)

    print("\n=== WHICH FILTERS HELP? (each row removes one filter) ===")
    for label, s in compare_filters(f, args.index, interval_min, args.rr, args.cost):
        print(one_line(label, s))

    periods = split_periods(f, args.index, interval_min, args.rr, args.cost)
    if periods:
        print("\n=== DOES IT WORK IN BOTH PARTS OF THE DATA? ===")
        print(one_line("First 60% of the days", periods[0]))
        print(one_line("Last 40% of the days", periods[1]))
        print("A rule that works in only one part is probably luck.")

    if args.save and trades:
        pd.DataFrame(trades).drop(columns=["context"]).to_csv("backtest_trades.csv", index=False)
        print("\nSaved: backtest_trades.csv")

    if args.learn:
        import candle_memory
        stored = candle_memory.replace_backtest(args.index, args.interval, trades)
        print(f"\nMemory updated: {stored} backtest trades saved in trade_memory.csv "
              f"(older backtest memory of {args.index.upper()} {args.interval} was replaced).")
        lessons = candle_memory.lessons()
        for title, key in (("Worked best", "strengths"), ("Worked worst", "weaknesses")):
            if lessons[key]:
                print(f"\n{title} (needs {lessons['min_samples']}+ examples):")
                for line in lessons[key]:
                    print("  -", line)

    print("\nReminder: past results do not promise future results. Check the same rules with paper trading before real money.")


if __name__ == "__main__":
    main()
