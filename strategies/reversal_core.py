"""
reversal_core.py
────────────────
SINGLE SOURCE OF TRUTH for the Reversal strategy's entry pattern, exit signal, and
trailing-stop maths.

Imported by EVERY consumer so the logic can never silently diverge:
  • strategies/reversal_5m.py        — live NSE index (Reversal5m / Reversal3m)
  • us_reversal.py                   — live US index ETFs (SPY/QQQ)
  • learning_engine._check_exits     — the NSE option exits (trail + breakdown)
  • scripts/backtest_reversal_*.py   — the backtests

The pattern/exit DECISION is exposed both as scalar predicates (for the vectorised
backtests — fast) and as DataFrame helpers (for the live strategies). Both share the
same predicate, so tuning a threshold here changes backtest + India + US at once.
"""
from typing import Optional

import pandas as pd

from analysis.indicators import rsi as _rsi, relative_volume as _rvol

# ── Tunable thresholds — change here, changes everywhere ──────────
RSI_LOW  = 30.0
RSI_HIGH = 70.0
MIN_RVOL = 1.2
MIN_BARS = 30


# ── Scalar predicates (the actual logic) ─────────────────────────
def is_reclaim(o_prev, c_prev, o_now, c_now,
               rsi_now, rsi_prev, rvol_now, vol_present,
               rsi_low=RSI_LOW, rsi_high=RSI_HIGH, min_rvol=MIN_RVOL) -> bool:
    """Bullish reversal: red candle, then a green candle closing above the red's OPEN,
    RSI inside (low, high) AND rising, volume above-average (fail-open if no volume)."""
    return bool(
        c_prev < o_prev and c_now > o_now and c_now > o_prev
        and rsi_low < rsi_now < rsi_high and rsi_now > rsi_prev
        and ((not vol_present) or rvol_now >= min_rvol)
    )


def is_breakdown(o_now, c_now, low_prev) -> bool:
    """Bear breakdown (long EXIT): a red candle closing below the prior candle's low."""
    return bool(c_now < o_now and c_now < low_prev)


def ratchet_stop(prev_stop: float, peak: float, init_stop: float,
                 trail: float, pct: bool = False, long: bool = True) -> float:
    """Trailing stop that only moves in the favourable direction. trail = points
    (pct=False) or fraction (pct=True) from the running peak/trough. LONG: stop
    ratchets UP, never below init_stop. SHORT: stop ratchets DOWN, never above
    init_stop. (peak is the favourable extreme — highest for long, lowest for short.)"""
    if long:
        trail_level = peak * (1 - trail) if pct else peak - trail
        return max(prev_stop, trail_level, init_stop)
    trail_level = peak * (1 + trail) if pct else peak + trail
    return min(prev_stop, trail_level, init_stop)


# ── DataFrame helpers (convenience for the live strategies) ──────
def bullish_reclaim(df: pd.DataFrame, rsi_low=RSI_LOW, rsi_high=RSI_HIGH,
                    min_rvol=MIN_RVOL) -> tuple[bool, dict]:
    """Evaluate the last two CLOSED candles of df. Returns (ok, context_dict)."""
    if df is None or len(df) < MIN_BARS:
        return False, {}
    o, c = df["open"], df["close"]
    r = _rsi(c)
    rn, rp = float(r.iloc[-1]), float(r.iloc[-2])
    vol_present = df["volume"].sum() > 0
    rv = float(_rvol(df).iloc[-1])
    ok = is_reclaim(float(o.iloc[-2]), float(c.iloc[-2]), float(o.iloc[-1]), float(c.iloc[-1]),
                    rn, rp, rv, vol_present, rsi_low, rsi_high, min_rvol)
    ctx = {"rsi_now": round(rn, 1), "rsi_prev": round(rp, 1),
           "rvol": round(rv, 2), "vol_present": vol_present}
    return ok, ctx


def bear_breakdown(df: pd.DataFrame) -> bool:
    """Long-exit breakdown on the last two candles of df."""
    if df is None or len(df) < 2:
        return False
    o, c, l = df["open"], df["close"], df["low"]
    return is_breakdown(float(o.iloc[-1]), float(c.iloc[-1]), float(l.iloc[-2]))
