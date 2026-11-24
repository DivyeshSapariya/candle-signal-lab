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
from data_sources import IST, INTERVAL_MINUTES, drop_forming_candle, fetch_angel_option, get_candles
from option_pricer import build_option_levels, get_vix_daily
from candle_analysis import live_analysis, rejected_setups
from candle_signals import compute_features
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


CACHE_SECONDS = 10                        # do not call the data source more than every 10s
_cache = {}
SERVER_TOKEN = str(time.time())           # new value every time the server restarts (page auto-refresh)


def pretty(name: str) -> str:
    return name.replace("_", " ").title()


def chart_time(ts) -> int:
    return int(ts.timestamp()) + IST_OFFSET_SECONDS


def build_payload(source: str, index: str, interval: str) -> dict:
    full = get_candles(source, index, interval, include_forming=True)
    finished = drop_forming_candle(full, interval)
    if len(finished) < 5:
        raise RuntimeError("Not enough finished candles yet. Try again in a few minutes.")

    interval_min = INTERVAL_MINUTES[interval]
    analysed = compute_features(finished, index, interval_min, get_vix_daily())
    pattern_cols = list(PATTERN_INFO.keys())

    # --- candles for the chart (includes the running candle) ---
    candles = [
        {"time": chart_time(ts), "open": float(r.open), "high": float(r.high),
         "low": float(r.low), "close": float(r.close)}
        for ts, r in full.iterrows()
    ]

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
                position, shape, color = "belowBar", "arrowUp", "#26a69a"
            elif leans == "PE":
                position, shape, color = "aboveBar", "arrowDown", "#ef5350"
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
                           should_alert, on_new_idea)
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
        "journal": journal_summary,
        "last_price": current_price,
        "prev_price": float(full["close"].iloc[-2]) if len(full) > 1 else None,
        "candle_running": len(full) > len(finished),
        "candles": candles,
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

    try:
        payload = build_payload(source, index, interval)
    except Exception as error:
        return jsonify({"error": str(error)}), 502

    _cache[key] = (time.time(), payload)
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


def _alert_tick(source: str = None, markets: list = None) -> int:
    """
    One look at every watched market. Runs only on weekdays from 9:10 to 16:00 IST (the last
    half hour after the close is for late results). Returns how many markets were checked.
    """
    source = source or os.environ.get("ALERT_SOURCE", default_source()).strip().lower()
    now = datetime.now(IST)
    minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5 or not (9 * 60 + 10 <= minutes <= 16 * 60):
        return 0
    checked = 0
    for index, interval in (markets or _alert_markets()):
        key = (source, index, interval)
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            continue                                   # a browser tab just looked at it
        try:
            _cache[key] = (time.time(), build_payload(source, index, interval))
            checked += 1
        except Exception as error:
            print(f"[alerts] {index} {interval}: {error}")
    return checked


def _alert_loop() -> None:
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
<style>
  :root, :root[data-theme="dark"] {
    --bg: #0b0f17; --surface: #131925; --surface2: #1a2130; --line: #252d3d;
    --text: #e8ebf2; --muted: #8f99ab; --blue: #4da3ff;
    --green: #2fbfa5; --red: #ef5350; --amber: #f5a524;
    --green-bg: rgba(47,191,165,.13); --red-bg: rgba(239,83,80,.13); --amber-bg: rgba(245,165,36,.13);
    --blue-bg: rgba(77,163,255,.13); --shadow: 0 1px 2px rgba(0,0,0,.35);
  }
  :root[data-theme="light"] {
    --bg: #f3f5f9; --surface: #ffffff; --surface2: #f0f3f8; --line: #dde2ec;
    --text: #18202e; --muted: #5d6879; --blue: #1a6fd1;
    --green: #0d8f7d; --red: #d03a37; --amber: #a86400;
    --green-bg: rgba(13,143,125,.10); --red-bg: rgba(208,58,55,.09); --amber-bg: rgba(168,100,0,.10);
    --blue-bg: rgba(26,111,209,.09); --shadow: 0 1px 2px rgba(20,30,50,.08);
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text); line-height: 1.35;
         font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13.5px; }
  button, select { font: inherit; color: inherit; }
  button { cursor: pointer; }
  :focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
  .up { color: var(--green); } .down { color: var(--red); } .muted { color: var(--muted); }
  .lvl-buy { color: var(--blue); }
  .small { font-size: 12px; }
  .wrap { max-width: 1800px; margin: 0 auto; padding: 0 12px; }

  /* ---------- top bar (title, status and controls in ONE row) ---------- */
  .topbar { background: var(--surface); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 20; }
  .topbar .wrap { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; padding-top: 6px; padding-bottom: 6px; }
  .brand h1 { margin: 0; font-size: 15px; font-weight: 700; white-space: nowrap; }
  .status { display: flex; flex-wrap: nowrap; gap: 6px; align-items: center; flex: 1 1 auto; min-width: 0; white-space: nowrap; }
  .controls { display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center; margin-left: auto; }
  .seg { display: inline-flex; background: var(--surface2); border: 1px solid var(--line); border-radius: 8px; padding: 2px; gap: 2px; }
  .seg button { background: transparent; border: 0; border-radius: 6px; padding: 4px 10px; font-size: 13px; color: var(--muted); font-weight: 600; }
  .seg button:hover { color: var(--text); }
  .seg button[aria-pressed="true"] { background: var(--surface); color: var(--text); box-shadow: var(--shadow); outline: 1px solid var(--line); }
  .field { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
  select { background: var(--surface2); border: 1px solid var(--line); border-radius: 7px; padding: 4px 8px; font-size: 13px; }
  .btn { background: var(--surface2); border: 1px solid var(--line); border-radius: 7px; padding: 4px 10px; font-size: 13px; font-weight: 600; }
  .btn:hover { border-color: var(--blue); }
  .btn.icon { padding: 4px 8px; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px; border-radius: 999px;
          font-size: 12px; font-weight: 600; background: var(--surface2); border: 1px solid var(--line); white-space: nowrap; }
  .pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
  .pill.open .dot { background: var(--green); box-shadow: 0 0 0 3px var(--green-bg); }
  .pill.warn { background: var(--amber-bg); border-color: var(--amber); color: var(--amber); }
  .pill.warn .dot { background: var(--amber); }
  .pill.ok { color: var(--green); }
  .pill.ok .dot { background: var(--green); }

  #error { display: none; margin: 8px 0 0; background: var(--red-bg); border: 1px solid var(--red);
           border-radius: 8px; padding: 6px 12px; align-items: center; gap: 12px; }
  #error.show { display: flex; }
  #error span { flex: 1; }
  #error button { background: transparent; border: 0; font-size: 18px; color: var(--muted); }

  /* ---------- decision banner (slim) ---------- */
  .decision { margin-top: 8px; border-radius: 10px; border: 1px solid var(--line); background: var(--surface);
              padding: 8px 14px; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr);
              gap: 14px; align-items: center; box-shadow: var(--shadow); border-left-width: 5px; }
  .decision.none { border-left-color: var(--muted); }
  .decision.ce { border-left-color: var(--green); background: linear-gradient(90deg, var(--green-bg), var(--surface) 60%); }
  .decision.pe { border-left-color: var(--red); background: linear-gradient(90deg, var(--red-bg), var(--surface) 60%); }
  .kickrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
  .decision .kicker { font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
  .decision h2 { margin: 0; font-size: 21px; line-height: 1.15; }
  .decision.ce h2 { color: var(--green); } .decision.pe h2 { color: var(--red); }
  .decision .sub { color: var(--muted); font-size: 13px; max-width: 70ch; }
  .tag { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 11px; font-weight: 700;
         background: var(--amber-bg); border: 1px solid var(--amber); color: var(--amber); }
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  .tile { background: var(--surface2); border: 1px solid var(--line); border-radius: 8px; padding: 5px 10px; }
  .tile .lbl { font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }
  .tile .val { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .tile .note { font-size: 11px; color: var(--muted); }
  .tile.buy .val { color: var(--blue); } .tile.stop .val { color: var(--red); } .tile.target .val { color: var(--green); }
  .tiles-wrap .what { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .tiles-wrap .what b { color: var(--text); }
  .empty-hint { color: var(--muted); font-size: 12.5px; border: 1px dashed var(--line); border-radius: 8px; padding: 6px 10px; }

  /* ---------- layout ---------- */
  .grid { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 10px; margin-top: 10px; align-items: start; }
  .left, .col { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  /* under the chart: latest candle and recent patterns sit side by side */
  .lowcol { flex-direction: row; align-items: stretch; }
  .lowcol > .card:first-child { flex: 1.5 1 0; min-width: 0; }
  .lowcol > .card:last-child { flex: 1 1 0; min-width: 0; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; box-shadow: var(--shadow); }
  .card h3 { margin: 0 0 5px; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 700; }
  .card h3 .hint { text-transform: none; letter-spacing: 0; font-weight: 500; }
  .row { display: flex; justify-content: space-between; gap: 10px; padding: 3px 0; border-top: 1px solid var(--line); }
  .row:first-of-type { border-top: 0; }
  .row .k { color: var(--muted); } .row .v { font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; }

  /* chart card: title, price and legend share ONE line */
  .chart-head { display: flex; flex-wrap: wrap; gap: 2px 14px; align-items: center; margin-bottom: 4px; }
  .ch-left { display: flex; align-items: baseline; gap: 8px; }
  .price { font-size: 22px; font-weight: 800; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .change { font-size: 12.5px; font-weight: 600; }
  .legend { display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 11.5px; color: var(--muted); flex: 1; }
  .legend b { font-size: 12px; }
  #chart { height: clamp(260px, calc(100vh - 505px), 560px); border-radius: 6px; overflow: hidden; }

  /* latest candle */
  .ohlc { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
  .ohlc div { background: var(--surface2); border-radius: 6px; padding: 3px 8px; }
  .ohlc .lbl { font-size: 11px; color: var(--muted); }
  .ohlc .val { font-weight: 700; font-size: 13px; font-variant-numeric: tabular-nums; }
  .pat { margin-top: 6px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface2); }
  .pat + .pat { margin-top: 5px; }
  .pat .name { font-weight: 700; margin-right: 8px; }

  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }
  .badge.CE { background: var(--green-bg); color: var(--green); border: 1px solid var(--green); }
  .badge.PE { background: var(--red-bg); color: var(--red); border: 1px solid var(--red); }
  .badge.WAIT { background: var(--surface2); color: var(--muted); border: 1px solid var(--line); }

  /* checklist */
  .pips { display: flex; align-items: center; gap: 4px; margin-bottom: 5px; }
  .pip { width: 22px; height: 6px; border-radius: 3px; background: var(--line); }
  .pip.on { background: var(--green); }
  .pips span { margin-left: 6px; font-size: 12px; color: var(--muted); }
  .group { font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin: 6px 0 2px; }
  .check { display: flex; gap: 7px; padding: 1px 0; align-items: flex-start; }
  .check .ic { flex: none; width: 17px; height: 17px; border-radius: 50%; display: inline-flex; align-items: center;
               justify-content: center; font-size: 11px; font-weight: 800; margin-top: 1px; }
  .check.ok .ic { background: var(--green-bg); color: var(--green); }
  .check.no .ic { background: var(--red-bg); color: var(--red); }

  /* option details */
  .badge-src { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 6px; }
  .badge-src.live { background: var(--green-bg); color: var(--green); }
  .badge-src.est { background: var(--amber-bg); color: var(--amber); }
  .box { margin-top: 6px; padding: 5px 9px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface2); font-size: 12.5px; }
  .box.warn { border-color: var(--amber); background: var(--amber-bg); }
  .box.agrees { border-color: var(--green); } .box.warns { border-color: var(--red); }
  details { margin-top: 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface2); }
  details summary { cursor: pointer; padding: 6px 10px; font-weight: 700; list-style: none; }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "\25B8"; display: inline-block; margin-right: 8px; transition: transform .15s; color: var(--muted); }
  details[open] summary::before { transform: rotate(90deg); }
  details .inner { padding: 0 12px 8px 12px; }
  details ol { margin: 0; padding-left: 18px; } details li { padding: 2px 0; }

  /* scoreboard */
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 5px; }
  .stat { background: var(--surface2); border-radius: 6px; padding: 3px 8px; text-align: center; }
  .stat .n { font-size: 16px; font-weight: 800; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .stat .l { font-size: 11px; color: var(--muted); }

  .list .item { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 3px 0; border-top: 1px solid var(--line); }
  .list .item:first-child { border-top: 0; }
  .list .t { color: var(--muted); font-size: 12px; }
  .list .what { flex: 1; min-width: 0; }
  #recent { max-height: 150px; overflow-y: auto; }
  /* live analysis: the 4 steps and the recently dropped setups */
  .steps { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 4px 0 8px; }
  .step { border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font-size: 12px; min-width: 0; }
  .step .st { font-weight: 700; display: flex; justify-content: space-between; align-items: center; gap: 6px; white-space: nowrap; }
  .step .sd { margin-top: 2px; color: var(--muted); line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2;
              -webkit-box-orient: vertical; overflow: hidden; }
  .step.ok { border-color: var(--green); background: var(--green-bg); } .step.ok .st { color: var(--green); }
  .step.fail { border-color: var(--red); background: var(--red-bg); } .step.fail .st { color: var(--red); }
  .step.wait { opacity: .6; }
  #stepNote { font-size: 13px; line-height: 1.4; }
  #rejected { max-height: 260px; overflow-y: auto; }
  #rejected .item { display: block; }
  #rejected .why { font-size: 12px; color: var(--muted); }
  @media (min-width: 1100px) { #rejected { column-count: 2; column-gap: 18px; max-height: none; }
                               #rejected .item { break-inside: avoid; } }

  /* history */
  .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 12px 0 6px; }
  .tab { background: var(--surface); border: 1px solid var(--line); border-radius: 999px; padding: 4px 14px; font-weight: 700; color: var(--muted); }
  .tab[aria-selected="true"] { color: var(--text); border-color: var(--blue); background: var(--blue-bg); }
  .chips { display: flex; gap: 5px; flex-wrap: wrap; margin: 6px 0; }
  .chip { background: var(--surface2); border: 1px solid var(--line); border-radius: 999px; padding: 2px 11px; font-size: 12.5px; color: var(--muted); font-weight: 600; }
  .chip[aria-pressed="true"] { color: var(--text); border-color: var(--blue); background: var(--blue-bg); }
  .sumline { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13px; }
  .sumline b { font-variant-numeric: tabular-nums; }
  .hrow { display: grid; grid-template-columns: 120px minmax(0, 1fr) 100px 80px; gap: 8px; align-items: center;
          padding: 5px 8px; border-top: 1px solid var(--line); cursor: pointer; border-radius: 6px; }
  .hrow:hover { background: var(--surface2); }
  .hrow .when { font-weight: 600; } .hrow .when small { margin-left: 6px; font-weight: 400; color: var(--muted); }
  .hrow .r { text-align: right; font-weight: 800; font-variant-numeric: tabular-nums; }
  .res { display: inline-block; padding: 1px 10px; border-radius: 999px; font-size: 11px; font-weight: 800; text-align: center;
         background: var(--surface2); color: var(--muted); border: 1px solid var(--line); }
  .res.SUCCESS { background: var(--green-bg); color: var(--green); border-color: var(--green); }
  .res.FAILED { background: var(--red-bg); color: var(--red); border-color: var(--red); }
  .res.TIME { background: var(--amber-bg); color: var(--amber); border-color: var(--amber); }
  .hdetail { padding: 10px; background: var(--surface2); border: 1px solid var(--line); border-radius: 8px; margin: 2px 0 8px;
             display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  .hchart { height: 220px; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  .learn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; margin-top: 8px; }
  .learn-grid .card { box-shadow: none; background: var(--surface2); }

  .guide { margin-top: 12px; }
  .guide ul { margin: 0; padding-left: 20px; } .guide li { padding: 2px 0; }
  footer { padding: 10px 0 20px; color: var(--muted); font-size: 12px; }

  /* ---------- screen sizes ---------- */
  /* wide: chart | checklist + trade | candle + scoreboard, all in one row */
  @media (min-width: 1500px) {
    .grid { grid-template-columns: minmax(0, 1fr) 330px 310px; }
    .left { display: contents; }
    .chartcard { grid-column: 1; grid-row: 1; }
    .mid { grid-column: 2; grid-row: 1; }
    .lowcol { grid-column: 3; grid-row: 1; flex-direction: column; }
    .lowcol > .card { flex: none; }
    #cardRejected { grid-column: 1; grid-row: 2; }
    #chart { height: clamp(320px, calc(100vh - 240px), 900px); }
  }
  @media (max-width: 1000px) {
    .grid { grid-template-columns: 1fr; }
    .mid, .lowcol { display: grid; grid-template-columns: 1fr 1fr; align-items: start; }
    .decision { grid-template-columns: 1fr; gap: 8px; }
    #chart { height: clamp(280px, 48vh, 460px); }
  }
  @media (max-width: 640px) {
    body { font-size: 14px; }
    .brand h1 { font-size: 14px; }
    .controls { margin-left: 0; }
    .mid, .lowcol { grid-template-columns: 1fr; }
    .decision h2 { font-size: 19px; }
    .tiles { grid-template-columns: 1fr; }
    .tile { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
    .ohlc { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .hrow { grid-template-columns: 1fr auto; row-gap: 2px; }
    .hrow .r { text-align: left; }
    .hdetail { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>

<header class="topbar">
  <div class="wrap">
    <div class="brand"><h1>Candle Signal Dashboard</h1></div>
    <div class="status" aria-live="polite">
      <span class="pill" id="pMarket"><span class="dot"></span><span id="pMarketText">Connecting...</span></span>
      <span class="pill" id="pData" style="display:none"><span class="dot"></span><span id="pDataText"></span></span>
      <span class="muted small" id="updated">Loading...</span>
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
      <button class="btn" id="refresh" type="button">&#x21BB; Refresh</button>
      <button class="btn icon" id="themeBtn" type="button" aria-label="Switch light or dark theme" title="Switch light / dark">&#x263E;</button>
    </div>
  </div>
</header>

<div class="wrap">
  <div id="error" role="alert"><span id="errorText"></span><button type="button" id="errorClose" aria-label="Dismiss">&times;</button></div>

  <!-- THE ANSWER -->
  <section class="decision none" id="decision" aria-live="polite" aria-label="Trade decision">
    <div>
      <div class="kickrow">
        <div class="kicker" id="dKicker">Trade idea</div>
        <div class="tag" id="dTag" style="display:none"></div>
      </div>
      <h2 id="dTitle">Loading latest candles...</h2>
      <div class="sub" id="dSub">Please wait a moment.</div>
    </div>
    <div class="tiles-wrap" id="dRight"></div>
  </section>

  <div class="grid">
    <div class="left">
      <section class="card chartcard">
        <div class="chart-head">
          <div class="ch-left">
            <span class="muted small" id="chartTitle">NIFTY 50 &middot; 5 min</span>
            <span><span class="price" id="price">--</span><span class="change" id="change"></span></span>
          </div>
          <div class="legend">
            <span><b class="up">&#x25B2;</b> leans CE (up)</span>
            <span><b class="down">&#x25BC;</b> leans PE (down)</span>
            <span><b>&bull;</b> wait</span>
          </div>
          <button class="btn" id="resetView" type="button">Reset view</button>
        </div>
        <div id="chart"></div>
      </section>

      <div class="col lowcol">
        <section class="card">
          <h3>Latest finished candle <span class="hint" id="lcTime"></span></h3>
          <div class="ohlc">
            <div><div class="lbl">Open</div><div class="val" id="lcO">--</div></div>
            <div><div class="lbl">High</div><div class="val" id="lcH">--</div></div>
            <div><div class="lbl">Low</div><div class="val" id="lcL">--</div></div>
            <div><div class="lbl">Close</div><div class="val" id="lcC">--</div></div>
            <div><div class="lbl">Colour</div><div class="val" id="lcColour">--</div></div>
            <div><div class="lbl">Trend before</div><div class="val" id="lcTrend">--</div></div>
          </div>
          <div id="patterns"></div>
        </section>

        <section class="card">
          <h3>Recent candle patterns</h3>
          <div id="recent" class="list muted">--</div>
        </section>
      </div>

      <section class="card" id="cardRejected">
        <h3>Recent setups with no idea <span class="hint">(a pattern appeared, but a rule said no)</span></h3>
        <div id="rejected" class="list muted">--</div>
      </section>
    </div>

    <div class="col mid">
      <section class="card" id="cardAnalysis">
        <h3>Live analysis <span class="hint" id="anNext"></span></h3>
        <div class="steps" id="anSteps"></div>
        <div id="stepNote" class="muted">--</div>
        <div id="anFacts" class="muted small" style="margin-top:4px"></div>
      </section>
      <section class="card" id="cardWhy">
        <h3>Why this decision</h3>
        <div id="why" class="muted">--</div>
      </section>
      <section class="card" id="cardTrade" style="display:none">
        <h3>Trade details</h3>
        <div id="trade"></div>
      </section>
      <section class="card">
        <h3>Paper-trade scoreboard <span class="hint">(automatic, no real money)</span></h3>
        <div id="journal" class="muted">--</div>
      </section>
    </div>
  </div>

  <!-- HISTORY / MEMORY -->
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="tabBtnHistory" aria-selected="true" type="button">Idea history</button>
    <button class="tab" role="tab" id="tabBtnLearned" aria-selected="false" type="button">What the tool learned</button>
  </div>
  <section class="card" id="tabHistory">
    <div id="histSummary" class="sumline muted">Loading...</div>
    <div class="chips" id="histChips">
      <button class="chip" data-f="ALL" type="button">All</button>
      <button class="chip" data-f="SUCCESS" type="button">Success</button>
      <button class="chip" data-f="FAILED" type="button">Failed</button>
      <button class="chip" data-f="TIME EXIT" type="button">Time exit</button>
      <button class="chip" data-f="WAITING" type="button">Waiting</button>
    </div>
    <div id="histList"></div>
  </section>
  <section class="card" id="tabLearned" style="display:none">
    <div id="learned" class="muted">Loading...</div>
  </section>

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
  dark:  { bg: '#131925', text: '#c4cad6', grid: '#1e2535', border: '#2a3246', up: '#26a69a', down: '#ef5350' },
  light: { bg: '#ffffff', text: '#2b3445', grid: '#eceff5', border: '#d5dbe7', up: '#0d8f7d', down: '#d03a37' },
};
let theme = store.get('cs.theme', 'dark') === 'light' ? 'light' : 'dark';
const chartOptions = (c) => ({
  layout: { background: { color: c.bg }, textColor: c.text },
  grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: c.border },
  rightPriceScale: { borderColor: c.border },
});
const seriesOptions = (c) => ({ upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down });

const chart = LightweightCharts.createChart($('chart'), Object.assign({ autoSize: true, crosshair: { mode: 0 } }, chartOptions(THEMES[theme])));
const series = chart.addCandlestickSeries(seriesOptions(THEMES[theme]));
let detailChart = null, detailSeries = null;

function applyTheme(t) {
  theme = t;
  document.documentElement.setAttribute('data-theme', t);
  store.set('cs.theme', t);
  const c = THEMES[t];
  chart.applyOptions(chartOptions(c)); series.applyOptions(seriesOptions(c));
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
  needFit = true; syncControls(); load();
}
document.querySelectorAll('#indexSeg button').forEach((b) => b.addEventListener('click', () => setState('index', b.dataset.v)));
document.querySelectorAll('#intervalSeg button').forEach((b) => b.addEventListener('click', () => setState('interval', b.dataset.v)));
$('source').addEventListener('change', (e) => setState('source', e.target.value));
$('refresh').addEventListener('click', load);
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
function renderDecision(d) {
  const p = d.plan;
  const box = $('decision');
  const right = clear($('dRight'));
  const tag = $('dTag');
  const practice = d.delayed
    ? 'Practice only: data is about ' + d.data_lag_min + ' min old (free Yahoo is delayed). Use Angel One for live data.'
    : (!d.market_open ? 'Practice only: market is closed, this is the last session.' : '');

  if (p.signal === 'NONE') {
    box.className = 'decision none';
    $('dKicker').textContent = 'Trade idea';
    $('dTitle').textContent = 'No trade right now';
    $('dSub').textContent = p.message || d.summary;
    right.appendChild(h('div', 'empty-hint', 'Most of the time there is no good setup, and that is normal. The filters are strict on purpose. ' +
      'Waiting is a decision. The checklist on the right shows what is missing.'));
  } else {
    const isCE = p.signal === 'CE';
    box.className = 'decision ' + (isCE ? 'ce' : 'pe');
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

// ---------------- live analysis (4 steps + dropped setups) ----------------
function renderAnalysis(d) {
  const a = d.analysis, stepsBox = clear($('anSteps')), note = $('stepNote'), rej = clear($('rejected'));
  if (!a) { note.textContent = 'Analysis is not available right now.'; rej.textContent = '--'; $('anNext').textContent = ''; return; }
  $('anNext').textContent = a.next_candle_closes ? '· next candle closes ' + a.next_candle_closes : '';
  const icon = { ok: '✔', fail: '✘', wait: '…' };
  a.steps.forEach((s, n) => {
    const el = h('div', 'step ' + s.state, null, [
      h('div', 'st', null, [h('span', null, (n + 1) + '. ' + s.title), h('span', null, icon[s.state])]),
      h('div', 'sd', s.detail),
    ]);
    el.title = s.detail;
    stepsBox.appendChild(el);
  });
  note.textContent = a.headline;
  $('anFacts').textContent = a.facts && a.facts.length ? a.facts.join('  ·  ') : '';
  if (!a.rejected.length) { rej.textContent = 'None in the recent candles.'; return; }
  a.rejected.forEach((r) => {
    rej.appendChild(h('div', 'item', null, [
      h('div', null, null, [h('span', 't', r.time + '  '), document.createTextNode(r.setup)]),
      h('div', 'why', 'Dropped: ' + r.why),
    ]));
  });
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
  addLine(p.stop, '#ef5350', 'STOP LOSS');
  addLine(p.target, '#26a69a', 'TARGET');
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
function renderCandle(d) {
  const l = d.latest;
  $('lcTime').textContent = '\u00B7 ' + l.time;
  $('lcO').textContent = fmt(l.open); $('lcH').textContent = fmt(l.high);
  $('lcL').textContent = fmt(l.low); $('lcC').textContent = fmt(l.close);
  const colourEl = $('lcColour');
  colourEl.textContent = l.colour.charAt(0) + l.colour.slice(1).toLowerCase();
  colourEl.className = 'val ' + (l.colour === 'GREEN' ? 'up' : l.colour === 'RED' ? 'down' : '');
  $('lcTrend').textContent = l.trend_before.charAt(0) + l.trend_before.slice(1).toLowerCase();

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
  $('updated').textContent = 'Updated ' + d.updated + (d.candle_running ? ' \u00B7 candle running' : '');

  renderDecision(d);
  renderAnalysis(d);
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
    line(s.entry, '#4da3ff', 'ENTRY'); line(s.stop, '#ef5350', 'STOP'); line(s.target, '#26a69a', 'TARGET');
    const marks = [{ time: s.signal_time, position: s.side === 'CE' ? 'belowBar' : 'aboveBar',
                     color: '#4da3ff', shape: s.side === 'CE' ? 'arrowUp' : 'arrowDown', text: 'signal' }];
    if (s.exit_time) {
      const col = s.status === 'TARGET' ? '#26a69a' : s.status === 'STOP' ? '#ef5350' : '#f5a524';
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
async function load() {
  if (loading) return;
  loading = true;
  const params = new URLSearchParams({ index: state.index, interval: state.interval, source: state.source });
  try {
    const res = await fetch('/api/data?' + params.toString());
    const d = await res.json();
    if (!res.ok) { showError(d.error || 'Could not get data.'); return; }
    hideError();
    series.setData(d.candles);
    series.setMarkers(d.markers);
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
