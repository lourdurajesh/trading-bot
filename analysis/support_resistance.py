"""
support_resistance.py
─────────────────────
SINGLE SOURCE for swing-pivot support/resistance levels and the entry-quality
decision that uses them. A trend/breakdown entry is only worth taking if there is
ROOM to the next level — shorting right onto a support (or buying into resistance),
especially when momentum is already exhausted, just catches the bounce.

Used by the directional strategies' entry logic (ShortTrend, TrendFollow, MCX
spreads) so "is there room / am I tagging a level?" is decided in ONE place.
Config: config/entry_filters.json.
"""
import json
import os
from typing import Optional

import pandas as pd

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "config", "entry_filters.json")


def _load_cfg() -> dict:
    try:
        with open(_CFG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False}


_CFG = _load_cfg()


def config() -> dict:
    return _CFG


def _pivots(df: pd.DataFrame, window: int, lookback: int):
    """Return (pivot_lows, pivot_highs) price lists over the last `lookback` bars.
    A pivot low = a bar whose low is the minimum of [i-window, i+window]; pivot high
    mirrors with highs. The window on each side makes them swing points, not noise."""
    if df is None or len(df) < 2 * window + 2:
        return [], []
    d = df.tail(lookback)
    lows, highs = d["low"].values, d["high"].values
    n = len(d)
    p_low, p_high = [], []
    for i in range(window, n - window):
        seg_lo = lows[i - window:i + window + 1]
        seg_hi = highs[i - window:i + window + 1]
        if lows[i] == seg_lo.min():
            p_low.append(float(lows[i]))
        if highs[i] == seg_hi.max():
            p_high.append(float(highs[i]))
    return p_low, p_high


def nearest_support(df: pd.DataFrame, price: float, cfg: Optional[dict] = None) -> Optional[float]:
    """Highest pivot-low strictly below `price` (the support the price would fall to)."""
    c = cfg or _CFG
    lows, _ = _pivots(df, int(c.get("pivot_window", 3)), int(c.get("level_lookback", 120)))
    below = [lv for lv in lows if lv < price]
    return max(below) if below else None


def nearest_resistance(df: pd.DataFrame, price: float, cfg: Optional[dict] = None) -> Optional[float]:
    """Lowest pivot-high strictly above `price` (the resistance the price would rise to)."""
    c = cfg or _CFG
    _, highs = _pivots(df, int(c.get("pivot_window", 3)), int(c.get("level_lookback", 120)))
    above = [lv for lv in highs if lv > price]
    return min(above) if above else None


def entry_blocked_reason(df: pd.DataFrame, direction: str, entry: float, target: float,
                         rsi_val: float, atr_val: float,
                         cfg: Optional[dict] = None) -> Optional[str]:
    """Return a reason string if this directional entry should be SKIPPED on
    support/resistance grounds, else None.

    SHORT is blocked when the next support below sits ABOVE the target (no room to
    fall — the move would stall there), OR when price is exhausted (RSI ≤ floor) and
    tagging that support (bounce zone). LONG mirrors against resistance.
    `df` should be a higher-timeframe frame (daily/1H) for significant levels.
    """
    c = cfg or _CFG
    if not c.get("enabled", False) or df is None or atr_val <= 0:
        return None
    buf = entry * float(c.get("level_buffer_pct", 0.4)) / 100.0
    min_room = float(c.get("min_room_atr", 1.0)) * atr_val
    long = (direction or "LONG").upper() == "LONG"

    if not long:  # SHORT
        sup = nearest_support(df, entry, c)
        if sup is None:
            return None
        # No room: the support sits above the target → the drop stalls before target.
        if sup > target:
            return (f"no downside room — support {sup:.2f} is above target {target:.2f}")
        # Exhausted into support: oversold AND price within a buffer of the support.
        if rsi_val <= float(c.get("oversold_floor", 40.0)) and (entry - sup) <= max(buf, min_room):
            return (f"oversold {rsi_val:.0f} tagging support {sup:.2f} — bounce zone")
    else:  # LONG
        res = nearest_resistance(df, entry, c)
        if res is None:
            return None
        if res < target:
            return (f"no upside room — resistance {res:.2f} is below target {target:.2f}")
        if rsi_val >= float(c.get("overbought_ceiling", 60.0)) and (res - entry) <= max(buf, min_room):
            return (f"overbought {rsi_val:.0f} tagging resistance {res:.2f} — rejection zone")
    return None


__all__ = ["nearest_support", "nearest_resistance", "entry_blocked_reason", "config"]
