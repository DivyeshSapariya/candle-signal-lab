"""
dashboard.py
------------
Live chart + live candle analysis in your browser.

Run:
    pip install flask pandas yfinance
    python dashboard.py
It opens  http://127.0.0.1:5000  in your browser.

Options:
    python dashboard.py --port 5050
    python dashboard.py --no-browser

The chart uses TradingView "Lightweight Charts" (free library, loaded from the internet
by your browser, so you need internet on the computer).
"""

import argparse
import os
import threading
import time
import webbrowser
from datetime import datetime, timedelta

import pandas as pd
from flask import Flask, Response, jsonify, request

import candle_journal as journal
import candle_memory
import telegram_notify as tg
from candle_patterns import PATTERN_INFO
from data_sources import (IST, INTERVAL_MINUTES, angel_futures, angel_option_contracts, angel_too_fast, drop_forming_candle,
                          fetch_angel_future, fetch_angel_option, fetch_angel_option_oi, get_candles, option_lot_size)
import data_sources
from option_pricer import build_option_levels, get_vix_daily, is_holiday
from candle_analysis import live_analysis, rejected_setups
import candle_chain
import risk_limits
import candle_store
from candle_signals import compute_features, TRADE_START_MIN, TRADE_END_MIN, EXPIRY_CUTOFF_MIN
from live_monitor import market_is_open
from trade_plan import build_trade_plan

tg.load_env_file()                        # optional .env file with the Telegram settings (never uploaded to Git)

app = Flask(__name__)

IST_OFFSET_SECONDS = 5 * 3600 + 30 * 60   # chart shows UTC, so we shift times to IST
MARKER_CANDLES = 200                      # show pattern arrows only on the last 200 candles
MARKER_TEXT_CANDLES = 40                  # show pattern names only on the last 40 candles
ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET")


def default_source() -> str:
    """Angel One (live data, real option prices) when its four values are in .env, else Yahoo."""
    return "angel" if all(os.environ.get(k) for k in ANGEL_KEYS) else "yahoo"


JOIN_WAIT_SECONDS = 4.0                   # how long a page poll waits for a fresh build before showing the last copy
SERVE_STALE_SECONDS = 600                 # the last copy may be shown for this long while a fresh one is built
STALE_SECONDS = 300                       # if the data source fails, show the last good data for up to 5 minutes
CACHE_SECONDS = 10                        # do not call the data source more than every 10s
_cache = {}
SERVER_TOKEN = str(time.time())           # new value every time the server restarts (page auto-refresh)


def pretty(name: str) -> str:
    return name.replace("_", " ").title()


def chart_time(ts) -> int:
    return int(ts.timestamp()) + IST_OFFSET_SECONDS


_inflight = {}
_inflight_guard = threading.Lock()


def _spawn_once(key, fn, background: bool = False):
    """
    Run fn() in a background thread, but only ONE thread per key at a time (a second call returns the
    thread that is already running). background=True makes its Angel One calls low priority.
    """
    with _inflight_guard:
        running = _inflight.get(key)
        if running is not None and running.is_alive():
            return running

        def work():
            try:
                if background:
                    with data_sources.background_requests():
                        fn()
                else:
                    fn()
            except Exception as error:
                print(f"[background] {key}: {error}")
            finally:
                with _inflight_guard:
                    _inflight.pop(key, None)

        thread = threading.Thread(target=work, name=f"refresh-{key[0]}", daemon=True)
        _inflight[key] = thread
        thread.start()
        return thread


_future_cache = {}
FUTURE_CACHE_SECONDS = 60
FUTURE_STALE_SECONDS = 600    # an older copy may be shown while a fresh one is fetched in the background
FUTURE_DAYS = 6               # enough candles for the 20-candle volume average


def _fetch_futures(index: str, interval: str):
    try:
        contract = angel_futures(index)[0]
        now = datetime.now(IST)
        df = fetch_angel_future(index, interval, now - timedelta(days=FUTURE_DAYS), now, contract)
    except Exception as error:
        print(f"[futures] volume not available: {error}")
        df = None
    _future_cache[(index, interval)] = (time.time(), df)
    return df


def _live_futures(index: str, interval: str):
    """
    Recent candles of the nearest index future (real volume), only with Angel One. Cached for a minute;
    after that the old copy is used at once while a fresh one is fetched in the background (only the very
    first fetch waits). Returns None when it cannot be fetched: the VWAP then falls back to the day's average price.
    """
    key = (index, interval)
    cached = _future_cache.get(key)
    age = time.time() - cached[0] if cached else None
    if cached and age < FUTURE_CACHE_SECONDS:
        return cached[1]
    if cached and cached[1] is not None and age < FUTURE_STALE_SECONDS:
        _spawn_once(("futures", key), lambda: _fetch_futures(index, interval), background=True)
        return cached[1]
    return _fetch_futures(index, interval)


_chain_cache = {}
CHAIN_CACHE_SECONDS = 120


def _option_chain(source: str, index: str, spot: float):
    """
    Open-interest facts for the card. With Angel One: the CURRENT OI of the contracts around the index
    level (one read-only request, cached for a minute), compared with the last saved session. Otherwise,
    or if that fails: the last saved day (python candle_store.py backfill-options). None when nothing exists.
    The card never makes the page wait: the live numbers are fetched in the background, and until they
    arrive the old copy (or, the very first time, the last saved day) is shown.
    """
    key = (source, index)
    cached = _chain_cache.get(key)
    if cached and time.time() - cached[0] < CHAIN_CACHE_SECONDS:
        return cached[1]
    if source != "angel":
        return _chain_compute(source, index, spot)
    _spawn_once(("chain", key), lambda: _chain_compute(source, index, spot), background=True)
    if cached:
        return cached[1]
    return _chain_compute(source, index, spot, live=False, store=False)


def _chain_compute(source: str, index: str, spot: float, live: bool = True, store: bool = True):
    key = (source, index)
    result = None
    try:
        saved = candle_store.load_option_chain(index)
    except Exception as error:
        print(f"[chain] saved data not readable: {error}")
        saved = None
    if source == "angel" and live:
        try:
            contracts = angel_option_contracts(index, spot)
            oi, traded = fetch_angel_option_oi(contracts)
            current = {(c["strike"], c["side"]): oi[c["token"]] for c in contracts if c["token"] in oi}
            expiry = contracts[0]["expiry"].isoformat()
            before = candle_chain.previous_session(saved, expiry, datetime.now(IST).date()) if saved is not None else {}
            result = candle_chain.summarize(current, before, spot, expiry, "live", traded or "now")
        except Exception as error:
            print(f"[chain] live open interest not available: {error}")
    if result is None and saved is not None:
        snap = candle_chain.saved_snapshot(saved)
        if snap:
            expiry, current, before, day = snap
            result = candle_chain.summarize(current, before, spot, expiry, "saved", day)
    if store:
        _chain_cache[key] = (time.time(), result)
    return result


def build_payload(source: str, index: str, interval: str) -> dict:
    started = time.time()
    full = get_candles(source, index, interval, include_forming=True)
    t_candles = time.time() - started
    finished = drop_forming_candle(full, interval)
    if len(finished) < 5:
        raise RuntimeError("Not enough finished candles yet. Try again in a few minutes.")

    interval_min = INTERVAL_MINUTES[interval]
    t_mark = time.time()
    fut = _live_futures(index, interval) if source == "angel" else None
    t_futures = time.time() - t_mark
    t_mark = time.time()
    vix_daily = get_vix_daily()
    t_vix = time.time() - t_mark
    analysed = compute_features(finished, index, interval_min, vix_daily, fut)
    pattern_cols = list(PATTERN_INFO.keys())

    # --- candles for the chart (includes the running candle) ---
    candles = [
        {"time": chart_time(ts), "open": float(r.open), "high": float(r.high),
         "low": float(r.low), "close": float(r.close)}
        for ts, r in full.iterrows()
    ]

    # --- indicator lines on the chart: VWAP (one piece per day, it restarts every morning) and the two EMAs ---
    lines = {"ema9": [], "ema21": [], "vwap": []}
    vwap_day, vwap_points = None, None
    for ts, r in analysed.iterrows():
        t = chart_time(ts)
        if pd.notna(r["ema9"]):
            lines["ema9"].append({"time": t, "value": round(float(r["ema9"]), 2)})
        if pd.notna(r["ema21"]):
            lines["ema21"].append({"time": t, "value": round(float(r["ema21"]), 2)})
        if pd.notna(r["vwap"]):
            if ts.date() != vwap_day:
                vwap_day, vwap_points = ts.date(), []
                lines["vwap"].append(vwap_points)
            vwap_points.append({"time": t, "value": round(float(r["vwap"]), 2)})

    # --- zones where the rules say no (first 15 min / after 3 pm, expiry-day afternoon), day open, sub-panel series ---
    zones = []
    for ts, r in analysed.iterrows():
        if bool(r["is_expiry"]) and r["start_min"] >= EXPIRY_CUTOFF_MIN:
            zones.append({"time": chart_time(ts), "kind": "expiry"})
        elif r["start_min"] < TRADE_START_MIN or r["start_min"] + interval_min > TRADE_END_MIN:
            zones.append({"time": chart_time(ts), "kind": "time"})

    def _col(series, digits=2):
        return [{"time": chart_time(t), "value": round(float(v), digits)} for t, v in series.items() if pd.notna(v)]

    panels = {"adx": _col(analysed["adx"], 1), "vix": _col(analysed["vix_prev"], 2), "volx": _col(analysed["vol_ratio"], 2),
              "oi_pcr": [], "oi_flow": []}
    try:                                  # open interest panels use the SAVED option history only (no rule uses them)
        saved_oi = candle_store.load_option_chain(index)
        if len(saved_oi):
            oi = candle_chain.oi_features(saved_oi, analysed.index, interval_min)
            panels["oi_pcr"] = _col(oi["oi_pcr"], 2)
            panels["oi_flow"] = _col(oi["oi_flow"] * 100, 2)
    except Exception as error:
        print(f"[chart] open interest panels not available: {error}")
    last_day = analysed["day"].iloc[-1]
    day_rows = analysed[analysed["day"] == last_day]
    levels = {k: (None if pd.isna(analysed.iloc[-1][k]) else round(float(analysed.iloc[-1][k]), 2))
              for k in ("pdh", "pdl", "pdc", "or_high", "or_low", "swing_high", "swing_low", "gap_pct")}
    levels["day_open"] = round(float(day_rows["open"].iloc[0]), 2)

    # --- arrows on the chart + list of recent patterns ---
    markers, recent = [], []
    for ts, row in analysed.iterrows():
        found = [p for p in pattern_cols if bool(row[p])]
        if not found:
            continue
        leans = PATTERN_INFO[found[0]]["leans"]
        recent.append({
            "time": ts.strftime("%d %b %H:%M"),
            "patterns": [pretty(p) for p in found],
            "leans": leans,
        })
        if ts >= analysed.index[-MARKER_CANDLES:][0]:
            if leans == "CE":
                position, shape, color = "belowBar", "arrowUp", "#2bd4b4"
            elif leans == "PE":
                position, shape, color = "aboveBar", "arrowDown", "#ff7a70"
            else:
                position, shape, color = "aboveBar", "circle", "#9aa0a6"
            show_text = ts >= analysed.index[-MARKER_TEXT_CANDLES:][0]
            markers.append({"time": chart_time(ts), "position": position, "shape": shape,
                            "color": color,
                            "text": " + ".join(pretty(p) for p in found) if show_text else ""})

    # --- latest finished candle ---
    last_ts = analysed.index[-1]
    last = analysed.iloc[-1]
    found_now = []
    for name in pattern_cols:
        if bool(last[name]):
            info = PATTERN_INFO[name]
            found_now.append({"name": pretty(name), "leans": info["leans"],
                              "meaning": info["meaning"], "needs": info["needs"]})

    colour = "GREEN" if last["green"] else "RED" if last["red"] else "FLAT"
    if found_now:
        first = found_now[0]
        if first["leans"] == "WAIT":
            summary = (f"{first['name']} found. The market is confused. "
                       "Best action: wait for the next candle.")
        else:
            summary = (f"{first['name']} found. It leans towards {first['leans']}. "
                       "But this is only one candle. Wait until support / resistance and "
                       "volume also agree before thinking about a trade.")
    else:
        summary = "No clear pattern on the last finished candle. Best action: wait."

    now = datetime.now(IST)
    market_open = market_is_open(now)
    newest = full.index[-1]
    lag_min = max(0.0, (now - newest).total_seconds() / 60)
    delayed = market_open and lag_min > INTERVAL_MINUTES[interval] + 3
    current_price = float(full["close"].iloc[-1])
    plan = build_trade_plan(analysed, index, interval_min, current_price)
    try:
        analysis = live_analysis(plan, last_ts, interval_min, market_open, last)
        analysis["rejected"] = rejected_setups(analysed, index, interval_min)
    except Exception as error:                          # the explanation must never break the dashboard
        print(f"[analysis] problem: {error}")
        analysis = None

    t_mark = time.time()
    try:
        chain = _option_chain(source, index, current_price)
    except Exception as error:                          # a side card must never break the dashboard
        print(f"[chain] problem: {error}")
        chain = None
    t_chain = time.time() - t_mark

    risk_cfg = risk_limits.settings()
    try:
        risk = risk_limits.day_status(journal.today_stats(now.date()), risk_cfg)
    except Exception as error:
        print(f"[risk] could not read today's numbers: {error}")
        risk = None

    # Convert index levels into OPTION price levels (what you actually buy / sell)
    if plan["signal"] != "NONE":
        live_price, live_expiry = None, None
        if source == "angel":
            try:
                live = fetch_angel_option(index, plan["strike"], plan["signal"])
                live_price, live_expiry = live["ltp"], live["expiry"]
            except Exception as error:
                print(f"[option price] could not get live option price: {error}")
        try:
            plan["option_levels"] = build_option_levels(
                index, plan["signal"], plan["strike"], current_price,
                plan["entry"], plan["stop"], plan["target"],
                now=now, live_price=live_price, expiry=live_expiry)
        except Exception as error:
            print(f"[option price] could not calculate option levels: {error}")
        try:                                              # display-only: how many lots fit your risk (off unless set in .env)
            o = plan.get("option_levels")
            plan["sizing"] = (risk_limits.position_size(risk_cfg, o.get("loss_per_unit"), option_lot_size(index))
                              if o else None)
        except Exception as error:
            print(f"[risk] position size not available: {error}")

    # Automatic history: save every idea of today (even ones that already vanished from the card),
    # then check how earlier ideas ended. Finished ideas also go into the memory.
    def option_levels_for(idea: dict):
        """Option prices to save with a NEW idea, so they never have to be guessed later."""
        if (plan["signal"] != "NONE" and plan.get("option_levels")
                and plan.get("decision_time") == idea.get("decision_time")):
            # this is the idea on screen right now: save the price as it is at this moment
            saved = dict(plan["option_levels"])
            live_now = saved.get("price_source") == "live" and market_open and not delayed
            saved["captured"] = "live" if live_now else "estimate_now"
            saved["captured_at"] = now.isoformat(timespec="seconds")
            return saved
        # an idea found after the fact (it appeared while nobody was looking): best estimate
        return _estimate_option_levels(
            index, interval, idea["option"], idea["signal"], idea["signal_time"],
            idea["entry"], idea["stop"], idea["target"], (idea.get("context") or {}).get("vix"))

    def should_alert(idea: dict) -> bool:
        """
        Telegram alert only for the idea that is on screen right now, while the market is open.
        Ideas found after the fact are stale, so they are saved but not sent. Old data (free Yahoo
        is always about 15 minutes late) still sends the alert, marked DELAYED DATA, unless
        TELEGRAM_ALLOW_DELAYED=0.
        """
        if not tg.is_configured() or not market_open:
            return False
        if plan["signal"] == "NONE" or plan.get("decision_time") != idea.get("decision_time"):
            return False
        return (not delayed) or tg.allow_delayed()

    def on_new_idea(idea: dict, levels, memory) -> None:
        tg.send_async(tg.format_idea(
            plan if plan.get("decision_time") == idea.get("decision_time") else idea,
            levels, memory, setup=journal.setup_line(idea.get("context") or {}),
            index=index, interval=interval, status=plan.get("status", ""),
            delayed_min=round(lag_min) if delayed else None))

    def on_finished(info: dict) -> None:
        tg.send_async(tg.format_result(info))

    try:
        journal.scan_today(analysed, index, interval, interval_min, now.date(),
                           now.isoformat(timespec="seconds"), option_levels_for,
                           should_alert, on_new_idea,
                           (lambda stats: risk_limits.check(stats, risk_cfg)) if risk_limits.limits_on(risk_cfg) else None)
        journal.update_results(analysed, index, interval_min, on_finished)
        journal_summary = journal.summary()
    except Exception as error:
        print(f"[journal] problem: {error}")
        journal_summary = None
    return {
        "index": index.upper(),
        "interval": interval,
        "source": source,
        "updated": now.strftime("%H:%M:%S"),
        "market_open": market_open,
        "newest_candle": newest.strftime("%H:%M"),
        "data_lag_min": round(lag_min),
        "delayed": delayed,
        "learning_only": (not market_open) or delayed,
        "plan": plan,
        "analysis": analysis,
        "chain": chain,
        "timing": {"candles": round(t_candles, 2), "futures": round(t_futures, 2),
                   "vix": round(t_vix, 2), "chain": round(t_chain, 2), "total": round(time.time() - started, 2),
                   "angel": dict(data_sources.angel_stats, recent=list(data_sources.angel_recent[-14:]))},
        "risk": risk,
        "journal": journal_summary,
        "last_price": current_price,
        "prev_price": float(full["close"].iloc[-2]) if len(full) > 1 else None,
        "candle_running": market_open and len(full) > len(finished),     # after the close the dropped candle is just the closing print
        "candles": candles,
        "lines": lines,
        "levels": levels,
        "zones": zones,
        "panels": panels,
        "markers": markers,
        "latest": {
            "time": last_ts.strftime("%d %b %H:%M"),
            "open": float(last["open"]), "high": float(last["high"]),
            "low": float(last["low"]), "close": float(last["close"]),
            "colour": colour,
            "trend_before": last["prior_trend"],
            "patterns": found_now,
        },
        "summary": summary,
        "recent": list(reversed(recent))[:8],
    }


_build_locks = {}
_build_locks_guard = threading.Lock()


def _fresh_payload(source: str, index: str, interval: str) -> dict:
    """
    The data for one market. Only ONE build per market runs at a time: requests that arrive while it
    is running wait for it and then use its result, so several tabs or the alert loop never repeat
    the same data-source calls (Angel One refuses too many requests).
    """
    key = (source, index, interval)
    with _build_locks_guard:
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        payload = build_payload(source, index, interval)
        _cache[key] = (time.time(), payload)
        return payload


@app.route("/api/data")
def api_data():
    source = request.args.get("source", default_source())
    index = request.args.get("index", "nifty")
    interval = request.args.get("interval", "5m")

    if source not in ("yahoo", "angel") or index not in ("nifty", "sensex") \
            or interval not in INTERVAL_MINUTES:
        return jsonify({"error": "Invalid options."}), 400

    key = (source, index, interval)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return jsonify(cached[1])

    # A routine poll never waits long: if the fresh build is not ready in a few seconds (Angel One calls are
    # queued), the last copy is shown at once and the next poll gets the new one. The Refresh button and a
    # change of market (?fresh=1) always wait for fresh numbers.
    if cached and request.args.get("fresh") != "1" and time.time() - cached[0] < SERVE_STALE_SECONDS:
        worker = _spawn_once(("payload", key), lambda: _fresh_payload(source, index, interval))
        worker.join(JOIN_WAIT_SECONDS)
        newest = _cache.get(key)
        if newest and newest[0] > cached[0]:
            return jsonify(newest[1])
        return jsonify({**cached[1], "refreshing": True})

    try:
        payload = _fresh_payload(source, index, interval)
    except Exception as error:
        busy = angel_too_fast(error=error)
        if cached and time.time() - cached[0] < STALE_SECONDS:
            # keep showing the last good numbers instead of an error box; the next refresh tries again
            return jsonify({**cached[1], "stale": True})
        if busy:
            return jsonify({"error": "Angel One is busy (too many requests). "
                                     "Waiting a few seconds and trying again automatically."}), 502
        return jsonify({"error": str(error)}), 502

    return jsonify(payload)


def _estimate_option_levels(index: str, interval: str, option: str, side: str, signal_time: str,
                            entry: float, stop: float, target: float, vix):
    """
    ESTIMATED option prices (buy / stop loss / target) as they would have been when an idea was made,
    worked out from the index levels and the India VIX saved with the idea (vix in %, e.g. 15.6).
    Returns None if anything is missing.
    """
    try:
        strike = int(str(option).split()[1])
        minutes = INTERVAL_MINUTES[interval]
        # the idea is known when the confirmation candle (the one after the signal candle) closes
        when = datetime.fromisoformat(signal_time) + timedelta(minutes=2 * minutes)
        levels = build_option_levels(
            index.lower(), side, strike, entry, entry, stop, target,
            now=when, sigma_override=(vix / 100.0) if vix else None)
        levels["captured"] = "estimate_at_decision"
        return levels
    except Exception as error:
        print(f"[option price estimate] {error}")
        return None


def _history_option_levels(item: dict):
    """Option prices for a past idea: the ones saved when it was made, else an estimate."""
    if item.get("option_levels"):
        return item["option_levels"]
    return _estimate_option_levels(item["index"], item["interval"], item["option"], item["side"],
                                   item["sort"], item["entry"], item["stop"], item["target"],
                                   item.get("vix"))


@app.route("/api/history")
def api_history():
    data = journal.history(limit=150)
    for item in data["items"]:
        item["option_levels"] = _history_option_levels(item)
    data["lessons"] = candle_memory.lessons()
    return jsonify(data)


@app.route("/api/version")
def api_version():
    """The page checks this. When it changes, the server was restarted after a code change."""
    response = jsonify({"token": SERVER_TOKEN})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/snapshot")
def api_snapshot():
    snap = journal.load_snapshot(request.args.get("key", ""))
    if snap is None:
        return jsonify({"error": "No chart saved yet. It is saved when the idea finishes."}), 404
    return jsonify(snap)


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


# ---------------------------------------------------------------------------
# Telegram alerts: a background loop that watches the markets, so an idea is found (and alerted)
# even when no browser tab is open. The alert itself is sent from inside build_payload(), which is
# also what a browser tab calls, so a new idea is alerted ONCE, whoever finds it first.
# ---------------------------------------------------------------------------
ALERT_POLL_SECONDS = 30


def _alert_markets() -> list:
    """The (index, interval) pairs to watch, from ALERT_MARKETS (default nifty:5m,sensex:5m)."""
    found = []
    for part in os.environ.get("ALERT_MARKETS", "nifty:5m,sensex:5m").split(","):
        index, _, interval = part.strip().lower().partition(":")
        if index in ("nifty", "sensex") and interval in INTERVAL_MINUTES and (index, interval) not in found:
            found.append((index, interval))
    return found or [("nifty", "5m"), ("sensex", "5m")]


ALERT_CANDLE_GRACE_SECONDS = 10       # after the close, one look this long after 3:30 pm is enough


def _candle_already_seen(cached, interval_min: int, now: datetime) -> bool:
    """
    True when the cached build (built_at, payload) already includes the candle that closed most recently.
    The alert loop needs a market only once per candle (an idea can only appear when a candle closes), which
    keeps its Angel One calls few: Angel One refuses requests when there are many. If the data source is
    late with the new candle, the answer stays False and the next tick looks again.
    """
    built_at, payload = cached
    close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= close:
        return built_at >= close.timestamp() + ALERT_CANDLE_GRACE_SECONDS      # after the close one look is enough
    boundary = now.replace(minute=(now.minute // interval_min) * interval_min if interval_min < 60 else 0,
                           second=0, microsecond=0)
    if built_at < boundary.timestamp():
        return False                                                           # built before this candle closed
    expected = (boundary - timedelta(minutes=interval_min)).strftime("%d %b %H:%M")
    return (payload.get("latest") or {}).get("time") == expected


def _alert_tick(source: str = None, markets: list = None) -> int:
    """
    One look at every watched market. Runs only on weekdays from 9:10 to 16:00 IST (the last
    half hour after the close is for late results). Returns how many markets were checked.
    """
    source = source or os.environ.get("ALERT_SOURCE", default_source()).strip().lower()
    now = datetime.now(IST)
    minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5 or is_holiday(now.date()) or not (9 * 60 + 10 <= minutes <= 16 * 60):
        return 0
    checked = 0
    for index, interval in (markets or _alert_markets()):
        key = (source, index, interval)
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            continue                                   # a browser tab just looked at it
        if cached and _candle_already_seen(cached, INTERVAL_MINUTES[interval], now):
            continue                                   # nothing new can have happened since the last candle closed
        try:
            _fresh_payload(source, index, interval)
            checked += 1
        except Exception as error:
            print(f"[alerts] {index} {interval}: {error}")
    return checked


def _alert_loop() -> None:
    with data_sources.background_requests():              # this thread's Angel One calls wait for page requests
        while True:
            try:
                _alert_tick()
            except Exception as error:
                print(f"[alerts] problem: {error}")
            time.sleep(ALERT_POLL_SECONDS)


def start_alert_loop() -> threading.Thread:
    thread = threading.Thread(target=_alert_loop, name="alert-loop", daemon=True)
    thread.start()
    return thread


def _alert_banner() -> str:
    if not tg.is_configured():
        return ("Telegram alerts: OFF (add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to a .env file; "
                "see .env.example)")
    watching = ", ".join(f"{i.upper()} {t}" for i, t in _alert_markets())
    source = os.environ.get("ALERT_SOURCE", default_source()).strip().lower()
    text = f"Telegram alerts: ON (watching {watching}; data: {source})"
    if source == "yahoo" and tg.allow_delayed():
        text += ("\n  Note: free Yahoo data is about 15 minutes late, so each alert says DELAYED DATA. "
                 "Use Angel One for live data, or set TELEGRAM_ALLOW_DELAYED=0 to skip alerts on old data.")
    elif source == "yahoo":
        text += ("\n  Note: free Yahoo data is about 15 minutes late and TELEGRAM_ALLOW_DELAYED=0, "
                 "so NO alert will be sent. Remove that setting to get alerts.")
    return text


PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Candle Signal Dashboard - NIFTY / SENSEX</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root, :root[data-theme="dark"] {
    --bg: #0b0f14; --surface: #131920; --surface2: #1a222c; --line: #263140; --sel: #243241;
    --text: #e8eef4; --muted: #8794a3; --soft: #9aa8b6; --blue: #4da3ff; --focus: #7ee8d6;
    --green: #2bd4b4; --red: #ff7a70; --amber: #f3b94d;
    --green-bg: #0f2b29; --red-bg: #341a1d; --amber-bg: #33280f;
    --green-bd: #1d5a52; --red-bd: #6b2d33; --amber-bd: #5c4716;
    --blue-bg: rgba(77,163,255,.13); --shadow: none;
    color-scheme: dark;
    --sans: Inter, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Consolas, monospace;
  }
  :root[data-theme="light"] {
    --bg: #f3f5f9; --surface: #ffffff; --surface2: #f0f3f8; --line: #dde2ec; --sel: #e0e7f2;
    --text: #18202e; --muted: #5d6879; --soft: #4a5568; --blue: #1a6fd1; --focus: #0d8f7d;
    --green: #0d8f7d; --red: #d03a37; --amber: #a86400;
    --green-bg: #e3f4f0; --red-bg: #fbe9e8; --amber-bg: #fbf0dc;
    --green-bd: #8fcfc4; --red-bd: #efb3b1; --amber-bd: #e3c48a;
    --blue-bg: rgba(26,111,209,.09); --shadow: 0 1px 2px rgba(20,30,50,.08);
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text); line-height: 1.35; font-family: var(--sans); font-size: 14px; }
  button, select { font: inherit; color: inherit; }
  button { cursor: pointer; }
  button:hover { filter: brightness(1.15); }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .up { color: var(--green); } .down { color: var(--red); } .muted { color: var(--muted); }
  .lvl-buy { color: var(--blue); }
  .small { font-size: 12px; }
  .wrap { max-width: 1480px; margin: 0 auto; padding: 0 16px; }

  /* ---------- top bar ---------- */
  .topbar { background: var(--bg); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 20; }
  .topbar .wrap { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; padding-top: 8px; padding-bottom: 8px; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo { width: 28px; height: 28px; border-radius: 8px; background: var(--surface2); border: 1px solid var(--line);
          display: flex; align-items: center; justify-content: center; }
  .brand h1 { margin: 0; display: flex; flex-direction: column; line-height: 1.15; font-size: 15px; font-weight: 700; letter-spacing: -.01em; white-space: nowrap; }
  .brand h1 small { font-size: 11px; font-weight: 400; color: var(--muted); letter-spacing: 0; }
  .status { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; flex: 1 1 auto; min-width: 0; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: center; margin-left: auto; }
  .seg { display: inline-flex; background: var(--surface2); border: 1px solid var(--line); border-radius: 9px; padding: 2px; gap: 2px; }
  .seg button { background: transparent; border: 0; border-radius: 7px; padding: 5px 11px; min-height: 32px; font-size: 12.5px; color: var(--soft); font-weight: 600; }
  .seg button[aria-pressed="true"] { background: var(--sel); color: var(--text); }
  .field { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); }
  select { background: var(--surface2); border: 1px solid var(--line); border-radius: 10px; padding: 0 10px; min-height: 36px; font-size: 13px; }
  .btn { background: var(--surface2); border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; min-height: 36px;
         font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
  .btn.icon { padding: 0; width: 36px; justify-content: center; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 500;
          background: var(--surface2); border: 1px solid var(--line); white-space: nowrap; }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
  .pill.open .dot { background: var(--green); box-shadow: 0 0 0 3px var(--green-bg); }
  .pill.warn { background: var(--amber-bg); border-color: var(--amber-bd); color: var(--amber); }
  .pill.warn .dot { background: var(--amber); }
  .pill.ok { color: var(--green); }
  .pill.ok .dot { background: var(--green); }
  #updated { color: var(--soft); }

  #error { display: none; margin: 12px 0 0; background: var(--red-bg); border: 1px solid var(--red-bd);
           border-radius: 10px; padding: 8px 12px; align-items: center; gap: 12px; }
  #error.show { display: flex; }
  #error span { flex: 1; }
  #error button { background: transparent; border: 0; font-size: 18px; color: var(--muted); }

  main.page { padding: 12px 0 0; display: flex; flex-direction: column; gap: 12px; }

  /* ---------- decision banner: answer on the left, the 4 steps on the right ---------- */
  .decision { border-radius: 12px; border: 1px solid var(--line); background: var(--surface); overflow: hidden; }
  .dmain { display: flex; flex-wrap: wrap; gap: 14px 20px; padding: 14px 16px; align-items: center; }
  .dleft { flex: 1 1 380px; min-width: 0; display: flex; gap: 14px; align-items: flex-start; }
  .dicon { flex: none; width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
           background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber-bd); }
  .decision.ce .dicon { background: var(--green-bg); color: var(--green); border-color: var(--green-bd); }
  .decision.pe .dicon { background: var(--red-bg); color: var(--red); border-color: var(--red-bd); }
  .dtext { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .kickrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
  .decision .kicker { font-size: 10.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .decision h2 { margin: 0; font-size: 26px; line-height: 1.1; font-weight: 700; letter-spacing: -.025em; }
  .decision.ce h2 { color: var(--green); } .decision.pe h2 { color: var(--red); }
  .decision .sub { color: var(--soft); font-size: 13px; line-height: 1.4; max-width: 70ch; }
  .tag { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .03em;
         background: var(--amber-bg); color: var(--amber); }
  .dright { flex: 2 1 560px; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .dstrip { border-top: 1px solid var(--line); background: var(--surface2); padding: 7px 16px; font-size: 12px; line-height: 1.4; color: var(--soft); }
  .dstrip:empty { display: none; }
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
  .tile { background: var(--surface2); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; }
  .tile .lbl { font-size: 10.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .tile .val { font-size: 18px; font-weight: 700; font-family: var(--mono); letter-spacing: -.02em; line-height: 1.2; }
  .tile .note { font-size: 11px; color: var(--muted); }
  .tile.buy .val { color: var(--blue); } .tile.stop .val { color: var(--red); } .tile.target .val { color: var(--green); }
  .tiles-wrap .what { font-size: 12px; color: var(--soft); margin-bottom: 6px; }
  .tiles-wrap .what b { color: var(--text); }
  .stepcap { font-size: 12px; color: var(--muted); }
  .stepcap:empty { display: none; }

  /* the four steps */
  .steps { display: flex; flex-wrap: wrap; gap: 10px; }
  .step { flex: 1 1 170px; min-width: 0; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; font-size: 12px;
          background: var(--surface2); display: flex; flex-direction: column; gap: 5px; }
  .step .st { font-weight: 600; display: flex; justify-content: space-between; align-items: center; gap: 8px; color: var(--soft); }
  .step .sr { display: inline-flex; align-items: center; gap: 4px; font-weight: 700; }
  .step .sd { font-size: 12.5px; line-height: 1.35; color: var(--text); display: -webkit-box; -webkit-line-clamp: 3;
              -webkit-box-orient: vertical; overflow: hidden; }
  .step.ok { border-color: var(--green-bd); background: var(--green-bg); } .step.ok .sr { color: var(--green); }
  .step.fail { border-color: var(--red-bd); background: var(--red-bg); } .step.fail .sr { color: var(--red); }
  .step.wait { opacity: .6; } .step.wait .sr { color: var(--muted); }

  /* ---------- layout: three tiers ---------- */
  .tier { display: flex; flex-wrap: wrap; gap: 12px; align-items: stretch; }
  .tier.top { align-items: flex-start; }
  .col { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .t1main { flex: 999 1 600px; min-width: 0; display: flex; flex-direction: column; }
  .t1side { flex: 1 1 340px; }
  .t2a { flex: 1 1 300px; } .t2b { flex: 1 1 380px; } .t2c { flex: 1 1 300px; }
  .t3a { flex: 1 1 480px; } .t3b { flex: 1 1 480px; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px; box-shadow: var(--shadow); min-width: 0; }
  .card h3 { margin: 0 0 8px; font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .card h3 .hint { text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 12px; margin-left: 6px; }
  .row { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; border-top: 1px solid var(--line); }
  .row:first-of-type { border-top: 0; }
  .row .k { color: var(--muted); } .row .v { font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; }

  /* chart card */
  .chartcard { flex: 1; display: flex; flex-direction: column; }
  .chart-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px 20px; margin-bottom: 6px; }
  .ch-left { display: flex; flex-direction: column; }
  #chartTitle { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; font-weight: 600; color: var(--muted); }
  .pricebox { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }
  .price { font-size: 26px; font-weight: 700; font-family: var(--mono); letter-spacing: -.03em; line-height: 1.15; }
  .change { font-size: 14px; font-weight: 600; font-family: var(--mono); }
  .legend { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; font-size: 12.5px; color: var(--soft); }
  .legend b { font-size: 12px; }
  .chartopts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 0 0 8px; }
  .chartopts .chip { min-height: 30px; padding: 0 10px; font-size: 12px; display: inline-flex; align-items: center; }
  .chartopts .gap { flex: 1 1 8px; }
  .chartopts .brk { flex: 0 0 100%; height: 0; }
  .chartopts .zl { font-size: 11px; color: var(--muted); letter-spacing: .08em; text-transform: uppercase; font-weight: 600; margin-right: 2px; }
  .lg { display: inline-block; width: 14px; height: 3px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }
  #chart { height: clamp(300px, calc(100vh - 470px), 620px); border-radius: 6px; overflow: hidden; }
  .subpanel { margin-top: 6px; }
  .subpanel .subtitle { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; font-weight: 600; color: var(--muted); padding: 2px 0 2px 4px; }
  .subchart { height: 96px; border-radius: 6px; overflow: hidden; }
  .subpanel .nodata { font-size: 12px; color: var(--muted); padding: 6px 4px; }
  .ftiles { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
  .ftile { flex: 1 1 90px; min-width: 0; background: var(--surface2); border-radius: 12px; padding: 8px 10px; display: flex; flex-direction: column; gap: 1px; }
  .ftile .lbl { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; font-weight: 600; color: var(--muted); }
  .ftile .val { font-size: 17px; font-weight: 700; font-family: var(--mono); letter-spacing: -.02em; }
  .ftile .val.amber { color: var(--amber); }
  .ftile .note { font-size: 11px; color: var(--muted); }
  .ftiles:empty { display: none; }

  /* latest candle */
  .lcwrap { display: flex; gap: 16px; align-items: stretch; }
  #lcSvg { flex: none; background: var(--surface2); border-radius: 10px; }
  .ohlc { flex: 1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .ohlc > div { background: var(--surface2); border-radius: 8px; padding: 6px 10px; }
  .ohlc .lbl { font-size: 11px; color: var(--muted); }
  .ohlc .val { font-weight: 600; font-size: 14px; font-family: var(--mono); }
  .lctiles { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .lctiles > div { flex: 1 1 120px; background: var(--surface2); border-radius: 10px; padding: 10px 12px; }
  .lctiles .lbl { font-size: 12px; color: var(--muted); }
  .lctiles .val { font-size: 15px; font-weight: 700; }
  .pat { margin-top: 8px; padding: 10px 12px; border: 1px dashed var(--line); border-radius: 10px; font-size: 13.5px; color: var(--soft); }
  .pat + .pat { margin-top: 6px; }
  .pat .name { font-weight: 700; margin-right: 8px; color: var(--text); }

  .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .03em; white-space: nowrap; }
  .badge.CE { background: var(--green-bg); color: var(--green); }
  .badge.PE { background: var(--red-bg); color: var(--red); }
  .badge.WAIT { background: var(--surface2); color: var(--muted); }

  /* checklist */
  .pips { display: flex; align-items: center; gap: 4px; margin-bottom: 8px; }
  .pip { width: 30px; height: 6px; border-radius: 3px; background: var(--sel); }
  .pip.on { background: var(--green); }
  .pips span { margin-left: 6px; font-size: 13px; color: var(--soft); }
  .group { font-size: 10.5px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin: 10px 0 2px; }
  .check { display: flex; gap: 12px; padding: 6px 0; align-items: flex-start; border-top: 1px solid var(--line); font-size: 12.5px; line-height: 1.35; }
  .check .ic { flex: none; width: 18px; height: 18px; border-radius: 50%; display: inline-flex; align-items: center;
               justify-content: center; font-size: 10px; font-weight: 800; }
  .check.ok .ic { background: var(--green-bg); color: var(--green); }
  .check.no .ic { background: var(--red-bg); color: var(--red); }

  /* option details */
  .badge-src { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 6px; }
  .badge-src.live { background: var(--green-bg); color: var(--green); }
  .badge-src.est { background: var(--amber-bg); color: var(--amber); }
  .box { margin-top: 6px; padding: 6px 10px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface2); font-size: 12.5px; }
  .box.warn { border-color: var(--amber-bd); background: var(--amber-bg); }
  .box.agrees { border-color: var(--green-bd); } .box.warns { border-color: var(--red-bd); }
  details { margin-top: 8px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface2); }
  details summary { cursor: pointer; padding: 8px 12px; font-weight: 600; list-style: none; min-height: 36px; display: flex; align-items: center; }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "\25B8"; display: inline-block; margin-right: 8px; transition: transform .15s; color: var(--muted); }
  details[open] summary::before { transform: rotate(90deg); }
  details .inner { padding: 0 14px 12px; font-size: 12.5px; line-height: 1.6; color: var(--soft); }
  details ol { margin: 0; padding-left: 18px; } details li { padding: 2px 0; }

  /* scoreboard */
  .stats { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  .stat { flex: 1 1 90px; min-width: 0; background: var(--surface2); border-radius: 12px; padding: 8px 10px; display: flex; flex-direction: column-reverse; gap: 1px; }
  .stat .n { font-size: 17px; font-weight: 700; font-family: var(--mono); letter-spacing: -.02em; }
  .stat .l { font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; font-weight: 600; color: var(--muted); }

  .list .item { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 5px 0; border-top: 1px solid var(--line); font-size: 12.5px; }
  .list .item:first-child { border-top: 0; }
  .list .t { color: var(--muted); font-size: 12px; font-family: var(--mono); }
  .list .what { flex: 1; min-width: 0; }
  #recent { max-height: 220px; overflow-y: auto; }
  #rejected { max-height: 360px; overflow-y: auto; }
  #rejected .item { display: block; padding: 6px 0; }
  #rejected .item:first-child { border-top: 1px solid var(--line); }
  #rejected .why { font-size: 12.5px; color: var(--soft); line-height: 1.4; }
  #rejected .t { margin-right: 6px; }

  /* history */
  .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; }
  .tab { background: transparent; border: 1px solid var(--line); border-radius: 999px; padding: 0 14px; min-height: 36px; font-size: 13px; font-weight: 600; color: var(--soft); }
  .tab[aria-selected="true"] { color: var(--text); background: var(--sel); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
  .chip { background: transparent; border: 1px solid var(--line); border-radius: 999px; padding: 0 12px; min-height: 34px; font-size: 13px; color: var(--soft); font-weight: 600; }
  .chip[aria-pressed="true"] { color: var(--text); background: var(--sel); }
  .sumline { display: flex; flex-wrap: wrap; gap: 6px 22px; font-size: 13px; color: var(--soft); margin-bottom: 8px; }
  .sumline b { font-family: var(--mono); color: var(--text); }
  .sumline b.up { color: var(--green); } .sumline b.down { color: var(--red); }
  .hrow { display: grid; grid-template-columns: 120px minmax(0, 1fr) 100px 80px; gap: 8px; align-items: center;
          padding: 7px 4px; border-top: 1px solid var(--line); cursor: pointer; border-radius: 6px; }
  .hrow:hover { background: var(--surface2); }
  .hrow .when { font-weight: 600; font-family: var(--mono); font-size: 14px; } .hrow .when small { margin-left: 6px; font-weight: 400; font-size: 12px; font-family: var(--sans); color: var(--muted); }
  .hrow .r { text-align: right; font-weight: 700; font-family: var(--mono); }
  .res { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; letter-spacing: .03em; text-align: center;
         background: var(--surface2); color: var(--muted); }
  .res.SUCCESS { background: var(--green-bg); color: var(--green); }
  .res.FAILED { background: var(--red-bg); color: var(--red); }
  .res.TIME { background: var(--amber-bg); color: var(--amber); }
  .hdetail { padding: 10px; background: var(--surface2); border: 1px solid var(--line); border-radius: 10px; margin: 2px 0 8px;
             display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  .hchart { height: 220px; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  .learn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-top: 8px; }
  .learn-grid .card { box-shadow: none; background: var(--surface2); }

  /* option chain (open interest) */
  .oi-sub { font-size: 12px; color: var(--muted); margin: 2px 0 8px; }
  .oi-row { display: grid; grid-template-columns: minmax(0, 1fr) 54px minmax(0, 1fr); gap: 6px; align-items: center;
            padding: 1px 4px; border-radius: 6px; font-family: var(--mono); }
  .oi-row.near { background: var(--surface2); }
  .oi-row .stk { text-align: center; font-size: 12px; font-weight: 700; padding: 2px 0; border-radius: 6px; }
  .oi-row.near .stk { background: var(--text); color: var(--bg); }
  .oi-head { font-family: var(--sans); font-size: 11px; font-weight: 600; color: var(--muted); padding-bottom: 6px; }
  .oi-head .ce { text-align: right; color: var(--red); } .oi-head .pe { color: var(--green); } .oi-head .stk { color: var(--muted); }
  .oi-cell { display: flex; align-items: center; gap: 6px; min-width: 0; white-space: nowrap; }
  .oi-cell.ce { justify-content: flex-end; }
  .oi-v { font-size: 11.5px; min-width: 38px; }
  .oi-cell.ce .oi-v { text-align: right; }
  .oi-chg { font-size: 10.5px; color: var(--muted); }
  .oi-bw { flex: none; width: 62px; height: 8px; display: flex; }
  .oi-cell.ce .oi-bw { justify-content: flex-end; }
  .oi-bar { min-width: 2px; height: 8px; }
  .oi-bar.ce { background: var(--red); border-radius: 3px 0 0 3px; }
  .oi-bar.pe { background: var(--green); border-radius: 0 3px 3px 0; }
  .oi-facts { margin: 10px 0 0; padding: 10px 0 0 16px; border-top: 1px solid var(--line); font-size: 12px; line-height: 1.4; color: var(--soft); }
  .oi-facts li { margin: 0 0 2px; }

  #riskLine:empty { display: none; }
  #riskLine { margin-bottom: 10px; font-size: 12.5px; color: var(--soft); }
  #riskLine .box { margin-top: 6px; }
  .guide { margin-top: 0; background: var(--surface); border-radius: 12px; }
  footer { margin: 0; padding: 0 0 20px; color: var(--muted); font-size: 12.5px; line-height: 1.5; }

  /* ---------- small screens ---------- */
  @media (max-width: 1000px) {
    .hdetail { grid-template-columns: 1fr; }
  }
  @media (max-width: 640px) {
    .topbar { position: static; }
    .controls { margin-left: 0; }
    .decision h2 { font-size: 22px; }
    .tiles { grid-template-columns: 1fr; }
    .tile { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
    .hrow { grid-template-columns: 1fr auto; row-gap: 2px; }
    .hrow .r { text-align: left; }
    #chart { height: clamp(280px, 48vh, 460px); }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>

<header class="topbar">
  <div class="wrap">
    <div class="brand">
      <span class="logo"><svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><line x1="5" y1="2" x2="5" y2="16" stroke="#2bd4b4" stroke-width="1.6"/><rect x="3" y="5" width="4" height="7" fill="#2bd4b4"/><line x1="13" y1="1" x2="13" y2="14" stroke="#ff7a70" stroke-width="1.6"/><rect x="11" y="3" width="4" height="7" fill="#ff7a70"/></svg></span>
      <h1>Candle Signal<small>Dashboard</small></h1>
    </div>
    <div class="status" aria-live="polite">
      <span class="pill" id="pMarket"><span class="dot"></span><span id="pMarketText">Connecting...</span></span>
      <span class="pill" id="pData" style="display:none"><span class="dot"></span><span id="pDataText"></span></span>
      <span class="small" id="updated">Loading...</span>
    </div>
    <div class="controls">
      <div class="seg" id="indexSeg" role="group" aria-label="Market">
        <button type="button" data-v="nifty">NIFTY 50</button>
        <button type="button" data-v="sensex">SENSEX</button>
      </div>
      <div class="seg" id="intervalSeg" role="group" aria-label="Candle size">
        <button type="button" data-v="1m">1 min</button>
        <button type="button" data-v="5m">5 min</button>
        <button type="button" data-v="15m">15 min</button>
      </div>
      <label class="field">Data
        <select id="source" aria-label="Data source">
          <option value="angel">Angel One (free)</option>
          <option value="yahoo">Yahoo (free)</option>
        </select>
      </label>
      <button class="btn" id="refresh" type="button"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>Refresh</button>
      <button class="btn icon" id="themeBtn" type="button" aria-label="Switch light or dark theme" title="Switch light / dark">&#x263E;</button>
    </div>
  </div>
</header>

<div class="wrap">
  <div id="error" role="alert"><span id="errorText"></span><button type="button" id="errorClose" aria-label="Dismiss">&times;</button></div>

<main class="page">

  <!-- THE ANSWER + the four steps -->
  <section class="decision none" id="decision" aria-live="polite" aria-label="Trade decision">
    <div class="dmain">
      <div class="dleft">
        <span class="dicon" id="dIcon" aria-hidden="true"></span>
        <div class="dtext">
          <div class="kickrow">
            <div class="kicker" id="dKicker">Trade idea</div>
            <div class="tag" id="dTag" style="display:none"></div>
          </div>
          <h2 id="dTitle">Loading latest candles...</h2>
          <div class="sub" id="dSub">Please wait a moment.</div>
        </div>
      </div>
      <div class="dright">
        <div class="steps" id="anSteps"></div>
        <div class="stepcap"><span id="stepNote"></span> <span id="anNext"></span></div>
        <div class="tiles-wrap" id="dRight"></div>
      </div>
    </div>
    <div class="dstrip" id="dStrip"></div>
  </section>

  <!-- TIER 1: decide -->
  <div class="tier">
    <div class="t1main">
      <section class="card chartcard" aria-label="Price chart">
        <div class="chart-head">
          <div class="ch-left">
            <span id="chartTitle">NIFTY 50 &middot; 5 min</span>
            <span class="pricebox"><span class="price" id="price">--</span><span class="change" id="change"></span></span>
          </div>
          <div class="legend">
            <span><b class="up">&#x25B2;</b> leans CE (up)</span>
            <span><b class="down">&#x25BC;</b> leans PE (down)</span>
            <span><b>&bull;</b> wait</span>
            <button class="btn" id="resetView" type="button">Reset view</button>
          </div>
        </div>
        <div class="chartopts" id="chartOpts" role="group" aria-label="Chart options"></div>
        <div id="chart"></div>
        <div id="subPanels"></div>
        <div class="ftiles" id="factTiles"></div>
      </section>
    </div>
    <div class="col t1side">
      <section class="card" id="cardWhy">
        <h3>Why this decision</h3>
        <div id="why" class="muted">--</div>
      </section>
      <section class="card" id="cardTrade" style="display:none">
        <h3>Trade details</h3>
        <div id="trade"></div>
      </section>
    </div>
  </div>

  <!-- TIER 2: context -->
  <div class="tier top">
    <div class="col t2a">
      <section class="card" aria-label="Latest finished candle">
        <h3>Latest finished candle <span class="hint" id="lcTime"></span></h3>
        <div class="lcwrap">
          <svg id="lcSvg" width="44" height="104" viewBox="0 0 44 104" role="img" aria-label="Latest candle shape"></svg>
          <div class="ohlc">
            <div><div class="lbl">Open</div><div class="val" id="lcO">--</div></div>
            <div><div class="lbl">High</div><div class="val" id="lcH">--</div></div>
            <div><div class="lbl">Low</div><div class="val" id="lcL">--</div></div>
            <div><div class="lbl">Close</div><div class="val" id="lcC">--</div></div>
          </div>
        </div>
        <div class="lctiles">
          <div><div class="lbl">Colour</div><div class="val" id="lcColour">--</div></div>
          <div><div class="lbl">Trend before</div><div class="val" id="lcTrend">--</div></div>
        </div>
        <div id="patterns"></div>
      </section>
      <section class="card" aria-label="Recent candle patterns">
        <h3>Recent candle patterns</h3>
        <div id="recent" class="list muted">--</div>
      </section>
    </div>
    <div class="col t2b">
      <section class="card" id="cardChain" aria-label="Option chain">
        <h3>Option chain (open interest) <span class="hint">&middot; context only</span></h3>
        <div class="oi-sub" id="oiMeta"></div>
        <div id="oiBody" class="muted">--</div>
      </section>
    </div>
    <div class="col t2c">
      <section class="card" aria-label="Paper-trade scoreboard">
        <h3>Paper-trade scoreboard <span class="hint">automatic, no real money</span></h3>
        <div id="riskLine"></div>
        <div id="journal" class="muted">--</div>
      </section>
    </div>
  </div>

  <!-- TIER 3: track record and logs -->
  <div class="tier top">
    <div class="col t3a">
      <section class="card" aria-label="Idea history">
        <div class="tabs" role="tablist">
          <button class="tab" role="tab" id="tabBtnHistory" aria-selected="true" type="button">Idea history</button>
          <button class="tab" role="tab" id="tabBtnLearned" aria-selected="false" type="button">What the tool learned</button>
        </div>
        <div id="tabHistory">
          <div id="histSummary" class="sumline muted">Loading...</div>
          <div class="chips" id="histChips" role="group" aria-label="Filter ideas">
            <button class="chip" data-f="ALL" type="button">All</button>
            <button class="chip" data-f="SUCCESS" type="button">Success</button>
            <button class="chip" data-f="FAILED" type="button">Failed</button>
            <button class="chip" data-f="TIME EXIT" type="button">Time exit</button>
            <button class="chip" data-f="WAITING" type="button">Waiting</button>
          </div>
          <div id="histList"></div>
        </div>
        <div id="tabLearned" style="display:none">
          <div id="learned" class="muted">Loading...</div>
        </div>
      </section>
    </div>
    <div class="col t3b">
      <section class="card" id="cardRejected" aria-label="Recent setups with no idea">
        <h3>Recent setups with no idea <span class="hint">a pattern appeared, but a rule said no</span></h3>
        <div id="rejected" class="list muted">--</div>
      </section>
    </div>
  </div>

  <details class="guide">
    <summary>Quick guide: how to read this page</summary>
    <div class="inner">
      <ul>
        <li><b>The big banner</b> is the answer. &ldquo;No trade right now&rdquo; is normal and most of the day it is correct. Waiting is a decision.</li>
        <li><b>CE</b> means a call option (you gain if the market goes up). <b>PE</b> means a put option (you gain if it goes down).</li>
        <li><b>Why this decision</b> lists every check. A red &#x2718; on a required check means no trade.</li>
        <li><b>Stop loss</b> is the price where you accept a small loss and leave. <b>Target</b> is where you take profit. Decide both BEFORE you buy.</li>
        <li><b>Estimate</b> option prices come from India VIX and can be 10 to 20% off. Always check the real price in your broker app first.</li>
        <li>The page refreshes by itself every 15 seconds. Everything here is for learning and paper trading.</li>
      </ul>
    </div>
  </details>

  <footer>
    The last candle on the chart may still be running; analysis uses only finished candles. Candle patterns alone are not a trade signal.
    Personal use only. Not financial advice.
  </footer>
</main>
</div>

<script>
const REFRESH_MS = 15000;
const $ = (id) => document.getElementById(id);
const store = {
  get(k, dv) { try { const v = localStorage.getItem(k); return v === null ? dv : v; } catch (e) { return dv; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } },
};
function h(tag, cls, text, kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== null && text !== undefined) e.textContent = text;
  (kids || []).forEach((k) => { if (k) e.appendChild(k); });
  return e;
}
const fmt = (n) => Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const rs = (n) => '\u20B9' + fmt(n);
const rUnit = (v) => (v === null || v === undefined) ? '' : (v >= 0 ? '+' : '') + v.toFixed(2) + 'R';
const clear = (node) => { node.textContent = ''; return node; };

// ---------------- state (remembered between visits) ----------------
const OK = { index: ['nifty', 'sensex'], interval: ['1m', '5m', '15m'], source: ['yahoo', 'angel'] };
const state = {
  index: store.get('cs.index', 'nifty'), interval: store.get('cs.interval', '5m'), source: store.get('cs.source', 'angel'),
};
Object.keys(OK).forEach((k) => { if (OK[k].indexOf(state[k]) < 0) state[k] = OK[k][k === 'interval' ? 1 : 0]; });
const NAMES = { nifty: 'NIFTY 50', sensex: 'SENSEX' };

// ---------------- theme + charts ----------------
const THEMES = {
  dark:  { bg: '#131920', text: '#9aa8b6', grid: '#1a222c', border: '#263140', up: '#2bd4b4', down: '#ff7a70' },
  light: { bg: '#ffffff', text: '#2b3445', grid: '#eceff5', border: '#d5dbe7', up: '#0d8f7d', down: '#d03a37' },
};
let theme = store.get('cs.theme', 'dark') === 'light' ? 'light' : 'dark';
const chartOptions = (c) => ({
  layout: { background: { color: c.bg }, textColor: c.text },
  grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: c.border },
  rightPriceScale: { borderColor: c.border, minimumWidth: 72 },
});
const seriesOptions = (c) => ({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down });

const chart = LightweightCharts.createChart($('chart'), Object.assign({ autoSize: true, crosshair: { mode: 0 } }, chartOptions(THEMES[theme])));
// shaded bands behind the candles: where the rules say no (time of day, expiry-day afternoon)
const zoneSeries = chart.addHistogramSeries({ priceScaleId: 'zones', priceLineVisible: false, lastValueVisible: false,
  crosshairMarkerVisible: false, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }) });
chart.priceScale('zones').applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } });
const series = chart.addCandlestickSeries(seriesOptions(THEMES[theme]));
// indicator lines: VWAP (amber, dashed, one piece per day) and the EMA 9 / EMA 21 the rules use
const LINE_COLORS = { vwap: '#f3b94d', ema9: '#4da3ff', ema21: '#b48bff' };
const lineOpts = (color, width, style) => ({ color: color, lineWidth: width, lineStyle: style, priceLineVisible: false,
  lastValueVisible: false, crosshairMarkerVisible: false });
const ema9Series = chart.addLineSeries(lineOpts(LINE_COLORS.ema9, 1, 0));
const ema21Series = chart.addLineSeries(lineOpts(LINE_COLORS.ema21, 1, 0));
let vwapSeries = [];
// what is drawn on the chart (each can be switched on / off; remembered between visits)
const CHART_OPTS = [
  { key: 'vwap', label: 'VWAP', color: '#f3b94d', on: true },
  { key: 'ema9', label: 'EMA 9', color: '#4da3ff', on: true },
  { key: 'ema21', label: 'EMA 21', color: '#b48bff', on: true },
  { key: 'prev', label: 'Previous day', color: '#9aa8b6', on: false },
  { key: 'orange', label: 'Opening range', color: '#7ee8d6', on: false },
  { key: 'swing', label: 'Swing high / low', color: '#6d8bb0', on: false },
  { key: 'walls', label: 'OI walls', color: '#ff7a70', on: false },
  { key: 'arrows', label: 'Pattern arrows', color: '#2bd4b4', on: true },
  { key: 'signal', label: 'Signal candle', color: '#f3b94d', on: true },
  { key: 'ideas', label: 'Past ideas', color: '#9aa8b6', on: true },
  { key: 'zones', label: 'No-trade zones', color: '#8794a3', on: true },
  { key: 'dayopen', label: 'Day open', color: '#8794a3', on: false },
  { key: 'rr', label: 'Risk / reward zone', color: '#2bd4b4', on: true },
  { key: 'p_adx', label: 'ADX panel', color: '#b48bff', on: false },
  { key: 'p_vix', label: 'VIX panel', color: '#f3b94d', on: false },
  { key: 'p_volx', label: 'Volume panel', color: '#4da3ff', on: false },
  { key: 'p_oipcr', label: 'OI ratio panel', color: '#7ee8d6', on: false },
  { key: 'p_oiflow', label: 'OI build-up panel', color: '#ff7a70', on: false },
];
// small panels under the chart, lined up with it (one chart each, zoom follows the main chart)
const PANELS = [
  { key: 'p_adx', data: 'adx', title: 'ADX (the rules need 20 or more)', kind: 'line', color: '#b48bff', ref: 20, refTitle: '20' },
  { key: 'p_vix', data: 'vix', title: 'India VIX, previous close (the rules need 22 or less)', kind: 'line', color: '#f3b94d', ref: 22, refTitle: '22' },
  { key: 'p_volx', data: 'volx', title: 'Futures volume, times its recent average (optional check: 1 or more)', kind: 'hist', color: '#4da3ff', ref: 1, refTitle: '1x' },
  { key: 'p_oipcr', data: 'oi_pcr', title: 'Put/call OI ratio (saved OI days only)', kind: 'line', color: '#7ee8d6', ref: 1, refTitle: '1' },
  { key: 'p_oiflow', data: 'oi_flow', title: 'OI build-up %, puts minus calls over 30 min (saved OI days only)', kind: 'hist2', color: '#ff7a70', ref: 0, refTitle: '0' },
];
const subCharts = {};
let rrSeries = [];
const opts = {};
CHART_OPTS.forEach((o) => { opts[o.key] = store.get('cs.opt.' + o.key, o.on ? '1' : '0') === '1'; });
let lastData = null, levelLines = [];
function setIndicatorLines(l) {
  ema9Series.setData(l.ema9); ema21Series.setData(l.ema21);
  while (vwapSeries.length > l.vwap.length) chart.removeSeries(vwapSeries.pop());
  while (vwapSeries.length < l.vwap.length) vwapSeries.push(chart.addLineSeries(lineOpts(LINE_COLORS.vwap, 2, 2)));
  l.vwap.forEach((points, i) => vwapSeries[i].setData(points));
  applyChartOpts();
}
// marks for the idea shown now (signal + confirmation candle) and for the ideas saved in the journal
const chartTime = (iso) => Math.round(Date.parse(iso) / 1000) + 19800;
const IDEA_COLORS = { 'SUCCESS': '#2bd4b4', 'FAILED': '#ff7a70', 'TIME EXIT': '#f3b94d' };
function ideaMarkers() {
  const out = [];
  const p = lastData && lastData.plan;
  if (opts.signal && p && p.signal !== 'NONE' && p.signal_time) {
    out.push({ time: chartTime(p.signal_time), position: 'inBar', shape: 'square', color: '#f3b94d', size: 1, text: 'signal' });
    if (p.mode === 'confirm' && p.decision_time && p.decision_time !== p.signal_time) {
      out.push({ time: chartTime(p.decision_time), position: 'inBar', shape: 'circle', color: '#7ee8d6', size: 1, text: 'confirm' });
    }
  }
  if (opts.ideas && histData) {
    histData.items.forEach((it) => {
      if (it.index !== state.index.toUpperCase() || it.interval !== state.interval || it.label === 'SKIPPED') return;
      out.push({ time: chartTime(it.sort), position: it.side === 'CE' ? 'belowBar' : 'aboveBar', shape: 'square',
        color: IDEA_COLORS[it.label] || '#9aa8b6', size: 1,
        text: it.side + ' ' + (it.r_net === null ? '\u2026' : rUnit(it.r_net)) });
    });
  }
  return out;
}
function applyChartOpts() {
  ema9Series.applyOptions({ visible: opts.ema9 });
  ema21Series.applyOptions({ visible: opts.ema21 });
  vwapSeries.forEach((x) => x.applyOptions({ visible: opts.vwap }));
  const marks = (lastData && opts.arrows ? lastData.markers : []).concat(lastData ? ideaMarkers() : []);
  series.setMarkers(marks.sort((a, b) => a.time - b.time));
  levelLines.forEach((x) => series.removePriceLine(x));
  levelLines = [];
  drawZones();
  drawRiskReward();
  drawPanels();
  if (!lastData) return;
  const lv = lastData.levels || {}, ch = lastData.chain;
  const add = (price, color, title, style) => {
    if (price === null || price === undefined) return;
    levelLines.push(series.createPriceLine({ price: price, color: color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title: title }));
  };
  if (opts.dayopen && lv.day_open !== undefined && lv.day_open !== null) {
    const gap = (lv.gap_pct === null || lv.gap_pct === undefined) ? '' : ' (gap ' + (lv.gap_pct >= 0 ? '+' : '') + lv.gap_pct.toFixed(2) + '%)';
    add(lv.day_open, '#8794a3', 'Open' + gap, 1);
  }
  if (opts.prev) { add(lv.pdh, '#9aa8b6', 'PD high', 2); add(lv.pdl, '#9aa8b6', 'PD low', 2); add(lv.pdc, '#9aa8b6', 'PD close', 1); }
  if (opts.orange) { add(lv.or_high, '#7ee8d6', 'OR high', 2); add(lv.or_low, '#7ee8d6', 'OR low', 2); }
  if (opts.swing) { add(lv.swing_high, '#6d8bb0', 'Swing high', 1); add(lv.swing_low, '#6d8bb0', 'Swing low', 1); }
  if (opts.walls && ch) {
    if (ch.call_wall) add(ch.call_wall.strike, '#ff7a70', 'Call wall', 3);
    if (ch.put_wall) add(ch.put_wall.strike, '#2bd4b4', 'Put wall', 3);
  }
}
const ZONE_COLORS = { time: 'rgba(135,148,163,0.16)', expiry: 'rgba(255,122,112,0.16)' };
function drawZones() {
  const z = lastData && opts.zones ? lastData.zones || [] : [];
  zoneSeries.setData(z.map((p) => ({ time: p.time, value: 1, color: ZONE_COLORS[p.kind] || ZONE_COLORS.time })));
}
function drawRiskReward() {
  const p = lastData && lastData.plan;
  const on = opts.rr && p && p.signal !== 'NONE' && p.decision_time;
  if (!on) { rrSeries.forEach((x) => chart.removeSeries(x)); rrSeries = []; return; }
  const from = chartTime(p.decision_time), times = lastData.candles.map((c) => c.time).filter((t) => t >= from);
  const make = (value, rgba) => {
    const x = chart.addBaselineSeries({ baseValue: { type: 'price', price: p.entry }, lineWidth: 1,
      topLineColor: 'rgba(0,0,0,0)', bottomLineColor: 'rgba(0,0,0,0)', topFillColor1: rgba, topFillColor2: rgba,
      bottomFillColor1: rgba, bottomFillColor2: rgba, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    x.setData(times.map((t) => ({ time: t, value: value })));
    return x;
  };
  rrSeries.forEach((x) => chart.removeSeries(x));
  rrSeries = [make(p.target, 'rgba(43,212,180,0.20)'), make(p.stop, 'rgba(255,122,112,0.20)')];
}
function panelData(def) {
  const m = new Map(((lastData.panels || {})[def.data] || []).map((x) => [x.time, x.value]));
  return lastData.candles.map((c) => {
    if (!m.has(c.time)) return { time: c.time };
    const v = m.get(c.time);
    const out = { time: c.time, value: v };
    if (def.kind === 'hist') out.color = v >= def.ref ? 'rgba(77,163,255,0.75)' : 'rgba(135,148,163,0.6)';
    if (def.kind === 'hist2') out.color = v >= 0 ? 'rgba(43,212,180,0.75)' : 'rgba(255,122,112,0.75)';
    return out;
  });
}
function drawPanels() {
  const host = $('subPanels');
  PANELS.forEach((def) => {
    const entry = subCharts[def.key];
    if (!opts[def.key] || !lastData) {
      if (entry) { entry.chart.remove(); entry.box.remove(); delete subCharts[def.key]; }
      return;
    }
    const data = panelData(def), has = data.some((x) => x.value !== undefined);
    let e = entry;
    if (!e) {
      const box = h('div', 'subpanel', null, [h('div', 'subtitle', def.title), h('div', 'subchart')]);
      host.appendChild(box);
      const c = LightweightCharts.createChart(box.lastChild, Object.assign({ autoSize: true, handleScroll: false, handleScale: false,
        crosshair: { mode: 0 } }, chartOptions(THEMES[theme])));
      c.applyOptions({ timeScale: { visible: false } });
      const ser = def.kind === 'line'
        ? c.addLineSeries({ color: def.color, lineWidth: 2, priceLineVisible: false, lastValueVisible: true })
        : c.addHistogramSeries({ color: def.color, priceLineVisible: false, lastValueVisible: false });
      ser.createPriceLine({ price: def.ref, color: '#8794a3', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: def.refTitle });
      e = subCharts[def.key] = { chart: c, series: ser, box: box };
      const range = chart.timeScale().getVisibleLogicalRange();
      if (range) c.timeScale().setVisibleLogicalRange(range);
    }
    e.series.setData(data);
    let note = e.box.querySelector('.nodata');
    if (!has && !note) { note = h('div', 'nodata', 'No data for the days on screen yet.'); e.box.appendChild(note); }
    if (has && note) note.remove();
  });
}
chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
  if (!range) return;
  Object.values(subCharts).forEach((e) => e.chart.timeScale().setVisibleLogicalRange(range));
});
function buildChartOpts() {
  const box = clear($('chartOpts'));
  CHART_OPTS.forEach((o) => {
    if (o.key === 'p_adx') { box.appendChild(h('span', 'brk')); box.appendChild(h('span', 'zl', 'Panels')); }
    const b = h('button', 'chip', null, [h('i', 'lg'), document.createTextNode(o.label)]);
    b.type = 'button';
    b.firstChild.style.background = o.color;
    b.setAttribute('aria-pressed', String(opts[o.key]));
    b.addEventListener('click', () => {
      opts[o.key] = !opts[o.key];
      store.set('cs.opt.' + o.key, opts[o.key] ? '1' : '0');
      b.setAttribute('aria-pressed', String(opts[o.key]));
      applyChartOpts();
    });
    box.appendChild(b);
  });
  box.appendChild(h('span', 'gap'));
  box.appendChild(h('span', 'zl', 'Zoom'));
  [['50', 50], ['100', 100], ['All', 0]].forEach(([label, n]) => {
    const b = h('button', 'chip', label);
    b.type = 'button';
    b.addEventListener('click', () => {
      if (n === 0) chart.timeScale().fitContent();
      else chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, lastN - n), to: lastN + 5 });
    });
    box.appendChild(b);
  });
}
let detailChart = null, detailSeries = null;

buildChartOpts();

function applyTheme(t) {
  theme = t;
  document.documentElement.setAttribute('data-theme', t);
  store.set('cs.theme', t);
  const c = THEMES[t];
  chart.applyOptions(chartOptions(c)); series.applyOptions(seriesOptions(c));
  Object.values(subCharts).forEach((e) => { e.chart.applyOptions(chartOptions(c)); e.chart.applyOptions({ timeScale: { visible: false } }); });
  if (detailChart) { detailChart.applyOptions(chartOptions(c)); detailSeries.applyOptions(seriesOptions(c)); }
  $('themeBtn').textContent = t === 'dark' ? '\u2600' : '\u263E';
  $('themeBtn').title = t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
}

let needFit = true, lastN = 0, planLines = [], lastJournalSig = null, nextAt = 0;

// ---------------- controls ----------------
function syncControls() {
  document.querySelectorAll('#indexSeg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === state.index)));
  document.querySelectorAll('#intervalSeg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === state.interval)));
  $('source').value = state.source;
  $('chartTitle').textContent = NAMES[state.index] + ' \u00B7 ' + state.interval.replace('m', ' min');
}
function setState(key, value) {
  if (state[key] === value) return;
  state[key] = value; store.set('cs.' + key, value);
  needFit = true; syncControls(); load(true);
}
document.querySelectorAll('#indexSeg button').forEach((b) => b.addEventListener('click', () => setState('index', b.dataset.v)));
document.querySelectorAll('#intervalSeg button').forEach((b) => b.addEventListener('click', () => setState('interval', b.dataset.v)));
$('source').addEventListener('change', (e) => setState('source', e.target.value));
$('refresh').addEventListener('click', () => load(true));
$('themeBtn').addEventListener('click', () => applyTheme(theme === 'dark' ? 'light' : 'dark'));
$('errorClose').addEventListener('click', () => $('error').classList.remove('show'));
$('resetView').addEventListener('click', () => { fitRecent(); });

function fitRecent() {
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, lastN - 80), to: lastN + 5 });
}
function showError(msg) { $('errorText').textContent = msg; $('error').classList.add('show'); }
function hideError() { $('error').classList.remove('show'); }

// ---------------- small builders ----------------
function badge(leans) { return h('span', 'badge ' + leans, leans === 'WAIT' ? 'WAIT' : 'leans ' + leans); }
function addLine(price, color, title) {
  planLines.push(series.createPriceLine({ price: price, color: color, lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: title }));
}
function kv(parent, k, v, cls) {
  parent.appendChild(h('div', 'row', null, [h('span', 'k', k), h('span', 'v' + (cls ? ' ' + cls : ''), v)]));
}
function checkItem(r) {
  return h('div', 'check ' + (r.ok ? 'ok' : 'no'), null, [h('span', 'ic', r.ok ? '\u2714' : '\u2718'), h('span', 'tx', r.text)]);
}
function tile(cls, label, value, note) {
  return h('div', 'tile ' + cls, null, [h('div', 'lbl', label), h('div', 'val', value), note ? h('div', 'note', note) : null]);
}

// ---------------- decision banner ----------------
const ICONS = {
  none: '<path d="M8 5v14M16 5v14"/>',
  ce: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  pe: '<path d="M12 5v14M5 12l7 7 7-7"/>',
};
function setIcon(kind) {
  $('dIcon').innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[kind] + '</svg>';
}
function renderDecision(d) {
  const p = d.plan;
  const box = $('decision');
  const right = clear($('dRight'));
  const tag = $('dTag');
  const strip = $('dStrip');
  let practice = d.delayed
    ? 'Practice only: data is about ' + d.data_lag_min + ' min old (free Yahoo is delayed). Use Angel One for live data.'
    : (!d.market_open ? 'Practice only: market is closed, this is the last session.' : '');
  if (d.risk && d.risk.reached) practice = (practice ? practice + ' ' : '') + 'Daily limit reached: new ideas are skipped, no alert.';

  if (p.signal === 'NONE') {
    box.className = 'decision none';
    setIcon('none');
    $('dKicker').textContent = 'Trade idea';
    $('dTitle').textContent = 'No trade right now';
    $('dSub').textContent = p.message || d.summary;
    strip.textContent = 'Most of the time there is no good setup, and that is normal. The filters are strict on purpose. ' +
      'Waiting is a decision. The checklist on the right shows what is missing.';
  } else {
    const isCE = p.signal === 'CE';
    box.className = 'decision ' + (isCE ? 'ce' : 'pe');
    setIcon(isCE ? 'ce' : 'pe');
    strip.textContent = '';
    $('dKicker').textContent = 'Trade idea \u00B7 ' + (isCE ? 'bullish' : 'bearish');
    $('dTitle').textContent = (isCE ? 'Buy CE (call)' : 'Buy PE (put)') + ' idea';
    $('dSub').textContent = p.status || 'A setup is confirmed.';
    const o = p.option_levels;
    const wrap = h('div');
    wrap.appendChild(h('div', 'what', null, [
      document.createTextNode(o ? 'What to buy: ' : 'Index levels for '),
      h('b', null, p.option + (o ? '  \u00B7 expiry ' + o.expiry : '')),
    ]));
    const tiles = h('div', 'tiles');
    if (o) {
      tiles.appendChild(tile('buy', 'Buy price', rs(o.entry_price), 'Now ' + rs(o.now_price)));
      tiles.appendChild(tile('stop', 'Stop loss', rs(o.stop_price), 'Loss ' + rs(o.loss_per_unit) + ' / unit'));
      tiles.appendChild(tile('target', 'Target', rs(o.target_price), 'Profit ' + rs(o.profit_per_unit) + ' / unit'));
    } else {
      tiles.appendChild(tile('buy', 'Index buy', fmt(p.entry), p.option + ' (ATM)'));
      tiles.appendChild(tile('stop', 'Index stop', fmt(p.stop), 'Risk ' + p.risk_points.toFixed(1) + ' pts'));
      tiles.appendChild(tile('target', 'Index target', fmt(p.target), 'Reward ' + p.reward_points.toFixed(1) + ' pts'));
    }
    wrap.appendChild(tiles);
    right.appendChild(wrap);
  }
  tag.style.display = practice ? '' : 'none';
  tag.textContent = practice;
}

// ---------------- live analysis (4 steps in the banner, facts under the chart, dropped setups) ----------------
function factTile(label, value, note, cls) {
  return h('div', 'ftile', null, [h('span', 'lbl', label), h('span', 'val' + (cls ? ' ' + cls : ''), value), note ? h('div', 'note', note) : null]);
}
function factFromText(f) {
  let m;
  if ((m = /^(.*?) ([\d,]+) \(price is (above|below)\)$/.exec(f))) return factTile(m[1], m[2], 'Price is ' + m[3], 'amber');
  if ((m = /^Opening gap ([+-][\d.]+%)$/.exec(f))) return factTile('Opening gap', m[1], 'vs previous close');
  if ((m = /^Volume ([\d.]+)x average$/.exec(f))) return factTile('Volume', m[1] + '\u00D7', 'of average');
  if (/^Volume not available/.test(f)) return factTile('Volume', '--', 'not available for this data');
  return factTile('Fact', f, '');
}
const STEP_MARK = {
  ok: ['Pass', '<path d="M4 12.5l5 5L20 6.5"/>'],
  fail: ['Fail', '<path d="M6 6l12 12M18 6L6 18"/>'],
  wait: ['Waiting', '<path d="M5 12h14"/>'],
};
function renderAnalysis(d) {
  const a = d.analysis, stepsBox = clear($('anSteps')), note = $('stepNote'), rej = clear($('rejected')), ft = clear($('factTiles'));
  if (!a) { note.textContent = 'Analysis is not available right now.'; rej.textContent = '--'; $('anNext').textContent = ''; return; }
  $('anNext').textContent = a.next_candle_closes ? '\u00B7 next candle closes ' + a.next_candle_closes : '';
  a.steps.forEach((s, n) => {
    const mark = STEP_MARK[s.state] || STEP_MARK.wait;
    const sr = h('span', 'sr');
    sr.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + mark[1] + '</svg>';
    sr.appendChild(document.createTextNode(mark[0]));
    const el = h('div', 'step ' + s.state, null, [
      h('div', 'st', null, [h('span', null, (n + 1) + '. ' + s.title), sr]),
      h('div', 'sd', s.detail),
    ]);
    el.title = s.detail;
    stepsBox.appendChild(el);
  });
  note.textContent = a.headline;
  (a.facts || []).forEach((f) => ft.appendChild(factFromText(f)));
  if (!a.rejected.length) { rej.textContent = 'None in the recent candles.'; return; }
  a.rejected.forEach((r) => {
    rej.appendChild(h('div', 'item', null, [
      h('div', null, null, [h('span', 't', r.time + '  '), h('b', null, r.setup)]),
      h('div', 'why', 'Dropped: ' + r.why),
    ]));
  });
}

// ---------------- option chain: open interest facts ----------------
function renderChain(d) {
  const c = d.chain, box = clear($('oiBody')), meta = $('oiMeta');
  meta.textContent = '';
  if (!c) { box.className = 'muted'; box.textContent = 'No option data yet. Run: python candle_store.py backfill-options'; return; }
  box.className = '';
  meta.textContent = 'Expiry ' + c.expiry + ' \u00B7 ' + (c.source === 'live' ? 'live ' + c.as_of : 'saved ' + c.as_of);
  const oi = (n) => n >= 1e5 ? (n / 1e5).toFixed(1) + 'L' : n >= 1e3 ? (n / 1e3).toFixed(0) + 'k' : String(Math.round(n));
  const sgn = (n) => (n > 0 ? '+' : n < 0 ? '-' : '') + oi(Math.abs(n));
  const ft = $('factTiles');
  ft.appendChild(factTile('Put/Call OI', c.pcr === null ? '--' : c.pcr.toFixed(2),
    c.pcr === null ? '' : (c.pcr > 1 ? 'more puts than calls' : 'more calls than puts')));
  if (c.call_wall) ft.appendChild(factTile('Call wall', String(c.call_wall.strike), 'most call OI', 'down'));
  if (c.put_wall) ft.appendChild(factTile('Put wall', String(c.put_wall.strike), 'most put OI', 'up'));
  const max = Math.max(1, ...c.rows.map((r) => Math.max(r.ce || 0, r.pe || 0)));
  let near = c.rows[0];
  c.rows.forEach((r) => { if (Math.abs(r.strike - c.spot) < Math.abs(near.strike - c.spot)) near = r; });
  box.appendChild(h('div', 'oi-row oi-head', null, [h('span', 'ce', 'Calls (CE)'), h('span', 'stk', 'Strike'), h('span', 'pe', 'Puts (PE)')]));
  c.rows.forEach((r) => {
    const cell = (side) => {
      const v = r[side], chg = r[side + '_chg'];
      const bar = h('span', 'oi-bar ' + side);
      bar.style.width = (v ? Math.max(5, v / max * 100) : 0) + '%';
      const val = h('span', 'oi-v', v === null ? '--' : oi(v));
      const delta = h('span', 'oi-chg', chg === null ? '' : sgn(chg));
      const bw = h('span', 'oi-bw', null, [bar]);
      return h('div', 'oi-cell ' + side, null, side === 'ce' ? [delta, val, bw] : [bw, val, delta]);
    };
    box.appendChild(h('div', 'oi-row' + (r === near ? ' near' : ''), null, [cell('ce'), h('div', 'stk', String(r.strike)), cell('pe')]));
  });
  box.appendChild(h('ul', 'oi-facts', null, c.facts.map((f) => h('li', null, f))));
}

// ---------------- why (checklist) ----------------
function renderWhy(d) {
  const p = d.plan, box = clear($('why'));
  box.className = '';
  if (!p.reasons || !p.reasons.length) {
    box.className = 'muted';
    box.textContent = 'No candle pattern to check yet. When one appears, every check will be listed here.';
    return;
  }
  const pips = h('div', 'pips');
  for (let i = 0; i < p.max_score; i++) pips.appendChild(h('span', 'pip' + (i < p.score ? ' on' : '')));
  pips.appendChild(h('span', null, 'Agreement ' + p.score + ' of ' + p.max_score + ' (needs 3)'));
  box.appendChild(pips);
  const req = p.reasons.filter((r) => r.required), bonus = p.reasons.filter((r) => !r.required);
  if (req.length) { box.appendChild(h('div', 'group', 'Must pass')); req.forEach((r) => box.appendChild(checkItem(r))); }
  if (bonus.length) { box.appendChild(h('div', 'group', 'Bonus points')); bonus.forEach((r) => box.appendChild(checkItem(r))); }
}

// ---------------- trade details ----------------
function renderTrade(d) {
  planLines.forEach((l) => series.removePriceLine(l));
  planLines = [];
  const p = d.plan, card = $('cardTrade');
  if (p.signal === 'NONE') { card.style.display = 'none'; return; }
  card.style.display = '';
  const box = clear($('trade'));
  const o = p.option_levels;

  if (o) {
    const head = h('div', 'row', null, [
      h('span', 'k', 'Option price source'),
      h('span', 'badge-src ' + (o.price_source === 'live' ? 'live' : 'est'), o.price_source === 'live' ? 'LIVE price' : 'ESTIMATE'),
    ]);
    box.appendChild(head);
    kv(box, 'Option', p.option + ' \u00B7 ' + o.expiry);
    // buy / stop loss / target prices, the price now and loss / profit per unit are in the banner tiles above
    box.appendChild(h('div', 'muted small', (o.price_source === 'live' ? 'Live option price used. ' : 'ESTIMATE only. The real price in Groww can differ. ') + o.note));
    if (o.warning) box.appendChild(h('div', 'box warn', o.warning));
  } else {
    kv(box, 'Option', p.option + ' (ATM)');
  }
  box.appendChild(h('div', 'group', 'On the index chart'));
  kv(box, 'Buy at', fmt(p.entry));
  kv(box, 'Stop loss at', fmt(p.stop), 'down');
  kv(box, 'Target at', fmt(p.target), 'up');
  kv(box, 'Risk : reward', p.risk_points.toFixed(1) + ' : ' + p.reward_points.toFixed(1) + ' points');

  if (p.sizing) {
    const z = p.sizing;
    box.appendChild(h('div', 'group', 'Position size (your risk settings)'));
    kv(box, 'Risk budget', rs(z.budget) + ' (' + z.risk_pct + '% of ' + rs(z.capital) + ')');
    kv(box, 'Lot size', String(z.lot_size));
    kv(box, 'Loss per lot at the stop', rs(z.loss_per_lot), 'down');
    kv(box, 'Lots that fit', String(z.lots), z.lots ? 'up' : 'down');
    box.appendChild(h('div', 'muted small', z.note + '. This is a helper only, no order is placed.'));
  }

  if (p.memory) {
    const icon = p.memory.verdict === 'warns' ? '\u26A0 ' : p.memory.verdict === 'agrees' ? '\u2714 ' : '\u2139 ';
    box.appendChild(h('div', 'box ' + p.memory.verdict, icon + 'Memory: ' + p.memory.text));
  }

  const steps = (p.mode === 'confirm') ? [
    'The signal is already confirmed, so the entry zone is NOW. If you want this trade, act within the next candle.',
    'First check the real option price in Groww. If it is very different from the estimate, do not trade.',
    'Buy, then place a stop-loss order at the STOP LOSS option price and a sell order at the TARGET option price.',
    'If you miss this candle, skip the idea. Do not chase.',
  ] : [
    'Buy this option only when the index reaches the BUY level. Not before.',
    'Check the real price in Groww first. If it is very different from the estimate, do not trade.',
    'After buying, place a stop-loss order at the STOP LOSS option price and a sell order at the TARGET option price.',
    'If the index does not reach the BUY level within the next 2 candles, skip this idea.',
  ];
  const ol = h('ol');
  steps.forEach((s) => ol.appendChild(h('li', null, s)));
  const det = h('details', null, null, [h('summary', null, 'How to place this trade (step by step)'), h('div', 'inner', null, [ol])]);
  det.open = false;      // collapsed by default to keep the page short
  box.appendChild(det);

  addLine(p.entry, '#4da3ff', 'ENTRY');
  addLine(p.stop, '#ff7a70', 'STOP LOSS');
  addLine(p.target, '#2bd4b4', 'TARGET');
}

// ---------------- daily risk limits (only shown when set in .env) ----------------
function renderRisk(d) {
  const box = clear($('riskLine')), r = d.risk;
  if (!r || !r.enabled) return;
  const parts = [];
  if (r.max_loss_r) parts.push('max loss ' + r.max_loss_r + 'R');
  if (r.max_ideas) parts.push('max ' + r.max_ideas + ' ideas');
  box.appendChild(h('div', null, null, [
    document.createTextNode('Today: ' + r.ideas + ' idea(s), '),
    h('b', r.net_r >= 0 ? 'up' : 'down', (r.net_r >= 0 ? '+' : '') + r.net_r.toFixed(2) + 'R'),
    document.createTextNode('  \u00B7 limits: ' + parts.join(', ')),
  ]));
  if (r.reached) box.appendChild(h('div', 'box warn', r.reached + '. New ideas are saved as skipped and no alert is sent.'));
}

// ---------------- scoreboard ----------------
function renderJournal(j) {
  const box = clear($('journal'));
  box.className = '';
  if (!j) { box.className = 'muted'; box.textContent = 'Scoreboard not available.'; return; }
  if (j.ideas === 0) {
    box.className = 'muted';
    box.textContent = 'No ideas saved yet. Ideas are saved automatically and checked later. See Idea history below.';
    return;
  }
  const stats = h('div', 'stats');
  const net = (j.total_r >= 0 ? '+' : '') + j.total_r.toFixed(2) + 'R';
  stats.appendChild(h('div', 'stat', null, [h('div', 'n', String(j.ideas)), h('div', 'l', 'Ideas')]));
  stats.appendChild(h('div', 'stat', null, [h('div', 'n', j.win_rate === null ? '--' : (j.win_rate * 100).toFixed(0) + '%'), h('div', 'l', 'Win rate')]));
  stats.appendChild(h('div', 'stat', null, [h('div', 'n ' + (j.total_r >= 0 ? 'up' : 'down'), net), h('div', 'l', 'Net result')]));
  box.appendChild(stats);
  box.appendChild(h('div', 'muted small', 'Target ' + j.targets + ' \u00B7 Stop loss ' + j.stops + ' \u00B7 Time exit ' + j.time_exits + ' \u00B7 Waiting ' + j.waiting));
  const list = h('div', 'list');
  list.style.marginTop = '8px';
  j.recent.forEach((r) => {
    list.appendChild(h('div', 'item', null, [
      h('span', 'what', r.time + '  ' + r.index + ' ' + r.side),
      h('span', r.r === null ? 'muted' : (r.r >= 0 ? 'up' : 'down'), r.status + (r.r === null ? '' : '  ' + rUnit(r.r))),
    ]));
  });
  box.appendChild(list);
  if (j.finished < 30) box.appendChild(h('div', 'muted small', 'Fewer than 30 finished ideas: too early to trust the win rate.'));
}

// ---------------- latest candle + recent ----------------
function drawMiniCandle(l) {
  const svg = clear($('lcSvg')), NS = 'http://www.w3.org/2000/svg';
  const range = (l.high - l.low) || 1, y = (v) => 12 + (l.high - v) / range * 80;
  const col = l.colour === 'GREEN' ? 'var(--green)' : l.colour === 'RED' ? 'var(--red)' : 'var(--muted)';
  const wick = document.createElementNS(NS, 'line');
  [['x1', 22], ['y1', y(l.high)], ['x2', 22], ['y2', y(l.low)], ['stroke', col], ['stroke-width', 2]].forEach(([k, v]) => wick.setAttribute(k, v));
  const top = Math.min(y(l.open), y(l.close)), hgt = Math.max(1.5, Math.abs(y(l.open) - y(l.close)));
  const body = document.createElementNS(NS, 'rect');
  [['x', 16], ['y', top], ['width', 12], ['height', hgt], ['fill', col]].forEach(([k, v]) => body.setAttribute(k, v));
  svg.appendChild(wick); svg.appendChild(body);
}

function renderCandle(d) {
  const l = d.latest;
  $('lcTime').textContent = '\u00B7 ' + l.time;
  $('lcO').textContent = fmt(l.open); $('lcH').textContent = fmt(l.high);
  $('lcL').textContent = fmt(l.low); $('lcC').textContent = fmt(l.close);
  const colourEl = $('lcColour');
  colourEl.textContent = l.colour.charAt(0) + l.colour.slice(1).toLowerCase();
  colourEl.className = 'val ' + (l.colour === 'GREEN' ? 'up' : l.colour === 'RED' ? 'down' : '');
  $('lcTrend').textContent = l.trend_before.charAt(0) + l.trend_before.slice(1).toLowerCase();
  drawMiniCandle(l);

  const box = clear($('patterns'));
  if (!l.patterns.length) {
    box.appendChild(h('div', 'pat muted', 'No clear pattern on this candle.'));
  } else {
    l.patterns.forEach((p) => {
      box.appendChild(h('div', 'pat', null, [
        h('div', null, null, [h('span', 'name', p.name), badge(p.leans)]),
        h('div', null, p.meaning),
        h('div', 'muted small', p.needs),
      ]));
    });
  }

  const rec = clear($('recent'));
  if (!d.recent.length) { rec.textContent = 'None found in the loaded candles.'; return; }
  d.recent.forEach((r) => {
    rec.appendChild(h('div', 'item', null, [
      h('span', 'what', null, [h('span', 't', r.time + '  '), document.createTextNode(r.patterns.join(' + '))]),
      badge(r.leans),
    ]));
  });
}

// ---------------- top-level render ----------------
function render(d) {
  const up = d.prev_price === null || d.last_price >= d.prev_price;
  $('price').textContent = fmt(d.last_price);
  $('price').className = 'price ' + (up ? 'up' : 'down');
  if (d.prev_price !== null) {
    const diff = d.last_price - d.prev_price;
    $('change').textContent = (diff >= 0 ? '\u25B2 +' : '\u25BC ') + fmt(diff) + ' (' + (diff / d.prev_price * 100).toFixed(2) + '%)';
    $('change').className = 'change ' + (diff >= 0 ? 'up' : 'down');
  } else { $('change').textContent = ''; }

  const pm = $('pMarket');
  pm.className = 'pill' + (d.market_open ? ' open' : '');
  $('pMarketText').textContent = d.market_open ? 'Market open' : 'Market closed';
  const pd = $('pData');
  pd.style.display = '';
  if (d.delayed) { pd.className = 'pill warn'; $('pDataText').textContent = 'Data ~' + d.data_lag_min + ' min old'; }
  else if (d.market_open) { pd.className = 'pill ok'; $('pDataText').textContent = 'Data up to date'; }
  else { pd.className = 'pill'; $('pDataText').textContent = 'Last session ' + d.newest_candle; }
  $('updated').textContent = 'Updated ' + d.updated + (d.candle_running ? ' \u00B7 candle running' : '') + (d.refreshing ? ' \u00B7 refreshing' : '');

  renderDecision(d);
  renderAnalysis(d);
  renderChain(d);
  renderRisk(d);
  renderWhy(d);
  renderTrade(d);
  renderJournal(d.journal);
  renderCandle(d);
}

// ---------------- history + memory ----------------
let histData = null, histFilter = 'ALL', openKey = null, lastHistJson = '';
const resClass = (label) => label === 'TIME EXIT' ? 'TIME' : label;

function closeDetail() { if (detailChart) { detailChart.remove(); detailChart = null; detailSeries = null; } }

async function drawSnapshot(key, box) {
  try {
    const r = await fetch('/api/snapshot?key=' + encodeURIComponent(key));
    const s = await r.json();
    if (!r.ok) { box.textContent = s.error || 'No chart.'; box.className = 'muted'; box.style.height = 'auto'; return; }
    box.textContent = '';
    const c = THEMES[theme];
    detailChart = LightweightCharts.createChart(box, Object.assign({ autoSize: true }, chartOptions(c)));
    const ser = detailSeries = detailChart.addCandlestickSeries(seriesOptions(c));
    ser.setData(s.candles);
    const line = (price, color, title) => { if (price !== null) ser.createPriceLine({
      price: price, color: color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: title }); };
    line(s.entry, '#4da3ff', 'ENTRY'); line(s.stop, '#ff7a70', 'STOP'); line(s.target, '#2bd4b4', 'TARGET');
    const marks = [{ time: s.signal_time, position: s.side === 'CE' ? 'belowBar' : 'aboveBar',
                     color: '#4da3ff', shape: s.side === 'CE' ? 'arrowUp' : 'arrowDown', text: 'signal' }];
    if (s.exit_time) {
      const col = s.status === 'TARGET' ? '#2bd4b4' : s.status === 'STOP' ? '#ff7a70' : '#f3b94d';
      marks.push({ time: s.exit_time, position: 'aboveBar', color: col, shape: 'circle',
                   text: s.status === 'TARGET' ? 'target hit' : s.status === 'STOP' ? 'stop hit' : 'time exit' });
    }
    marks.sort((a, b) => a.time - b.time);
    ser.setMarkers(marks);
    detailChart.timeScale().fitContent();
  } catch (e) { box.textContent = 'Could not load chart.'; }
}

function buildDetail(it) {
  const left = h('div');
  left.appendChild(h('div', null, it.story));
  if (it.setup) { const su = h('div', 'muted small', it.setup); su.style.marginTop = '6px'; left.appendChild(su); }
  const lv = h('div'); lv.style.marginTop = '8px';
  const ol = it.option_levels;
  if (ol) {
    lv.appendChild(h('div', 'group', it.option + ' option price'));
    kv(lv, 'Buy at', rs(ol.entry_price), 'lvl-buy');
    kv(lv, 'Stop loss at', rs(ol.stop_price), 'down');
    kv(lv, 'Target at', rs(ol.target_price), 'up');
    kv(lv, 'Loss / profit per unit', '-' + rs(ol.loss_per_unit) + '  /  +' + rs(ol.profit_per_unit));
    const srcText = ol.captured === 'live'
      ? 'LIVE option price, saved at the moment this idea appeared.'
      : ol.captured === 'estimate_now'
        ? 'ESTIMATE, saved when this idea appeared (no live option price was available, so it uses India VIX). The real price in Groww can differ by 10 to 20%.'
        : 'ESTIMATE. This idea has no saved option price, so it is worked out from the index level and the saved India VIX. The real price in Groww can differ by 10 to 20%.';
    lv.appendChild(h('div', 'muted small', srcText));
  } else {
    lv.appendChild(h('div', 'group', it.index + ' index levels'));
    kv(lv, 'Buy at', fmt(it.entry), 'lvl-buy');
    kv(lv, 'Stop loss at', fmt(it.stop), 'down');
    kv(lv, 'Target at', fmt(it.target), 'up');
  }
  if (it.bars) kv(lv, 'Candles held', String(it.bars));
  if (it.peak_r !== null && it.bars) kv(lv, 'Best / worst point', '+' + it.peak_r.toFixed(1) + 'R / -' + it.dd_r.toFixed(1) + 'R');
  left.appendChild(lv);
  const rs2 = h('div'); rs2.style.marginTop = '8px';
  (it.reasons || []).forEach((r) => rs2.appendChild(checkItem(r)));
  left.appendChild(rs2);
  if (it.memory) left.appendChild(h('div', 'box ' + it.memory.verdict, 'Memory said at that time: ' + it.memory.text));

  const box = h('div', 'hchart');
  if (it.has_chart) drawSnapshot(it.key, box);
  else { box.className = 'muted'; box.style.height = 'auto'; box.textContent = 'The chart is saved when the idea finishes (target, stop loss or time exit).'; }
  return h('div', 'hdetail', null, [left, h('div', null, null, [box])]);
}

function renderHistory() {
  if (!histData) return;
  const s = histData.summary;
  const sum = clear($('histSummary'));
  if (s.ideas === 0) {
    sum.className = 'muted';
    sum.textContent = 'No ideas saved yet. Every idea shown during market hours is saved here automatically, with its result.';
  } else {
    sum.className = 'sumline';
    const part = (label, val, cls) => h('span', null, null, [document.createTextNode(label + ' '), h('b', cls || '', val)]);
    sum.appendChild(part('Ideas', String(s.ideas)));
    sum.appendChild(part('Success', String(s.success), 'up'));
    sum.appendChild(part('Failed', String(s.failed), 'down'));
    sum.appendChild(part('Time exit', String(s.time_exit)));
    sum.appendChild(part('Waiting', String(s.waiting)));
    sum.appendChild(part('Win rate', s.win_rate === null ? '--' : (s.win_rate * 100).toFixed(0) + '%'));
    sum.appendChild(part('Net', (s.total_r >= 0 ? '+' : '') + s.total_r.toFixed(2) + 'R', s.total_r >= 0 ? 'up' : 'down'));
    if (s.finished < 30) sum.appendChild(h('span', 'muted', '(fewer than 30 finished: too early to trust)'));
  }
  document.querySelectorAll('#histChips .chip').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.f === histFilter)));

  closeDetail();
  const list = clear($('histList'));
  const items = histData.items.filter((x) => histFilter === 'ALL' || x.label === histFilter ||
    (histFilter === 'WAITING' && x.label === 'RUNNING'));
  if (items.length === 0 && s.ideas > 0) { list.appendChild(h('div', 'muted', 'Nothing in this filter.')); return; }

  items.forEach((it) => {
    const row = h('div', 'hrow', null, [
      h('div', 'when', it.time, [h('small', null, it.date + ' \u00B7 ' + it.index)]),
      h('div', null, null, [badge(it.side), h('span', null, '  ' + it.option)]),
      h('div', null, null, [h('span', 'res ' + resClass(it.label), it.label)]),
      h('div', 'r ' + (it.r_net === null ? '' : (it.r_net >= 0 ? 'up' : 'down')), rUnit(it.r_net)),
    ]);
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.setAttribute('aria-expanded', String(openKey === it.key));
    list.appendChild(row);
    if (openKey === it.key) list.appendChild(buildDetail(it));
    const toggle = () => { openKey = (openKey === it.key) ? null : it.key; renderHistory(); };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
  });
}

function renderLearned() {
  if (!histData) return;
  const L = histData.lessons, box = clear($('learned'));
  box.className = '';
  box.appendChild(h('div', null, 'The tool keeps a diary of every finished idea (pattern, time of day, trend strength, key level, VIX, and the result). ' +
    'When a new idea appears, it looks up similar past ideas. Below is what the diary says so far.'));
  const counts = h('div', 'muted', 'Experience saved: ' + L.experiences + ' ideas (' + L.live + ' from live journal, ' + L.backtest + ' from backtests). ' +
    'Break-even win rate is about ' + (L.breakeven * 100).toFixed(0) + '%.' +
    (L.overall ? ' Overall: ' + (L.overall.win_rate * 100).toFixed(0) + '% hit target, average ' + L.overall.avg_r.toFixed(2) + 'R.' : ''));
  counts.style.margin = '8px 0';
  box.appendChild(counts);
  const grid = h('div', 'learn-grid');
  const need = 'Not enough experience yet. A lesson needs at least ' + L.min_samples + ' similar ideas.';
  const block = (title, lines, ok) => {
    const c = h('div', 'card', null, [h('h3', null, title)]);
    if (!lines.length) c.appendChild(h('div', 'muted', need));
    lines.forEach((t) => c.appendChild(checkItem({ ok: ok, text: t })));
    return c;
  };
  grid.appendChild(block('What worked best', L.strengths, true));
  grid.appendChild(block('What failed most', L.weaknesses, false));
  box.appendChild(grid);
  const warn = h('div', 'muted small', 'Careful: small numbers can be luck, backtest memory comes from the same past data the rules were tuned on, and markets change. ' +
    'The memory only advises. You decide. To give it a head start, run: python candle_backtest.py --learn');
  warn.style.marginTop = '12px';
  box.appendChild(warn);
}

async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    const d = await r.json();
    const j = JSON.stringify(d);
    if (j === lastHistJson) return;           // nothing changed, do not redraw
    lastHistJson = j; histData = d;
    renderHistory(); renderLearned();
    applyChartOpts();
  } catch (e) { /* the dashboard keeps working without history */ }
}

function showTab(name) {
  const hist = name === 'history';
  $('tabHistory').style.display = hist ? '' : 'none';
  $('tabLearned').style.display = hist ? 'none' : '';
  $('tabBtnHistory').setAttribute('aria-selected', String(hist));
  $('tabBtnLearned').setAttribute('aria-selected', String(!hist));
}
$('tabBtnHistory').addEventListener('click', () => showTab('history'));
$('tabBtnLearned').addEventListener('click', () => showTab('learned'));
document.querySelectorAll('#histChips .chip').forEach((b) => b.addEventListener('click', () => {
  histFilter = b.dataset.f; renderHistory();
}));

// ---------------- loading ----------------
let loading = false;
async function load(manual) {
  if (loading) return;
  loading = true;
  const params = new URLSearchParams({ index: state.index, interval: state.interval, source: state.source });
  if (manual === true) params.set('fresh', '1');
  try {
    const res = await fetch('/api/data?' + params.toString());
    const d = await res.json();
    if (!res.ok) { showError(d.error || 'Could not get data.'); return; }
    hideError();
    series.setData(d.candles);
    lastData = d;
    if (d.lines) setIndicatorLines(d.lines); else applyChartOpts();
    lastN = d.candles.length;
    if (needFit) { fitRecent(); needFit = false; }
    render(d);
    const sig = d.journal ? [d.journal.ideas, d.journal.finished, d.journal.waiting].join('-') : '';
    if (sig !== lastJournalSig) { lastJournalSig = sig; loadHistory(); }
  } catch (e) {
    showError('Cannot reach the dashboard server. Is dashboard.py still running?');
  } finally {
    loading = false;
    nextAt = Date.now() + REFRESH_MS;
  }
}

applyTheme(theme);
syncControls();
load();
loadHistory();
setInterval(() => { if (Date.now() >= nextAt) load(); }, 1000);
setInterval(loadHistory, 60000);

// Auto-refresh: when the server restarts because the code changed, reload this page by itself.
let serverToken = null;
async function watchServer() {
  try {
    const r = await fetch('/api/version', { cache: 'no-store' });
    const j = await r.json();
    if (serverToken === null) serverToken = j.token;
    else if (j.token !== serverToken) location.reload();
  } catch (e) { /* the server may be restarting right now; try again in a moment */ }
}
watchServer();
setInterval(watchServer, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-reload", action="store_true",
                        help="do not restart automatically when a .py file changes")
    parser.add_argument("--no-alerts", action="store_true",
                        help="do not run the background Telegram alert loop")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    # With auto-reload this file runs twice (a watcher and the real server). Print and open the
    # browser only in the first one, so you get one message and one tab.
    restarted_copy = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if not restarted_copy:
        reload_note = "" if args.no_reload else "  Code changes reload automatically."
        print(f"Dashboard running at {url}  (press Ctrl + C to stop).{reload_note}")
        print("Telegram alerts: switched off with --no-alerts" if args.no_alerts else _alert_banner())
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Start the alert loop only in the process that really serves (not in the auto-reload watcher),
    # so there is exactly one loop.
    if (restarted_copy or args.no_reload) and not args.no_alerts and tg.is_configured():
        start_alert_loop()
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=not args.no_reload)
