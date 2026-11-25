"""
test_holidays.py
----------------
Checks that holidays come from holidays.csv, that a bad line never breaks anything, that a changed
file is noticed by itself, and that a holiday means "Market closed" and no alerts.

Run:   python test_holidays.py
"""

import os
import tempfile
import time
from datetime import date, datetime

import live_monitor
import option_pricer as op
from data_sources import IST


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    stamp = time.time() + write.counter                 # make sure the file time really changes
    write.counter += 2
    os.utime(path, (stamp, stamp))


write.counter = 1


def main():
    # the real file shipped with the project has the two known holidays
    assert os.path.exists(op.HOLIDAYS_FILE)
    assert op.is_holiday(date(2026, 11, 24)) and op.is_holiday(date(2026, 12, 25))
    assert not op.is_holiday(date(2026, 11, 25))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "h.csv")
        os.environ["HOLIDAYS_FILE"] = path
        try:
            # file rules: comments, header, blank lines; one bad line is skipped, not fatal
            write(path, "# comment\ndate,name\n\n2027-01-26,Republic Day\nnot-a-date,Oops\n2027-03-03,Holi\n")
            found, problems = op.read_holidays(path)
            assert found == {date(2027, 1, 26), date(2027, 3, 3)} and len(problems) == 1, (found, problems)
            assert op.is_holiday(date(2027, 1, 26)) and not op.is_holiday(date(2026, 11, 24)), "file replaces the built-in list"

            # editing the file is noticed without a restart
            write(path, "date,name\n2027-01-26,Republic Day\n2027-05-01,Maharashtra Day\n")
            assert op.is_holiday(date(2027, 5, 1)) and not op.is_holiday(date(2027, 3, 3))

            # a holiday: market closed, expiry moves to the previous trading day
            holiday = date(2027, 5, 1)                          # a Saturday: use a weekday one instead
            write(path, "date,name\n2027-04-27,Test holiday\n")
            tuesday = datetime(2027, 4, 27, 11, 0, tzinfo=IST)
            assert tuesday.weekday() == 1 and not live_monitor.market_is_open(tuesday)
            assert op.previous_trading_day(date(2027, 4, 27)) == date(2027, 4, 26)
            assert not op.is_expiry_day("nifty", date(2027, 4, 27)) and op.is_expiry_day("nifty", date(2027, 4, 26))
            normal = datetime(2027, 4, 28, 11, 0, tzinfo=IST)
            assert live_monitor.market_is_open(normal)

            # missing file: the two built-in dates are used, nothing crashes
            os.remove(path)
            assert op.is_holiday(date(2026, 11, 24)) and op.is_holiday(date(2026, 12, 25))

            # an empty file means no holidays at all (your choice), still no crash
            write(path, "date,name\n")
            assert not op.is_holiday(date(2026, 11, 24))
        finally:
            os.environ.pop("HOLIDAYS_FILE", None)

    # the alert loop does nothing on a holiday
    import dashboard
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "h.csv")
        os.environ["HOLIDAYS_FILE"] = path
        try:
            today = datetime.now(IST)
            write(path, f"date,name\n{today.date().isoformat()},Today\n")
            assert dashboard._alert_tick(source="yahoo", markets=[("nifty", "5m")]) == 0
        finally:
            os.environ.pop("HOLIDAYS_FILE", None)

    print("OK: holidays.csv is read, bad lines skipped, edits noticed, holiday = market closed and no alerts")


if __name__ == "__main__":
    main()
