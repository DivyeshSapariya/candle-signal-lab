"""
stop_ladder.py
--------------
Stop-loss LADDER advice for a running idea: as the price moves in your favour, it tells you where to move your stop.
It is advice only. The project never places or changes an order, and the paper results in the journal keep
using the first stop (so they stay comparable with every test we have run).

The ladder is fixed (written down before use, not tuned). R = the distance from your fill to the FIRST stop:
    best gain reaches +0.75 R  -> move the stop to your buy level (breakeven)
    best gain reaches +1.25 R  -> move the stop to lock +0.5 R
    best gain reaches +1.75 R  -> move the stop to lock +1.0 R     (the target is +2.0 R)

What a one-year test on the current rules said (index R, 0.1 R cost, 546 trades, see the README): a ladder raises the
win rate (for example 36% -> 33 to 47% depending on the version) but NOT the average result. It protects gains that
would otherwise be given back, and it also stops out trades that would have gone on to the target.
"""

LADDER = ((0.75, 0.0), (1.25, 0.5), (1.75, 1.0))          # (best gain reached in R, the gain the new stop locks in R)


def ladder_step(peak_r) -> int:
    """0 = keep the first stop; 1, 2, 3 = how many ladder steps the best gain has reached."""
    if peak_r is None:
        return 0
    return sum(1 for reach, _ in LADDER if float(peak_r) >= reach)


def lock_r(step: int):
    """The gain (in R) the stop locks at this step, or None for step 0 (the first stop)."""
    return LADDER[step - 1][1] if step >= 1 else None


def stop_level(side: str, fill: float, risk: float, step: int):
    """The index level of the stop at this step. Step 0: the first stop."""
    sign = 1 if side == "CE" else -1
    return fill - sign * risk if step == 0 else fill + sign * LADDER[step - 1][1] * risk


def reach_level(side: str, fill: float, risk: float, step: int):
    """The index level where this step is reached (step 1..3)."""
    sign = 1 if side == "CE" else -1
    return fill + sign * LADDER[step - 1][0] * risk


def option_price_at(index_level: float, plan_entry: float, plan_target: float, entry_price: float, target_price: float):
    """
    A rough option price at an index level, by a straight line between the idea's saved buy and target prices
    (the stop levels of the ladder lie between them). It ignores time decay and the curve of the price, so it is an
    estimate. Returns None when the saved numbers cannot give one.
    """
    try:
        span = float(plan_target) - float(plan_entry)
        if span == 0:
            return None
        price = float(entry_price) + (float(index_level) - float(plan_entry)) / span * (float(target_price) - float(entry_price))
    except (TypeError, ValueError):
        return None
    return round(max(price, 0.05), 2)


def advice(idea: dict) -> dict:
    """
    idea: side, fill, risk (index points), peak_r (best gain so far in R), entry (the idea's index entry), target
    (index), option_levels (saved buy / stop / target option prices, may be None).
    Returns the stop to use now and the next step, in index levels and (when possible) option prices.
    """
    side, fill, risk = idea["side"], float(idea["fill"]), float(idea["risk"])
    step = ladder_step(idea.get("peak_r"))
    ol = idea.get("option_levels") or {}

    def option(level):
        if not ol or "entry_price" not in ol or "target_price" not in ol:
            return None
        return option_price_at(level, idea["entry"], idea["target"], ol["entry_price"], ol["target_price"])

    now_level = stop_level(side, fill, risk, step)
    out = {
        "step": step, "peak_r": None if idea.get("peak_r") is None else round(float(idea["peak_r"]), 2),
        "stop_index": round(now_level, 2),
        "stop_option": (ol.get("stop_price") if step == 0 and ol else option(now_level)),
        "first_stop_index": round(stop_level(side, fill, risk, 0), 2), "first_stop_option": ol.get("stop_price") if ol else None,
        "lock_r": lock_r(step), "buy_option": ol.get("entry_price") if ol else None, "next": None,
    }
    if step < len(LADDER):
        nxt = step + 1
        new_level = stop_level(side, fill, risk, nxt)
        out["next"] = {"at_r": LADDER[nxt - 1][0], "at_index": round(reach_level(side, fill, risk, nxt), 2),
                       "stop_index": round(new_level, 2), "stop_option": option(new_level), "lock_r": LADDER[nxt - 1][1]}
    return out
