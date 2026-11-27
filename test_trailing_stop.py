"""
test_trailing_stop.py
---------------------
Checks the trailing stop advice: it starts when the option is Rs 20 above the buy price, follows the highest price
Rs 15 below it and never moves down; the one-line Telegram message; one message per rise of at least Rs 3 (only for
ideas that had a new-idea alert); the open-ideas list; and that the paper result keeps using the FIRST stop.

Run:   python test_trailing_stop.py
"""

import os
import tempfile

import candle_journal as cj
import candle_signals as cs
import telegram_notify as tg
import trailing_stop as ts
from test_no_lookahead import make_candles


def main():
    # --- the rule (option prices run 2 rupees per index point here: buy 100 at index 100, target 140 at index 120) ---
    levels = {"entry_price": 100.0, "stop_price": 80.0, "target_price": 140.0}
    idea = {"side": "CE", "fill": 100.0, "risk": 10.0, "peak_r": 0.0, "entry": 100.0, "target": 120.0, "option_levels": levels}
    a = ts.advice(idea)
    assert a["has_prices"] and not a["active"] and a["stop_option"] == 80.0 and a["start_option"] == 120.0
    a = ts.advice(dict(idea, peak_r=0.95))                                   # option about 119: not 20 above the buy yet
    assert not a["active"] and a["stop_option"] == 80.0
    a = ts.advice(dict(idea, peak_r=1.0))                                    # option 120 = buy + 20: trailing starts
    assert a["active"] and a["high_option"] == 120.0 and a["stop_option"] == 105.0 and a["locks"] == 5.0
    a = ts.advice(dict(idea, peak_r=1.6, latest=110.0))                      # option 132 at best; now 120
    assert a["stop_option"] == 117.0 and a["now_option"] == 120.0 and a["locks"] == 17.0
    put = ts.advice(dict(idea, side="PE", target=80.0, peak_r=1.0, latest=95.0,
                         option_levels={"entry_price": 100.0, "stop_price": 80.0, "target_price": 140.0}))
    assert put["active"] and put["stop_option"] == 105.0 and put["now_option"] == 110.0
    assert ts.advice(dict(idea, option_levels=None, peak_r=2.0))["has_prices"] is False, "no option prices: no trailing stop"

    # --- when an alert is due: on, and at least Rs 3 above the last announced stop ---
    on = ts.advice(dict(idea, peak_r=1.4))                                   # stop 113
    assert ts.should_alert(on, 80.0) and ts.should_alert(on, 110.0) and not ts.should_alert(on, 111.0)
    assert not ts.should_alert(ts.advice(dict(idea, peak_r=0.5)), 80.0), "not on yet"
    assert not ts.should_alert(on, None)

    # --- the Telegram message: one line ---
    line = tg.format_trail({"option": "SENSEX 84400 CE", "advice": dict(on, now_option=116.0), "old_stop": 110.0})
    assert line == "SENSEX 84400 CE - SL 113 ------ OLD SL 110 | MP 116", line
    assert tg.format_trail({"option": "NIFTY 25450 PE", "advice": dict(on, stop_option=252.35, now_option=None), "old_stop": 248.4}) \
        == "NIFTY 25450 PE - SL 252.35 ------ OLD SL 248.4"

    # --- the journal: a steady climb of 1 point a candle ---
    df = make_candles(seed=2, days=3)
    days = sorted(set(df.index.normalize()))
    i = next(k for k in range(len(df)) if df.index[k].strftime("%H:%M") == "10:00" and df.index[k].normalize() == days[1])
    price = float(df["close"].iloc[i])
    for k in range(i + 1, len(df)):
        if df.index[k].normalize() != df.index[i].normalize():
            break
        prev = float(df["close"].iloc[k - 1])
        df.iloc[k, [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]] = [prev, prev + 1.05, prev - 0.05, prev + 1.0]
    f_full = cs.compute_features(df, "nifty", 5, None)
    plan = {"signal": "CE", "signal_time": f_full.index[i - 1].isoformat(), "decision_time": f_full.index[i].isoformat(),
            "mode": "confirm", "entry": price, "stop": price - 10, "target": price + 20, "risk_points": 10.0,
            "option": "NIFTY test CE", "score": 3, "context": {}, "reasons": []}
    jl = {"entry_price": 100.0, "stop_price": 80.0, "target_price": 140.0, "expiry": "x"}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cj.JOURNAL_FILE = os.path.join(tmp, "j.csv")
        cj.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
        assert cj.record_idea(plan, "nifty", "5m", "now", option_levels=jl, alerts="new")
        sent = []
        for k in range(i + 2, i + 22):                                       # the candles arrive one at a time
            f_k = cs.compute_features(df.iloc[: k + 1], "nifty", 5, None)
            cj.update_results(f_k, "nifty", 5, None, lambda info: sent.append(info))
            cj.update_results(f_k, "nifty", 5, None, lambda info: sent.append(info))      # asking again must not repeat it
        row = cj._load().iloc[0]
        stops = [x["advice"]["stop_option"] for x in sent]
        assert len(sent) >= 2 and stops == sorted(set(stops)), stops
        assert sent[0]["old_stop"] == 80.0 and [x["old_stop"] for x in sent[1:]] == stops[:-1]
        assert all(b - a_ >= 3.0 - 1e-9 for a_, b in zip([80.0] + stops, stops)), "each message is at least Rs 3 higher"
        assert all(x["advice"]["now_option"] is not None for x in sent)
        assert row["alerts"].startswith("new") and f"trail={stops[-1]:.2f}" in row["alerts"] and row["alerts"].count("trail=") == 1
        assert float(row["stop"]) == price - 10 and row["status"] == "TARGET", "the paper result still uses the FIRST stop"
        assert cj.open_ideas() == [], "a finished idea is not an open idea"

        # a jump: ONE message, for the newest stop
        cj.JOURNAL_FILE = os.path.join(tmp, "j2.csv")
        assert cj.record_idea(plan, "nifty", "5m", "now", option_levels=jl, alerts="new")
        sent2 = []
        cj.update_results(cs.compute_features(df.iloc[: i + 16], "nifty", 5, None), "nifty", 5, None, lambda info: sent2.append(info))
        assert len(sent2) == 1 and sent2[0]["old_stop"] == 80.0 and sent2[0]["advice"]["stop_option"] > 100.0

        # the dashboard list shows a running idea with its advice
        cj.JOURNAL_FILE = os.path.join(tmp, "j3.csv")
        assert cj.record_idea(plan, "nifty", "5m", "now", option_levels=jl, alerts="new")
        cj.update_results(cs.compute_features(df.iloc[: i + 14], "nifty", 5, None), "nifty", 5)
        running = cj.open_ideas()
        assert len(running) == 1 and running[0]["option"] == "NIFTY test CE"
        assert running[0]["advice"]["has_prices"] and running[0]["advice"]["active"] and running[0]["advice"]["stop_option"] > 100.0

        # an idea WITHOUT a new-idea alert gets no message and no mark; an idea without option prices gets none either
        cj.JOURNAL_FILE = os.path.join(tmp, "j4.csv")
        assert cj.record_idea(plan, "nifty", "5m", "now", option_levels=jl, alerts="")
        sent4 = []
        cj.update_results(cs.compute_features(df.iloc[: i + 16], "nifty", 5, None), "nifty", 5, None, lambda info: sent4.append(info))
        assert sent4 == [] and "trail" not in cj._load().iloc[0]["alerts"]
        cj.JOURNAL_FILE = os.path.join(tmp, "j5.csv")
        assert cj.record_idea(plan, "nifty", "5m", "now", option_levels=None, alerts="new")
        sent5 = []
        cj.update_results(cs.compute_features(df.iloc[: i + 16], "nifty", 5, None), "nifty", 5, None, lambda info: sent5.append(info))
        assert sent5 == [] and "trail" not in cj._load().iloc[0]["alerts"]

    print("OK: trailing stop: starts at +Rs 20, follows 15 below the highest, one line per rise of Rs 3, paper result unchanged")


if __name__ == "__main__":
    main()
