"""
test_risk_limits.py
-------------------
Checks the optional risk helpers: off by default, the daily limits, the position-size maths, and that
a limit saves new ideas as SKIPPED (no alert, no trade) without touching ideas saved earlier.

Run:   python test_risk_limits.py
"""

import os
import tempfile

import candle_journal as cj
import candle_signals as cs
import risk_limits as rl
from test_no_lookahead import make_candles


def main():
    # --- settings: everything off by default, bad values ignored ---
    off = rl.settings({})
    assert not rl.limits_on(off) and not rl.sizing_on(off) and rl.check({"ideas": 99, "net_r": -99}, off) is None
    cfg = rl.settings({"RISK_MAX_LOSS_R": "3", "RISK_MAX_IDEAS_PER_DAY": "4", "RISK_CAPITAL": "200000",
                       "RISK_PER_TRADE_PCT": "1"})
    assert rl.limits_on(cfg) and rl.sizing_on(cfg) and cfg["max_ideas"] == 4
    bad = rl.settings({"RISK_MAX_LOSS_R": "abc", "RISK_CAPITAL": "-5", "RISK_PER_TRADE_PCT": "1"})
    assert bad["max_loss_r"] is None and not rl.sizing_on(bad), "bad or negative values are ignored"

    # --- the two limits ---
    assert rl.check({"ideas": 3, "net_r": -2.9}, cfg) is None
    assert "4 ideas" in rl.check({"ideas": 4, "net_r": 1.0}, cfg)
    assert "-3.0R" in rl.check({"ideas": 1, "net_r": -3.0}, cfg)
    status = rl.day_status({"ideas": 1, "net_r": -3.2}, cfg)
    assert status["enabled"] and status["reached"] and status["max_loss_r"] == 3

    # --- position size: 200000 x 1% = 2000 budget; loss 20 per unit x lot 65 = 1300 per lot -> 1 lot ---
    z = rl.position_size(cfg, 20.0, 65)
    assert z["budget"] == 2000 and z["loss_per_lot"] == 1300 and z["lots"] == 1, z
    assert rl.position_size(cfg, 10.0, 65)["lots"] == 3                          # 650 per lot
    zero = rl.position_size(cfg, 40.0, 65)                                       # 2600 per lot > budget
    assert zero["lots"] == 0 and "more than your budget" in zero["note"]
    assert rl.position_size(off, 20.0, 65) is None and rl.position_size(cfg, None, 65) is None
    assert rl.position_size(cfg, 20.0, None) is None

    # --- journal: a limit saves NEW ideas as SKIPPED, with no alert and no trade ---
    loose = {k: False for k in cs.DEFAULT_FILTERS}
    cj.evaluate = lambda f, i, index, im, filters=None: cs.evaluate(f, i, index, im, loose)
    df = make_candles(seed=2, days=3)
    f = cs.compute_features(df, "nifty", 5, None)
    today = f["day"].iloc[-1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        cj.JOURNAL_FILE = os.path.join(tmp, "j.csv")
        cj.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
        alerts = []
        keep = lambda stats: rl.check(stats, rl.settings({"RISK_MAX_IDEAS_PER_DAY": "2"}))

        added = cj.scan_today(f, "nifty", "5m", 5, today, "now", None, lambda p: True,
                              lambda p, lv, mem: alerts.append(p["signal_time"]), keep)
        rows = cj._load()
        skipped = rows[rows["status"] == "SKIPPED"]
        traded = rows[rows["status"] != "SKIPPED"]
        assert len(traded) == 2 and len(skipped) >= 1, (len(traded), len(skipped))
        assert all(n.startswith("Daily limit") for n in skipped["note"]), "skipped rows say why"
        assert len(alerts) == 2, "no alert for a skipped idea"
        assert all(a in set(traded["signal_time"]) for a in alerts)
        assert all("new" not in str(a) for a in skipped["alerts"])
        assert cj.today_stats(today)["ideas"] == 2, "skipped ideas do not count as ideas made"

        # a second scan changes nothing: no duplicates, skipped stay skipped, no extra alerts
        before = cj._load().copy()
        cj.scan_today(f, "nifty", "5m", 5, today, "now", None, lambda p: True,
                      lambda p, lv, mem: alerts.append(p["signal_time"]), keep)
        after = cj._load()
        assert len(after) == len(before) and list(after["status"]) == list(before["status"]) and len(alerts) == 2
        # results are checked for the traded ideas only; skipped rows are untouched
        cj.update_results(f, "nifty", 5)
        again = cj._load()
        assert all(again.loc[skipped.index, "status"] == "SKIPPED")

        # net R today: only finished ideas count, cost included
        finished = again[again["status"].isin(cj.FINISHED)]
        expect = sum(float(r) - cj.COST_R for r in finished["r"])
        assert abs(cj.today_stats(today)["net_r"] - round(expect, 4)) < 1e-9

        # without a limit nothing is skipped
        cj.JOURNAL_FILE = os.path.join(tmp, "j2.csv")
        cj.scan_today(f, "nifty", "5m", 5, today, "now", None, None, None, None)
        assert not (cj._load()["status"] == "SKIPPED").any() or all(
            not str(n).startswith("Daily limit") for n in cj._load()["note"])

    print("OK: limits off by default, daily limits skip NEW ideas only (no alert, no trade), position-size maths")


if __name__ == "__main__":
    main()
