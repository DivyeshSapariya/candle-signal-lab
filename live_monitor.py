"""
live_monitor.py
---------------
Watches NIFTY50 or SENSEX candles and prints the candle pattern report
every time a NEW candle finishes.

Examples:
    python live_monitor.py                              # NIFTY, 5-minute, Yahoo (free, no account)
    python live_monitor.py --index sensex --interval 15m
    python live_monitor.py --source angel               # Angel One free API (needs account)
    python live_monitor.py --once                       # check once and exit (good for testing)

Stop with Ctrl + C.
"""

import argparse
import time
from datetime import datetime

from candle_patterns import add_patterns, latest_candle_report
from data_sources import IST, INTERVAL_MINUTES, get_candles


def market_is_open(now: datetime) -> bool:
    """NSE / BSE: Monday to Friday, 9:15 to 15:30 IST (holidays are not checked)."""
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", choices=["nifty", "sensex"], default="nifty")
    parser.add_argument("--interval", choices=list(INTERVAL_MINUTES), default="5m")
    parser.add_argument("--source", choices=["yahoo", "angel"], default="yahoo")
    parser.add_argument("--once", action="store_true", help="check one time and exit")
    parser.add_argument("--wait", type=int, default=30, help="seconds between checks")
    args = parser.parse_args()

    print(f"Watching {args.index.upper()} | {args.interval} candles | source: {args.source}")
    print("Press Ctrl + C to stop.\n")

    last_seen = None
    closed_message_shown = False

    while True:
        now = datetime.now(IST)

        if not args.once and not market_is_open(now):
            if not closed_message_shown:
                print(f"[{now:%H:%M}] Market is closed. Waiting for 9:15 AM...")
                closed_message_shown = True
            time.sleep(60)
            continue
        closed_message_shown = False

        try:
            candles = get_candles(args.source, args.index, args.interval)
            analysed = add_patterns(candles)
            newest_time = analysed.index[-1]

            if newest_time != last_seen:
                last_seen = newest_time
                print("=" * 60)
                print(f"NEW CANDLE  ({args.index.upper()} {args.interval})")
                print(latest_candle_report(analysed))
                print()
        except Exception as error:  # keep running even if one request fails
            print(f"[{now:%H:%M:%S}] Could not get data: {error}")

        if args.once:
            break
        time.sleep(args.wait)


if __name__ == "__main__":
    main()
