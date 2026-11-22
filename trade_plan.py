"""
trade_plan.py
-------------
Live layer used by the dashboard.

The actual rules (patterns, filters, entry, stop loss, target) live in candle_signals.py,
so the live tool and the backtest always use the SAME rules.

This file only adds one thing: where the price is NOW compared with the plan.
"""

import candle_memory
from candle_signals import compute_features, evaluate  # noqa: F401  (re-exported for convenience)


def live_status(plan: dict, current_price: float) -> str:
    sign = 1 if plan["signal"] == "CE" else -1
    risk = plan["risk_points"]
    moved = (current_price - plan["entry"]) * sign    # positive = price is beyond entry

    if (current_price - plan["stop"]) * sign <= 0:
        return "Stop loss level already touched. This idea is no longer valid."
    if (current_price - plan["target"]) * sign >= 0:
        return "Target level already reached. This idea is finished."
    if moved > 0.5 * risk:
        return "Price already moved far beyond entry. Chasing is risky. Wait for the next setup."

    if plan.get("mode") == "confirm":
        return ("Signal is confirmed. The entry zone is NOW (near the entry level). "
                "Buy only if the real option price in Groww is close to the estimate.")
    if moved < 0:
        return f"Waiting. Price needs to move {abs(moved):,.1f} points to reach entry."
    return "Entry level just crossed. Idea is active."


def build_trade_plan(features, index: str, interval_min: int, current_price: float,
                     filters: dict = None) -> dict:
    """features = output of compute_features() for FINISHED candles."""
    plan = evaluate(features, len(features) - 1, index, interval_min, filters)
    if plan["signal"] != "NONE":
        plan["status"] = live_status(plan, current_price)
        try:
            plan["memory"] = candle_memory.recall(plan, index, interval_min)
        except Exception as error:                      # the memory must never break the dashboard
            print(f"[memory] problem: {error}")
            plan["memory"] = None
        if candle_memory.should_block(plan["memory"]):
            return {"signal": "NONE", "score": plan["score"], "max_score": plan["max_score"],
                    "reasons": plan["reasons"],
                    "message": "Blocked by memory: " + plan["memory"]["text"]}
    return plan
