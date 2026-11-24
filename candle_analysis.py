"""
candle_analysis.py
------------------
Explains HOW the tool reaches its decision. It adds nothing to the rules.

Everything here is built from candle_signals.evaluate(), the same function that produces the
trade ideas, so what the "Live analysis" panel shows can never disagree with the real decision.

  live_analysis()    the 4 steps for the newest candle: pattern, confirmation, filters, points
  rejected_setups()  recent candles where a pattern appeared but NO idea was made, and why
"""

from datetime import timedelta

import pandas as pd

from candle_signals import evaluate, MIN_SCORE

LOOKBACK_CANDLES = 150      # about 2 trading days of 5-minute candles
MAX_REJECTED = 8


def _step(key: str, title: str, state: str, detail: str) -> dict:
    """state is 'ok', 'fail' or 'wait' (not reached yet)."""
    return {"key": key, "title": title, "state": state, "detail": detail}


def market_facts(row) -> list:
    """Extra facts about the newest candle (information only; the checks for them are off by default)."""
    facts = []
    if pd.notna(row.get("vwap")):
        label = "Day average price" if row.get("vwap_proxy") else "VWAP"
        side = "above" if row["close"] > row["vwap"] else "below"
        facts.append(f"{label} {row['vwap']:,.0f} (price is {side})")
    if pd.notna(row.get("gap_pct")):
        facts.append(f"Opening gap {row['gap_pct']:+.2f}%")
    facts.append(f"Volume {row['vol_ratio']:.1f}x average" if pd.notna(row.get("vol_ratio"))
                 else "Volume not available for this data")
    return facts


def live_analysis(plan: dict, last_candle_start, interval_min: int, market_open: bool,
                  last_row=None) -> dict:
    """
    plan = the decision for the newest finished candle (from build_trade_plan).
    Returns the four steps and one plain sentence saying where the decision stands.
    """
    reasons = plan.get("reasons") or []
    by_key = {r.get("key"): r for r in reasons}
    required = [r for r in reasons if r["required"] and r.get("key") not in ("pattern", "confirm")]

    steps = []
    pattern = by_key.get("pattern")
    if pattern:
        steps.append(_step("pattern", "Pattern", "ok", pattern["text"]))
    else:
        steps.append(_step("pattern", "Pattern", "fail", "No clear CE / PE pattern on the signal candle."))

    confirm = by_key.get("confirm")
    if not pattern:
        steps.append(_step("confirm", "Confirmation", "wait", "Needs a pattern first."))
    elif confirm:
        steps.append(_step("confirm", "Confirmation", "ok" if confirm["ok"] else "fail", confirm["text"]))
    else:
        steps.append(_step("confirm", "Confirmation", "ok", "Not required (confirmation filter is off)."))

    if not pattern:
        steps.append(_step("filters", "Filters", "wait", "Needs a pattern first."))
    else:
        failed = [r for r in required if not r["ok"]]
        passed = len(required) - len(failed)
        if failed:
            steps.append(_step("filters", "Filters", "fail",
                               f"{passed} of {len(required)} passed. Failed: {failed[0]['text']}"))
        else:
            steps.append(_step("filters", "Filters", "ok", f"All {len(required)} must-pass checks passed."))

    if not pattern:
        steps.append(_step("points", "Agreement", "wait", "Needs a pattern first."))
    else:
        score = plan.get("score", 0)
        steps.append(_step("points", "Agreement", "ok" if score >= MIN_SCORE else "fail",
                           f"{score} of {plan.get('max_score', 4)} points (an idea needs {MIN_SCORE})."))

    if plan.get("signal") != "NONE":
        headline = f"Idea ready: {plan['option']}. All steps passed."
    else:
        headline = plan.get("message") or "Waiting for a setup."

    next_close = None
    if market_open and last_candle_start is not None:
        # last_candle_start is the START of the newest FINISHED candle, so the running candle
        # started one candle later and closes one candle after that.
        next_close = (last_candle_start + timedelta(minutes=2 * interval_min)).strftime("%H:%M")

    return {"steps": steps, "headline": headline, "next_candle_closes": next_close,
            "min_score": MIN_SCORE,
            "facts": market_facts(last_row) if last_row is not None else []}


def rejected_setups(features, index: str, interval_min: int, filters: dict = None,
                    lookback: int = LOOKBACK_CANDLES, limit: int = MAX_REJECTED) -> list:
    """
    Walk the most recent candles with the SAME evaluate() and list the ones where a pattern was
    found but no idea came out, newest first, with the first reason it was dropped.
    """
    out = []
    n = len(features)
    for i in range(n - 1, max(n - 1 - lookback, 29), -1):
        res = evaluate(features, i, index, interval_min, filters)
        if res["signal"] != "NONE" or not res.get("reasons"):
            continue
        reasons = res["reasons"]
        failed = [r for r in reasons if r["required"] and not r["ok"]]
        if failed:
            why = failed[0]["text"]
        else:
            why = f"Only {res['score']} of {res['max_score']} points (needs {MIN_SCORE})."
        out.append({
            "time": features.index[i].strftime("%d %b %H:%M"),
            "setup": reasons[0]["text"],
            "why": why,
            "score": res["score"],
        })
        if len(out) >= limit:
            break
    return out
