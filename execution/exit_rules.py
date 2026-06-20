"""
exit_rules.py
─────────────
SINGLE SOURCE for the long-OPTION exit DECISION — when and why to exit a bought
option. One place so a change to "how we exit" applies to every engine that holds
options instead of being copied per engine.

Owns only the DECISION (+ the trailing-stop state update). Each engine keeps its own
ACTION — computing the exit fill price (live LTP vs BS-modelled) and recording the
close — because those differ by data source, not by rule.

Consumers (being migrated, U3):
  • learning_engine._check_exits   — NSE index options  ✅
  • us_reversal                    — US SPY/QQQ options  ✅
  • commodity_options._check_exits — MCX options          (slice-2)
  • execution/position_manager     — production options   (slice-3)

Stop semantics (underlying spot vs option LTP) are a PARAMETER here, not a fork.
"""
from typing import Optional

from strategies.reversal_core import ratchet_stop, bear_breakdown  # shared primitives


def underlying_exit(spot: Optional[float], entry_spot: float, sl: float, trail: float,
                    peak: float, stop: float, *, breakdown: bool = False,
                    pct: bool = False) -> tuple[Optional[str], float, float]:
    """
    Underlying-driven exit for a long option (used by Reversal trail/breakdown, MCX
    STOP_SPOT, …). Ratchets a trailing stop on the index/underlying and/or checks a
    bear-breakdown signal.

      sl, trail : initial-stop / trail distance — POINTS (pct=False) or FRACTION (pct=True).
                  trail=0 → fixed hard stop only (breakdown mode).
      peak, stop: running state (carry between cycles).
      breakdown : precomputed bear-breakdown flag (caller passes bear_breakdown(df)).

    Returns (reason|None, new_peak, new_stop). reason ∈ {STOP, TRAIL_STOP, BREAK}.
    """
    init_stop = entry_spot * (1 - sl) if pct else entry_spot - sl
    if spot and spot > 0:
        peak = max(peak, float(spot))
        stop = ratchet_stop(stop, peak, init_stop, trail, pct=pct) if trail else max(stop, init_stop)
        if spot <= stop:
            return ("TRAIL_STOP" if stop > init_stop + 1e-6 else "STOP"), peak, stop
    if breakdown:
        return "BREAK", peak, stop
    return None, peak, stop


def premium_exit(ltp: Optional[float], stop: float, target: float,
                 direction: str = "LONG") -> Optional[str]:
    """Premium-level (option-price) exit decision.

    LONG  (option-buying):  STOP when premium falls to ≤ stop, TARGET when it rises to ≥ target.
    SHORT (option-selling, e.g. strangle/iron-condor): mirrored — STOP when premium rises to
          ≥ stop (value going against us), TARGET when it falls to ≤ target.
    """
    if ltp is None or ltp <= 0:
        return None
    if direction == "LONG":
        if stop and ltp <= stop:
            return "STOP"
        if target and ltp >= target:
            return "TARGET"
    else:
        if stop and ltp >= stop:
            return "STOP"
        if target and ltp <= target:
            return "TARGET"
    return None


def spot_breached(spot: Optional[float], stop: float, direction: str = "LONG") -> bool:
    """Underlying-spot stop breach — LONG exits when spot ≤ stop, SHORT when spot ≥ stop.
    The one predicate for spot-based SL (MCX STOP_SPOT, Reversal hard stop, …)."""
    if spot is None or spot <= 0:
        return False
    return (spot <= stop) if direction == "LONG" else (spot >= stop)


# ratchet_stop + bear_breakdown are imported at top; re-export so engines have one
# import site for all option-exit primitives.
__all__ = ["underlying_exit", "premium_exit", "spot_breached", "ratchet_stop", "bear_breakdown"]
