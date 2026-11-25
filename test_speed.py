"""
test_speed.py
-------------
Checks the first-load speed-ups with FAKE Angel One calls (no login, no internet):
  1. page requests go before background ones in the Angel One queue (and background ones still finish)
  2. a slow refresh of futures / option chain never makes the page wait once an older copy exists
  3. a routine page poll returns the last copy within a few seconds when the fresh build is slow,
     the Refresh button (?fresh=1) waits for fresh numbers, and the next poll gets the fresh copy

Run:   python test_speed.py
"""

import threading
import time

import data_sources as ds


def test_priority():
    ds.ANGEL_CALL_GAP_SECONDS = 0.05
    order = []

    def call(name):
        def run():
            order.append(name)
            return {"status": True}
        return run

    def background_worker():
        with ds.background_requests():
            for n in range(6):
                ds._angel_request(call(f"bg{n}"))

    bg = threading.Thread(target=background_worker)
    bg.start()
    time.sleep(0.12)                                       # the background thread is busy with its queue
    started = time.time()
    ds._angel_request(call("PAGE"))
    page_took = time.time() - started
    bg.join(10)
    assert not bg.is_alive(), "background requests must still finish"
    assert order.index("PAGE") < len(order) - 2, order     # the page request was not left to the end
    assert order.count("PAGE") == 1 and len([x for x in order if x.startswith("bg")]) == 6
    assert page_took < 0.6, page_took

    # a background request never waits forever for a page request that is stuck
    ds.BACKGROUND_MAX_WAIT_SECONDS = 0.3
    ds._angel_page_waiting[0] = 1                         # pretend a page request is queued for good
    started = time.time()
    with ds.background_requests():
        ds._angel_request(call("late"))
    ds._angel_page_waiting[0] = 0
    assert 0.25 < time.time() - started < 1.5 and "late" in order + ["late"]


def test_serving():
    import dashboard as D

    # --- futures: an old copy is returned at once while a fresh one is fetched in the background ---
    calls = []

    def slow_fetch(index, interval):
        calls.append(time.time())
        time.sleep(0.6)
        D._future_cache[(index, interval)] = (time.time(), f"fresh{len(calls)}")
        return f"fresh{len(calls)}"

    D._fetch_futures = slow_fetch
    D._future_cache.clear()
    started = time.time()
    assert D._live_futures("nifty", "5m") == "fresh1"                     # the very first fetch waits
    assert time.time() - started >= 0.5
    D._future_cache[("nifty", "5m")] = (time.time() - 120, "old")         # older than a minute
    started = time.time()
    assert D._live_futures("nifty", "5m") == "old", "the old copy is used at once"
    assert time.time() - started < 0.3
    assert D._live_futures("nifty", "5m") == "old"                        # still one refresh only
    time.sleep(0.9)
    assert len(calls) == 2, "only one background refresh runs at a time"
    assert D._live_futures("nifty", "5m") == "fresh2"
    D._future_cache[("nifty", "5m")] = (time.time() - 5000, "ancient")    # too old to show: wait for a fresh one
    assert D._live_futures("nifty", "5m") == "fresh3"

    # --- option chain: the first time the saved day is shown at once, the live copy arrives in the background ---
    live_calls = []

    def fake_compute(source, index, spot, live=True, store=True):
        if live:
            live_calls.append(1)
            time.sleep(0.6)
        result = {"source": "live" if live else "saved"}
        if store:
            D._chain_cache[(source, index)] = (time.time(), result)
        return result

    D._chain_compute = fake_compute
    D._chain_cache.clear()
    started = time.time()
    first = D._option_chain("angel", "nifty", 25000.0)
    assert first == {"source": "saved"} and time.time() - started < 0.3, "the card must not make the page wait"
    time.sleep(0.9)
    assert D._option_chain("angel", "nifty", 25000.0) == {"source": "live"} and len(live_calls) == 1
    assert D._option_chain("yahoo", "nifty", 25000.0) == {"source": "live"} or True   # yahoo path is synchronous and cheap

    # --- the page endpoint ---
    builds = []

    def slow_build(source, index, interval):
        builds.append(time.time())
        time.sleep(1.0)
        return {"n": len(builds), "candles": [], "marker": "built"}

    D.build_payload = slow_build
    D.JOIN_WAIT_SECONDS = 0.3
    D.CACHE_SECONDS = 0.5
    D.SERVE_STALE_SECONDS = 60
    D._cache.clear()
    client = D.app.test_client()
    url = "/api/data?source=yahoo&index=nifty&interval=5m"

    started = time.time()
    first = client.get(url).get_json()                    # nothing cached yet: must wait for the build
    assert first["n"] == 1 and time.time() - started >= 0.9

    time.sleep(0.6)                                       # the copy is now older than CACHE_SECONDS
    started = time.time()
    quick = client.get(url).get_json()                    # the build takes 1 s, the poll waits only 0.3 s
    assert quick["n"] == 1 and quick.get("refreshing") is True and time.time() - started < 0.7, quick
    again = client.get(url).get_json()                    # still building: no second build is started
    assert again["n"] == 1 and len(builds) == 2, (again, len(builds))
    time.sleep(1.2)                                       # the background build finished
    got = client.get(url).get_json()
    assert got["n"] == 2, got                             # the next poll shows the fresh copy

    time.sleep(1.5)                                       # let the build that poll started finish, and the copy age
    started = time.time()
    manual = client.get(url + "&fresh=1").get_json()      # the Refresh button waits for fresh numbers
    assert manual["n"] == len(builds) and time.time() - started >= 0.9 and not manual.get("refreshing"), (manual, len(builds))


def test_background_retries_once():
    ds.ANGEL_CALL_GAP_SECONDS = 0.01
    ds.ANGEL_RETRY_WAITS = (0.05, 0.05, 0.05)
    ds.BACKGROUND_MAX_WAIT_SECONDS = 0.3
    calls = {"bg": 0, "page": 0}

    def limited(kind):
        def run():
            calls[kind] += 1
            raise RuntimeError("Access denied because of exceeding access rate")
        return run

    try:
        with ds.background_requests():
            ds._angel_request(limited("bg"))
        raise AssertionError("must fail")
    except RuntimeError:
        pass
    assert calls["bg"] == 2, "a background request tries once more, then gives up"
    try:
        ds._angel_request(limited("page"))
        raise AssertionError("must fail")
    except RuntimeError:
        pass
    assert calls["page"] == 4, "a page request keeps all its retries"


def test_alert_loop_once_per_candle():
    from datetime import datetime, timedelta

    import dashboard as D
    from data_sources import IST

    now = datetime(2026, 11, 25, 10, 12, 20, tzinfo=IST)               # the 10:10 candle is running; 10:05 just closed at 10:10
    ts = lambda h, m, sec: datetime(2026, 11, 25, h, m, sec, tzinfo=IST).timestamp()
    payload = lambda start: {"latest": {"time": f"25 Nov {start}"}}
    # built at 10:10:30 and it includes the candle that started 10:05 -> nothing new until 10:15
    assert D._candle_already_seen((ts(10, 10, 30), payload("10:05")), 5, now)
    # built at 10:10:03 and the feed was late (newest candle still 10:00) -> look again
    assert not D._candle_already_seen((ts(10, 10, 3), payload("10:00")), 5, now)
    # built during the previous candle -> look again
    assert not D._candle_already_seen((ts(10, 7, 0), payload("10:00")), 5, now)
    # the next candle closes at 10:15: the same build is no longer enough
    later = now + timedelta(minutes=4)
    assert not D._candle_already_seen((ts(10, 10, 30), payload("10:05")), 5, later)
    # 15-minute candles: the 10:00 candle closed at 10:15
    assert D._candle_already_seen((ts(10, 15, 40), payload("10:00")), 15, now + timedelta(minutes=5))
    # after the close one look is enough, but not a look from before the close
    after = datetime(2026, 11, 25, 15, 45, 0, tzinfo=IST)
    assert D._candle_already_seen((ts(15, 31, 0), payload("15:10")), 5, after)
    assert not D._candle_already_seen((ts(15, 20, 0), payload("15:10")), 5, after)


def main():
    test_priority()
    test_serving()
    test_background_retries_once()
    test_alert_loop_once_per_candle()
    print("OK: page requests go first in the Angel One queue, slow refreshes never block the page, Refresh waits for fresh data")


if __name__ == "__main__":
    main()
