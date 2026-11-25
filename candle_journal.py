"""
candle_journal.py
-----------------
Automatic trade journal (paper trading) and idea HISTORY.

Every trade idea is saved, even after it disappears from the "Trade idea" card:
  - what the idea was (CE / PE, entry, stop loss, target) and the checklist behind it
  - what happened later: SUCCESS (target hit), FAILED (stop loss hit), TIME EXIT, or waiting
  - a plain-English story of what happened
  - a small chart (saved in the trade_snapshots folder when the idea finishes)
  - what the memory said about similar past ideas at that moment

Finished ideas also go into the memory (candle_memory.py), so the tool learns from them.

The dashboard also scans today's candles each time, so an idea is saved even if it appeared
and vanished while you were not looking.

Note: with delayed (Yahoo) data the ideas are seen late, so treat results as "if I had acted on time".
Files: trade_journal.csv, trade_snapshots/*.json
"""

import json
import os
import re
import threading

import pandas as pd

import candle_memory
from candle_backtest import COST_R, simulate_trade
from candle_signals import evaluate, pretty

JOURNAL_FILE = "trade_journal.csv"
SNAPSHOT_DIR = "trade_snapshots"
IST_OFFSET = 5 * 3600 + 30 * 60          # the chart shows UTC, so times are shifted to IST
COLUMNS = ["key", "created", "index", "interval", "side", "signal_time", "decision_time",
           "mode", "entry", "stop", "target", "risk", "option", "status", "fill",
           "exit_time", "exit_price", "r", "note",
           "pattern", "score", "reasons", "context", "memory", "bars", "peak_r", "dd_r", "story",
           "option_levels", "alerts"]
FINISHED = ("TARGET", "STOP", "TIME")
LABELS = {"TARGET": "SUCCESS", "STOP": "FAILED", "TIME": "TIME EXIT", "PENDING": "WAITING",
          "OPEN": "RUNNING", "SKIPPED": "SKIPPED"}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _load() -> pd.DataFrame:
    if not os.path.exists(JOURNAL_FILE):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(JOURNAL_FILE, dtype=str, keep_default_na=False)   # everything as text
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[COLUMNS]


def _save(df: pd.DataFrame):
    df.to_csv(JOURNAL_FILE, index=False)


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _num(value):
    n = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(n) else float(n)


def _chart_time(ts) -> int:
    return int(pd.Timestamp(ts).timestamp()) + IST_OFFSET


def safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(key))


def snapshot_path(key: str) -> str:
    return os.path.join(SNAPSHOT_DIR, safe_name(key) + ".json")


# ---------------------------------------------------------------------------
# Words: describe the setup and tell the story of what happened
# ---------------------------------------------------------------------------
def setup_line(ctx: dict) -> str:
    if not ctx:
        return ""
    word = lambda v: "up" if v > 0 else "down" if v < 0 else "unknown"
    bits = [" + ".join(pretty(p) for p in ctx.get("patterns", []))]
    if ctx.get("level"):
        bits.append(ctx["level"])
    if ctx.get("adx") is not None:
        bits.append(f"ADX {ctx['adx']:.0f}")
    bits.append(f"15 min trend {word(ctx.get('htf15', 0))}, 60 min trend {word(ctx.get('htf60', 0))}")
    if ctx.get("vix") is not None:
        bits.append(f"India VIX {ctx['vix']:.1f}")
    if ctx.get("is_expiry"):
        bits.append("expiry day")
    return "Setup: " + "; ".join(b for b in bits if b) + "."


def make_story(status: str, bars: int, peak_r: float, dd_r: float, r_gross, interval_min: int,
               note: str = "") -> str:
    minutes = bars * interval_min
    if status == "TARGET":
        text = f"Target reached after {bars} candle(s) ({minutes} min)."
        if dd_r >= 0.6:
            text += f" It was uncomfortable on the way: price dipped {dd_r:.1f}R against the idea before turning."
        else:
            text += " Price moved the right way without a big dip."
    elif status == "STOP":
        if bars <= 2:
            text = (f"Stop loss hit within {bars} candle(s). Price turned against the idea almost at once, "
                    "so this was probably a false signal.")
        elif peak_r >= 1.0:
            text = (f"Price first moved {peak_r:.1f}R in favour (the target needs 2R), then reversed and hit the "
                    f"stop loss after {bars} candles. A near miss.")
        else:
            text = (f"Price drifted against the idea and hit the stop loss after {bars} candles ({minutes} min). "
                    f"It never moved much in favour (best {peak_r:.1f}R).")
    elif status == "TIME":
        text = (f"Neither target nor stop loss was hit. The trade was closed after {bars} candles ({minutes} min) "
                f"at {r_gross:+.1f}R. Best point +{peak_r:.1f}R, worst -{dd_r:.1f}R. "
                "Option buyers also lose money to time decay while waiting.")
    elif status == "SKIPPED":
        text = f"Not entered: {note}."
    elif status == "OPEN":
        text = f"Trade is running. So far: best +{peak_r:.1f}R, worst -{dd_r:.1f}R."
    else:
        text = "Waiting for the next candle to finish."
    return text


# ---------------------------------------------------------------------------
# Save a new idea
# ---------------------------------------------------------------------------
def record_idea(plan: dict, index: str, interval: str, created: str, memory: dict = None,
                option_levels: dict = None, alerts: str = "", skip_reason: str = "") -> bool:
    """
    Save a new idea once. Returns True if it was new.
    option_levels: the option prices (buy / stop loss / target) at the moment the idea was made.
    It is saved with the idea, because the real option price cannot be found out later.
    alerts: notes which alerts belong to this idea ("new" = a Telegram alert was sent for it, and
    "result" is added when the alert about its result has been sent). It keeps every alert to ONE.
    skip_reason: when given (a daily risk limit), the idea is saved as SKIPPED with this note and is never traded.
    """
    key = f"{index}|{interval}|{plan['signal_time']}|{plan['signal']}"
    with _lock:
        df = _load()
        if key in set(df["key"].astype(str)):
            return False
        ctx = plan.get("context") or {}
        row = {
            "key": key, "created": created, "index": index, "interval": interval,
            "side": plan["signal"], "signal_time": plan["signal_time"],
            "decision_time": plan["decision_time"], "mode": plan["mode"],
            "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"],
            "risk": plan["risk_points"], "option": plan["option"],
            "status": "SKIPPED" if skip_reason else "PENDING", "fill": "", "exit_time": "", "exit_price": "", "r": "",
            "note": skip_reason,
            "pattern": ctx.get("pattern", ""), "score": plan.get("score", ""),
            "reasons": json.dumps(plan.get("reasons", [])), "context": json.dumps(ctx),
            "memory": json.dumps(memory) if memory else "",
            "bars": "", "peak_r": "", "dd_r": "",
            "story": (make_story("SKIPPED", 0, 0, 0, None, 5, skip_reason) if skip_reason
                      else make_story("PENDING", 0, 0, 0, None, 5)),
            "option_levels": json.dumps(option_levels) if option_levels else "",
            "alerts": alerts,
        }
        row = {k: str(v) for k, v in row.items()}            # the journal file stores plain text
        _save(pd.concat([df, pd.DataFrame([row])], ignore_index=True))
        return True


# ---------------------------------------------------------------------------
# Check how earlier ideas ended
# ---------------------------------------------------------------------------
def _write_snapshot(f: pd.DataFrame, i: int, res: dict, row) -> None:
    exit_idx = res["exit_idx"] if res["exit_idx"] is not None else min(i + 1, len(f) - 1)
    lo, hi = max(0, i - 40), min(len(f) - 1, exit_idx + 6)
    part = f.iloc[lo:hi + 1]
    snap = {
        "candles": [{"time": _chart_time(ts), "open": float(r.open), "high": float(r.high),
                     "low": float(r.low), "close": float(r.close)} for ts, r in part.iterrows()],
        "side": row["side"], "status": res["status"],
        "entry": res["fill"], "stop": res["stop"], "target": res["target"],
        "exit_price": res["exit_price"],
        "exit_time": _chart_time(res["exit_time"]) if res["exit_time"] else None,
        "signal_time": _chart_time(row["signal_time"]),
        "decision_time": _chart_time(row["decision_time"]),
    }
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(snapshot_path(row["key"]), "w", encoding="utf-8") as fh:
        json.dump(snap, fh)


def update_results(f: pd.DataFrame, index: str, interval_min: int, on_finished=None):
    """
    Check unfinished ideas against the finished candles in f (output of compute_features).
    on_finished: optional function(info) called ONCE for an idea that had an alert ("new") and has
    now ended (target / stop loss / time exit). info has the option, side, status, result in R, etc.
    """
    new_experiences = []
    finished_alerts = []
    with _lock:
        df = _load()
        open_rows = df[(df["index"] == index) & (df["interval"] == f"{interval_min}m")
                       & (df["status"].isin(["PENDING", "OPEN"]))]
        if open_rows.empty:
            return
        changed = False
        for row_id, row in open_rows.iterrows():
            try:
                i = f.index.get_loc(pd.Timestamp(row["decision_time"]).tz_convert(f.index.tz))
            except (KeyError, TypeError, ValueError):
                continue
            plan = {"signal": row["side"], "entry": float(row["entry"]),
                    "risk_points": float(row["risk"]), "mode": row["mode"]}
            res = simulate_trade(f, i, plan, interval_min)
            story = make_story(res["status"], res["bars"], res["peak_r"], res["dd_r"], res["r"],
                               interval_min, res["note"])
            new_values = [
                str(res["status"]),
                "" if res["fill"] is None else str(round(res["fill"], 2)),
                str(res["exit_time"] or ""),
                "" if res["exit_price"] is None else str(round(res["exit_price"], 2)),
                "" if res["r"] is None else str(res["r"]),
                str(res["note"]), str(res["bars"]), str(res["peak_r"]), str(res["dd_r"]), story,
            ]
            columns = ["status", "fill", "exit_time", "exit_price", "r", "note",
                       "bars", "peak_r", "dd_r", "story"]
            if list(df.loc[row_id, columns]) == new_values:
                continue
            df.loc[row_id, columns] = new_values
            changed = True

            if res["status"] in FINISHED:
                alerts = str(row["alerts"])
                if on_finished is not None and "new" in alerts and "result" not in alerts:
                    df.loc[row_id, "alerts"] = (alerts + ",result").strip(",")   # marked BEFORE sending
                    finished_alerts.append({
                        "key": row["key"], "index": row["index"], "interval": row["interval"],
                        "side": row["side"], "option": row["option"], "status": res["status"],
                        "exit_time": res["exit_time"],
                        "r_net": res["r"] - COST_R, "r_gross": res["r"], "bars": res["bars"], "story": story,
                        "peak_r": res["peak_r"], "dd_r": res["dd_r"],
                        "entry": _num(row["entry"]), "stop": _num(row["stop"]), "target": _num(row["target"]),
                        "option_levels": _json(row["option_levels"], None),
                    })
                try:
                    _write_snapshot(f, i, res, row)
                except Exception as error:
                    print(f"[journal] could not save chart: {error}")
                ctx = _json(row["context"], {})
                if ctx:
                    new_experiences.append(candle_memory.make_experience(
                        "live|" + row["key"], "live", row["index"], row["interval"],
                        row["signal_time"], row["side"], int(_num(row["score"]) or 0), ctx,
                        res["status"], res["r"] - COST_R))
        if changed:
            _save(df)
    if new_experiences:                      # the memory learns from every finished idea
        candle_memory.add_experiences(new_experiences)
    for info in finished_alerts:             # sent after the lock is released
        try:
            on_finished(info)
        except Exception as error:
            print(f"[journal] result alert failed: {error}")


def today_stats(day) -> dict:
    """How today is going, over all markets: ideas made (not counting skipped ones) and net R of finished ones."""
    with _lock:
        df = _load()
    ideas, net = 0, 0.0
    for _, row in df.iterrows():
        try:
            if pd.Timestamp(row["signal_time"]).date() != day:
                continue
        except (ValueError, TypeError):
            continue
        if row["status"] != "SKIPPED":
            ideas += 1
        r = _num(row["r"])
        if row["status"] in FINISHED and r is not None:
            net += r - COST_R
    return {"ideas": ideas, "net_r": round(net, 4)}


def waiting_markets() -> set:
    """(index, interval) of every saved idea that has no final result yet (waiting for entry, or running)."""
    with _lock:
        df = _load()
    open_rows = df[df["status"].isin(["PENDING", "OPEN"])]
    return {(str(r["index"]), str(r["interval"])) for _, r in open_rows.iterrows()}


def _status_of(key: str):
    """(status, note) of a saved idea, or None when it is new."""
    with _lock:
        df = _load()
    hit = df[df["key"] == key]
    return None if hit.empty else (str(hit.iloc[0]["status"]), str(hit.iloc[0]["note"]))


def scan_today(f: pd.DataFrame, index: str, interval: str, interval_min: int,
               today, created: str, option_levels_for=None, should_alert=None, on_new_idea=None,
               limit_check=None) -> int:
    """
    Save every idea of TODAY's session that is not saved yet (for example ideas that appeared
    and vanished while you were not looking). Same one-trade-at-a-time rule as the backtest.
    option_levels_for: optional function(plan) -> option prices to save with a new idea.
    should_alert: optional function(plan) -> True if a NEW idea deserves an alert (Telegram).
    on_new_idea: optional function(plan, option_levels, memory), called once for such an idea,
    only when it was really new (never again after a restart).
    limit_check: optional function(today_stats) -> reason or None. A NEW idea with a reason is saved as
    SKIPPED (daily risk limit) and gets no alert and no trade.
    """
    days = f["day"].values
    todays = [i for i in range(len(f)) if days[i] == today]
    if not todays:
        return 0
    added, blocked_until = 0, 0
    for i in range(max(todays[0], 30), len(f)):
        if i < blocked_until:
            continue
        plan = evaluate(f, i, index, interval_min)
        if plan["signal"] == "NONE":
            continue
        key = f"{index}|{interval}|{plan['signal_time']}|{plan['signal']}"
        known = _status_of(key)
        if known is not None and known[0] == "SKIPPED" and known[1].startswith("Daily limit"):
            continue                                  # skipped because of a risk limit: it was never traded
        skip_reason = ""
        if known is None and limit_check is not None:
            try:
                skip_reason = limit_check(today_stats(today)) or ""
            except Exception as error:                # a limit problem must never stop the journal
                print(f"[journal] could not check the daily limit: {error}")
        try:
            memory = candle_memory.recall(plan, index, interval_min)
        except Exception:
            memory = None
        levels = None
        if option_levels_for is not None:
            try:
                levels = option_levels_for(plan)
            except Exception as error:               # the journal must work without option prices
                print(f"[journal] could not get option prices: {error}")
        want_alert = False
        if should_alert is not None and on_new_idea is not None and not skip_reason:
            try:
                want_alert = bool(should_alert(plan))
            except Exception as error:
                print(f"[journal] could not decide about the alert: {error}")
        if record_idea(plan, index, interval, created, memory, levels, "new" if want_alert else "", skip_reason):
            added += 1
            if skip_reason:
                continue                              # saved as skipped: not traded, does not block the next idea
            if want_alert:
                try:
                    on_new_idea(plan, levels, memory)
                except Exception as error:           # an alert problem must never stop the journal
                    print(f"[journal] alert failed: {error}")
        res = simulate_trade(f, i, plan, interval_min)
        if res["status"] in ("OPEN", "PENDING"):
            break
        if res["status"] != "SKIPPED":
            blocked_until = res["exit_idx"] + 1
    return added


# ---------------------------------------------------------------------------
# Reading the journal
# ---------------------------------------------------------------------------
def history(limit: int = 100) -> dict:
    with _lock:
        df = _load()
    items = []
    for _, row in df.iterrows():
        r = _num(row["r"])
        ts = pd.Timestamp(row["signal_time"])
        items.append({
            "key": row["key"], "sort": ts.isoformat(),
            "time": ts.strftime("%H:%M"), "date": ts.strftime("%d %b"),
            "index": str(row["index"]).upper(), "interval": row["interval"], "side": row["side"],
            "option": row["option"], "mode": row["mode"],
            "entry": _num(row["entry"]), "stop": _num(row["stop"]), "target": _num(row["target"]),
            "status": row["status"], "label": LABELS.get(row["status"], row["status"]),
            "r_net": None if r is None else round(r - COST_R, 2),
            "bars": int(_num(row["bars"]) or 0),
            "peak_r": _num(row["peak_r"]), "dd_r": _num(row["dd_r"]),
            "story": row["story"],
            "setup": setup_line(_json(row["context"], {})),
            "room": (_json(row["context"], {}) or {}).get("room"),
            "vix": (_json(row["context"], {}) or {}).get("vix"),
            "option_levels": _json(row["option_levels"], None),
            "reasons": _json(row["reasons"], []),
            "memory": _json(row["memory"], None),
            "has_chart": os.path.exists(snapshot_path(row["key"])),
        })
    items.sort(key=lambda x: x["sort"], reverse=True)

    count = lambda label: sum(1 for x in items if x["label"] == label)
    net = [x["r_net"] for x in items if x["status"] in FINISHED and x["r_net"] is not None]
    finished = len(net)
    summary = {
        "ideas": len(items), "success": count("SUCCESS"), "failed": count("FAILED"),
        "time_exit": count("TIME EXIT"), "waiting": count("WAITING") + count("RUNNING"),
        "skipped": count("SKIPPED"),
        "win_rate": (count("SUCCESS") / finished) if finished else None,
        "total_r": round(sum(net), 2) if net else 0.0,
        "finished": finished,
    }
    return {"summary": summary, "items": items[:limit]}


def summary(limit: int = 8) -> dict:
    """Short summary for the small journal card."""
    h = history(limit)
    s = h["summary"]
    return {
        "ideas": s["ideas"], "finished": s["finished"], "targets": s["success"],
        "stops": s["failed"], "time_exits": s["time_exit"], "waiting": s["waiting"],
        "win_rate": s["win_rate"], "total_r": s["total_r"],
        "recent": [{"time": f"{x['date']} {x['time']}", "side": x["side"], "index": x["index"],
                    "status": x["status"], "r": x["r_net"]} for x in h["items"]],
    }


def load_snapshot(key: str):
    path = snapshot_path(key)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
