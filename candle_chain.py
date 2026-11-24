"""
candle_chain.py
---------------
Turns saved / live option open interest (OI) into plain facts for the dashboard card:
put/call ratio, the strikes with the most call and put OI, how OI changed since the previous
session, and where the index stands between them.

It only EXPLAINS. No trade rule uses it (it is tested as a possible filter later, section 3b of the README).
"""

import numpy as np
import pandas as pd

LOOKAROUND = "Facts only. Not used in the trade decision."


def _last_by_day(df: pd.DataFrame) -> dict:
    """{day: {expiry: {(strike, side): last OI of that day}}} from the saved option table."""
    out = {}
    if df is None or df.empty:
        return out
    d = df.dropna(subset=["oi"])
    for (day, expiry, strike, side), g in d.groupby([d.index.date, "expiry", "strike", "side"]):
        out.setdefault(day, {}).setdefault(expiry, {})[(float(strike), side)] = float(g["oi"].iloc[-1])
    return out


def saved_snapshot(df: pd.DataFrame):
    """
    Latest saved day: (expiry, {(strike, side): oi}, {same for the day before} , "dd Mon" of that day).
    None when nothing is saved. The expiry is the nearest one on that day.
    """
    days = _last_by_day(df)
    if not days:
        return None
    day = max(days)
    expiry = min(days[day])
    earlier = [x for x in days if x < day and expiry in days[x]]
    prev = days[max(earlier)][expiry] if earlier else {}
    return expiry, days[day][expiry], prev, f"{day:%d %b}"


def previous_session(df: pd.DataFrame, expiry: str, today) -> dict:
    """Last saved OI per contract on the latest day BEFORE `today` for this expiry (empty if none)."""
    days = _last_by_day(df)
    earlier = [x for x in days if x < today and expiry in days[x]]
    return days[max(earlier)][expiry] if earlier else {}


def _pct(now: float, before: float):
    return None if not before else round((now - before) / before * 100, 1)


def summarize(current: dict, previous: dict, spot: float, expiry: str, source: str, as_of: str) -> dict:
    """
    current / previous: {(strike, side): oi}. Returns the dict the dashboard card shows
    (rows around the index level, totals, put/call ratio, walls, plain-words facts).
    """
    strikes = sorted({k[0] for k in current})
    if not strikes:
        return None
    rows = []
    for k in strikes:
        row = {"strike": k}
        for side, name in (("CE", "ce"), ("PE", "pe")):
            oi = current.get((k, side))
            before = previous.get((k, side))
            row[name] = oi
            row[name + "_chg"] = None if oi is None or before is None else oi - before
        rows.append(row)

    calls = sum(v for (k, s), v in current.items() if s == "CE")
    puts = sum(v for (k, s), v in current.items() if s == "PE")
    pcr = round(puts / calls, 2) if calls else None
    ce_prev = sum(v for (k, s), v in previous.items() if s == "CE" and (k, "CE") in current)
    pe_prev = sum(v for (k, s), v in previous.items() if s == "PE" and (k, "PE") in current)
    calls_chg = _pct(calls, ce_prev) if previous else None
    puts_chg = _pct(puts, pe_prev) if previous else None

    def wall(side):
        cand = [(v, k) for (k, s), v in current.items() if s == side]
        if not cand:
            return None
        oi, k = max(cand)
        before = previous.get((k, side))
        return {"strike": k, "oi": oi, "chg": None if before is None else oi - before}

    call_wall, put_wall = wall("CE"), wall("PE")

    facts = []
    if pcr is not None:
        facts.append(f"Put/call OI ratio {pcr:.2f}: " + ("more put OI than call OI." if pcr > 1 else "more call OI than put OI."))
    if call_wall:
        facts.append(f"Most call OI at {call_wall['strike']:.0f} (often watched as resistance).")
    if put_wall:
        facts.append(f"Most put OI at {put_wall['strike']:.0f} (often watched as support).")
    if call_wall and put_wall and spot:
        up, down = call_wall["strike"] - spot, spot - put_wall["strike"]
        if up >= 0 and down >= 0:
            facts.append(f"The index is {down:.0f} points above the put wall and {up:.0f} below the call wall.")
        elif up < 0:
            facts.append(f"The index is {-up:.0f} points ABOVE the call wall.")
        else:
            facts.append(f"The index is {-down:.0f} points BELOW the put wall.")
    if calls_chg is not None and puts_chg is not None:
        facts.append(f"Since the previous session: call OI {calls_chg:+.1f}%, put OI {puts_chg:+.1f}% "
                     "(only the strikes shown).")
    facts.append(LOOKAROUND)

    return {"expiry": expiry, "source": source, "as_of": as_of, "spot": spot, "pcr": pcr,
            "calls": calls, "puts": puts, "calls_chg_pct": calls_chg, "puts_chg_pct": puts_chg,
            "call_wall": call_wall, "put_wall": put_wall, "rows": rows, "facts": facts}


# ---------------------------------------------------------------------------
# Open-interest FEATURES per candle (for research; used by the optional oi_* checks)
# ---------------------------------------------------------------------------
OI_FLOW_LOOKBACK_BARS = 6        # 6 five-minute bars = 30 minutes
OI_MIN_STRIKES = 6               # fewer strikes on a side at one moment = not reliable, leave empty
OI_TOLERANCE = pd.Timedelta(minutes=20)
OI_COLUMNS = ["oi_pcr", "oi_call_wall", "oi_put_wall", "oi_flow"]


def _bar_features(chain: pd.DataFrame) -> pd.DataFrame:
    """One row per saved 5-minute bar: put/call ratio, the two walls, and the put-minus-call OI build-up."""
    empty = pd.DataFrame(columns=OI_COLUMNS, index=pd.DatetimeIndex([], tz=chain.index.tz))
    d = chain.dropna(subset=["oi"])
    if d.empty:
        return empty
    d = d[d["expiry"] == d.groupby(d.index.date)["expiry"].transform("min")]     # nearest expiry of each day
    wide = d.pivot_table(index=d.index, columns=["strike", "side"], values="oi", aggfunc="last").sort_index()
    ce, pe = wide.xs("CE", level="side", axis=1), wide.xs("PE", level="side", axis=1)
    n_ce, n_pe = ce.notna().sum(axis=1), pe.notna().sum(axis=1)
    good = (n_ce >= OI_MIN_STRIKES) & (n_pe >= OI_MIN_STRIKES) & (n_ce == n_pe)

    out = pd.DataFrame(index=wide.index, columns=OI_COLUMNS, dtype=float)
    out["oi_pcr"] = (pe.sum(axis=1) / ce.sum(axis=1).replace(0, np.nan)).where(good)
    out["oi_call_wall"] = ce.fillna(-1).idxmax(axis=1).astype(float).where(good)
    out["oi_put_wall"] = pe.fillna(-1).idxmax(axis=1).astype(float).where(good)

    # build-up over the last 30 minutes, on the contracts present at both moments, same day only
    k = OI_FLOW_LOOKBACK_BARS
    same = pd.Series(wide.index.date, index=wide.index) == pd.Series(wide.index.date, index=wide.index).shift(k)
    gap = (pd.Series(wide.index, index=wide.index) - pd.Series(wide.index, index=wide.index).shift(k)) == pd.Timedelta(minutes=5 * k)
    ok = same & gap & good
    d_ce, d_pe = ce - ce.shift(k), pe - pe.shift(k)
    base = (ce.shift(k).where(d_ce.notna()).sum(axis=1) + pe.shift(k).where(d_pe.notna()).sum(axis=1)).replace(0, np.nan)
    out["oi_flow"] = ((d_pe.sum(axis=1) - d_ce.sum(axis=1)) / base).where(ok)
    return out


def oi_features(chain: pd.DataFrame, candle_index: pd.DatetimeIndex, interval_min: int) -> pd.DataFrame:
    """
    The OI features known when each candle has CLOSED, aligned to `candle_index` (candle START times).
    A saved 5-minute OI bar labelled t is complete at t + 5 minutes, so a candle that starts at s and
    closes at s + interval can use bars up to s + interval - 5. Candles without a recent bar are empty (NaN).
    oi_pcr   : put OI / call OI.   oi_call_wall / oi_put_wall : strike with the most call / put OI.
    oi_flow  : (put OI build-up - call OI build-up over 30 min) / OI 30 min ago. Positive = more puts written
               than calls (often read as bullish).
    """
    if chain is None or len(chain) == 0:
        return pd.DataFrame(np.nan, index=candle_index, columns=OI_COLUMNS)
    bars = _bar_features(chain)
    if bars.empty:
        return pd.DataFrame(np.nan, index=candle_index, columns=OI_COLUMNS)
    key = candle_index + pd.Timedelta(minutes=interval_min - 5)
    out = bars.reindex(bars.index.union(key)).sort_index().ffill(limit=None)       # latest bar at or before the key
    seen = pd.Series(bars.index, index=bars.index).reindex(out.index).ffill()
    fresh = (out.index.to_series() - seen) <= OI_TOLERANCE
    out.loc[~fresh.values, :] = np.nan
    out = out.reindex(key)
    out.index = candle_index
    return out[OI_COLUMNS]
