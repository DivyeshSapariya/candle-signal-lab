"""
test_stop_ladder.py
-------------------
Checks the older stop-ladder helpers (stop_ladder.py): the steps and levels, and the option-price estimate that the
trailing stop also uses. The dashboard and the Telegram alert now use the trailing stop (see test_trailing_stop.py).

Run:   python test_stop_ladder.py
"""

import stop_ladder as sl
import telegram_notify as tg


def main():
    # --- steps and levels ---
    assert [sl.ladder_step(x) for x in (None, 0.0, 0.74, 0.75, 1.24, 1.25, 1.74, 1.75, 2.5)] == [0, 0, 0, 1, 1, 2, 2, 3, 3]
    assert [sl.lock_r(n) for n in (0, 1, 2, 3)] == [None, 0.0, 0.5, 1.0]
    assert [sl.stop_level("CE", 100.0, 10.0, n) for n in range(4)] == [90.0, 100.0, 105.0, 110.0]
    assert [sl.stop_level("PE", 100.0, 10.0, n) for n in range(4)] == [110.0, 100.0, 95.0, 90.0]
    assert [sl.reach_level("CE", 100.0, 10.0, n) for n in (1, 2, 3)] == [107.5, 112.5, 117.5]
    assert [sl.reach_level("PE", 100.0, 10.0, n) for n in (1, 2, 3)] == [92.5, 87.5, 82.5]

    # --- option-price estimate: a straight line between the saved buy and target prices ---
    assert sl.option_price_at(110.0, 100.0, 120.0, 50.0, 80.0) == 65.0
    assert sl.option_price_at(100.0, 100.0, 120.0, 50.0, 80.0) == 50.0
    assert sl.option_price_at(0.0, 100.0, 120.0, 50.0, 80.0) == 0.05, "never below 5 paise"
    assert sl.option_price_at(110.0, 100.0, 100.0, 50.0, 80.0) is None and sl.option_price_at(110.0, None, 120.0, 50.0, 80.0) is None

    idea = {"side": "CE", "fill": 100.0, "risk": 10.0, "peak_r": 0.5, "entry": 100.0, "target": 120.0,
            "option_levels": {"entry_price": 50.0, "stop_price": 35.0, "target_price": 80.0}}
    a0 = sl.advice(idea)
    assert a0["step"] == 0 and a0["stop_index"] == 90.0 and a0["stop_option"] == 35.0 and a0["next"]["at_index"] == 107.5
    assert a0["next"]["stop_index"] == 100.0 and a0["next"]["stop_option"] == 50.0
    a2 = sl.advice(dict(idea, peak_r=1.4))
    assert a2["step"] == 2 and a2["stop_index"] == 105.0 and a2["stop_option"] == 57.5 and a2["lock_r"] == 0.5
    a3 = sl.advice(dict(idea, peak_r=1.9))
    assert a3["step"] == 3 and a3["next"] is None and a3["stop_option"] == 65.0
    assert sl.advice(dict(idea, option_levels=None, peak_r=1.4))["stop_option"] is None, "no option prices: index levels only"
    put = sl.advice(dict(idea, side="PE", entry=100.0, target=80.0, peak_r=1.3, option_levels=None))
    assert put["stop_index"] == 95.0 and put["next"]["at_index"] == 82.5

    # --- the older ladder message (no longer sent by the dashboard) still formats ---
    msg = tg.format_ladder({"option": "NIFTY 25450 CE", "index": "nifty", "interval": "5m", "advice": a2, "before": a0})
    assert msg.splitlines()[0].endswith("MOVE STOP LOSS - NIFTY 25450 CE")
    assert "New stop loss - ₹57.50" in msg and "Old stop loss - ₹35.00" in msg and "+0.5 R" in msg

    print("OK: stop ladder helpers: steps and levels, option-price estimate (used by the trailing stop)")


if __name__ == "__main__":
    main()
