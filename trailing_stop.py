"""
trailing_stop.py
----------------
Trailing stop-loss ADVICE for a running idea. It is advice only: the project never places or changes an order, and the
paper results in the journal keep using the first stop (so they stay comparable with every test we have run).

The rule (your idea, written down before use and not tuned), in option prices per unit:
    - Keep the first stop until the option has gained ACTIVATE_RS (Rs 20) above your buy price.
    - From then on the stop follows the HIGHEST option price seen, TRAIL_RS (Rs 15) below it. It never moves down.
    - An alert is sent when the new stop is at least ALERT_STEP_RS (Rs 3) above the last stop that was announced.

Option prices here are estimates: a straight line between the saved buy price and target price of the idea (see
stop_ladder.option_price_at). Time decay and the bid-ask gap are not included.

What a one-year test on NIFTY and SENSEX said (see the README): this trailing stop gives more small wins and protects
gains, but it did NOT raise the average result, and no version beat random entries. It protects profit; it does not
create it.
"""

from stop_ladder import option_price_at

ACTIVATE_RS = 20.0
TRAIL_RS = 15.0
ALERT_STEP_RS = 3.0


def _tick(price: float) -> float:
    """Options trade in steps of 5 paise."""
    return round(round(price / 0.05) * 0.05, 2)


def advice(idea: dict) -> dict:
    """
    idea: side, fill, risk (index points), peak_r (best gain so far in R), entry (the idea's index entry), target (index),
    option_levels (saved buy / stop / target option prices, may be None), latest (the latest index close, optional).
    Returns the stop to use now, in option prices. has_prices is False when the idea has no saved option prices.
    """
    ol = idea.get("option_levels") or {}
    out = {"has_prices": False, "active": False, "peak_r": round(float(idea.get("peak_r") or 0.0), 2),
           "buy_option": None, "first_stop_option": None, "high_option": None, "stop_option": None,
           "now_option": None, "start_option": None, "locks": None}
    if not all(k in ol for k in ("entry_price", "stop_price", "target_price")):
        return out
    buy, first = float(ol["entry_price"]), float(ol["stop_price"])
    sign = 1 if idea["side"] == "CE" else -1

    def estimate(level):
        return option_price_at(level, idea["entry"], idea["target"], buy, ol["target_price"])

    high = estimate(float(idea["fill"]) + sign * float(idea.get("peak_r") or 0.0) * float(idea["risk"]))
    if high is None:
        return out
    high = max(high, buy)
    active = high - buy >= ACTIVATE_RS - 1e-9
    stop = max(first, min(_tick(high - TRAIL_RS), high)) if active else first
    now = estimate(float(idea["latest"])) if idea.get("latest") is not None else None
    out.update(has_prices=True, active=active, buy_option=round(buy, 2), first_stop_option=round(first, 2),
               high_option=round(high, 2), stop_option=round(stop, 2), now_option=now,
               start_option=round(buy + ACTIVATE_RS, 2), locks=round(stop - buy, 2))
    return out


def should_alert(adv: dict, old_stop) -> bool:
    """True when the trailing stop is on and has risen at least ALERT_STEP_RS above the last announced stop."""
    if not adv.get("has_prices") or not adv.get("active") or old_stop is None:
        return False
    return adv["stop_option"] - float(old_stop) >= ALERT_STEP_RS - 1e-9
