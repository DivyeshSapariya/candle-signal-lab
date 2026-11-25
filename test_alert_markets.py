"""
test_alert_markets.py
---------------------
New-idea alerts go only to the markets in ALERT_MARKETS. An idea found in another market is still saved
in the journal (no alert flag), so it never gets a result alert either.

Run:   python test_alert_markets.py
"""

import os
import tempfile

import candle_journal as cj
import candle_signals as cs
import dashboard as D
from test_no_lookahead import make_candles


def main():
    # --- which markets are allowed ---
    os.environ.pop("ALERT_MARKETS", None)
    assert D._alert_allowed("nifty", "5m") and D._alert_allowed("sensex", "5m")
    assert not D._alert_allowed("nifty", "15m") and not D._alert_allowed("sensex", "1m")
    os.environ["ALERT_MARKETS"] = "nifty:15m, sensex:5m"
    assert D._alert_allowed("nifty", "15m") and D._alert_allowed("sensex", "5m") and not D._alert_allowed("nifty", "5m")
    os.environ["ALERT_MARKETS"] = "rubbish,nifty:2m"
    assert D._alert_allowed("nifty", "5m"), "an unusable setting falls back to the default"
    os.environ.pop("ALERT_MARKETS", None)

    # --- the journal: an idea in a market without alerts is saved, flagged without 'new', and never alerted ---
    loose = {k: False for k in cs.DEFAULT_FILTERS}
    cj.evaluate = lambda f, i, index, im, filters=None: cs.evaluate(f, i, index, im, loose)
    df = make_candles(seed=2, days=3)
    f = cs.compute_features(df, "nifty", 5, None)
    today = f["day"].iloc[-1]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for interval, expect_alert in (("5m", True), ("15m", False)):
            cj.JOURNAL_FILE = os.path.join(tmp, f"j_{interval}.csv")
            cj.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
            alerted = []
            # the same rule the page uses: the market must be one of the chosen ones
            should = lambda plan, interval=interval: D._alert_allowed("nifty", interval)
            cj.scan_today(f, "nifty", interval, 5, today, "now", None, should,
                          lambda p, lv, mem: alerted.append(p["signal_time"]))
            rows = cj._load()
            assert len(rows) > 0, "the ideas are saved either way"
            if expect_alert:
                assert len(alerted) == len(rows) and all("new" in a for a in rows["alerts"])
            else:
                assert alerted == [] and all("new" not in a for a in rows["alerts"]), "no alert for a market that was not chosen"

            # a result alert goes out only for an idea that had a new-idea alert
            results = []
            cj.update_results(f, "nifty", 5, lambda info: results.append(info["key"]))
            if expect_alert:
                assert len(results) > 0
            else:
                assert results == [], "no result alert for an idea that never had a new-idea alert"

    print("OK: new-idea alerts only for the chosen markets; other markets are saved without alerts")


if __name__ == "__main__":
    main()
