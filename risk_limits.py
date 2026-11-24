"""
risk_limits.py
--------------
Optional risk helpers. Everything is OFF until you set the values in .env (see .env.example):

  RISK_MAX_LOSS_R=3            stop making NEW ideas for the day after losing this many R (net, finished ideas)
  RISK_MAX_IDEAS_PER_DAY=4     stop making NEW ideas for the day after this many ideas
  RISK_CAPITAL=200000          your trading money in rupees        } together they switch on the
  RISK_PER_TRADE_PCT=1         the share of it you accept to lose  } position-size helper

What a limit does: a new idea is still saved in the journal, but as SKIPPED ("Daily limit ..."), and no
Telegram alert is sent for it. The dashboard says so. The position-size helper only SHOWS how many
lots fit your risk. Nothing here places an order.
"""

import math
import os


def _number(name: str, env: dict):
    text = str(env.get(name, "")).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        print(f"[risk] {name}='{text}' is not a number, ignored")
        return None
    return value if value > 0 else None


def settings(env=None) -> dict:
    env = os.environ if env is None else env
    ideas = _number("RISK_MAX_IDEAS_PER_DAY", env)
    return {
        "max_loss_r": _number("RISK_MAX_LOSS_R", env),
        "max_ideas": int(ideas) if ideas else None,
        "capital": _number("RISK_CAPITAL", env),
        "risk_pct": _number("RISK_PER_TRADE_PCT", env),
    }


def limits_on(cfg: dict) -> bool:
    return bool(cfg["max_loss_r"] or cfg["max_ideas"])


def sizing_on(cfg: dict) -> bool:
    return bool(cfg["capital"] and cfg["risk_pct"])


def check(stats: dict, cfg: dict):
    """The reason a NEW idea must be skipped today, or None. stats = {"ideas": n, "net_r": R}."""
    if cfg["max_ideas"] and stats["ideas"] >= cfg["max_ideas"]:
        return f"Daily limit: {stats['ideas']} ideas already today (maximum {cfg['max_ideas']})"
    if cfg["max_loss_r"] and stats["net_r"] <= -cfg["max_loss_r"]:
        return f"Daily limit: {stats['net_r']:+.1f}R today (maximum loss {cfg['max_loss_r']:g}R)"
    return None


def day_status(stats: dict, cfg: dict) -> dict:
    """What the dashboard shows about today."""
    return {
        "enabled": limits_on(cfg), "max_loss_r": cfg["max_loss_r"], "max_ideas": cfg["max_ideas"],
        "ideas": stats["ideas"], "net_r": round(stats["net_r"], 2), "reached": check(stats, cfg),
    }


def position_size(cfg: dict, loss_per_unit, lot_size):
    """
    How many lots fit the risk budget. loss_per_unit = rupees lost per option unit at the stop loss.
    Returns None when the helper is off or a number is missing.
    """
    if not sizing_on(cfg) or not loss_per_unit or loss_per_unit <= 0 or not lot_size:
        return None
    budget = cfg["capital"] * cfg["risk_pct"] / 100.0
    per_lot = loss_per_unit * lot_size
    lots = int(math.floor(budget / per_lot))
    note = (f"{lots} lot(s) keep the loss at the stop within your budget" if lots
            else f"Even 1 lot loses about {per_lot:,.0f} at the stop, more than your budget of {budget:,.0f}")
    return {"budget": round(budget), "capital": cfg["capital"], "risk_pct": cfg["risk_pct"], "lot_size": int(lot_size),
            "loss_per_lot": round(per_lot), "lots": lots, "note": note}
