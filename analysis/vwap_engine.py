"""
vwap_engine.py
──────────────
Spec TASK 5: VWAP Acceptance Engine.

Goes beyond "price > VWAP" to measure institutional acceptance:
  - time_above_pct     : fraction of bars price spent above VWAP
  - rejection_count    : bars where price touched VWAP from above and dropped
  - reclaim_count      : bars where price touched VWAP from below and recovered
  - distance_pct       : current distance from VWAP (signed %)
  - slope_pct          : VWAP slope over last 10 bars (% per bar)

Output state:
  VWAP_ACCEPTED  — price consistently above VWAP, institutions buying
  VWAP_REJECTED  — price consistently below VWAP, selling pressure
  VWAP_NEUTRAL   — oscillating around VWAP, no clear bias

Feature flag: VWAP_ENGINE_ENABLED (default True).
"""

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("VWAP_ENGINE_ENABLED", "true").lower() != "false"
CACHE_TTL_SECONDS = 60   # re-compute every minute (fast enough for 5m data)


class VWAPPosition(str):
    ACCEPTED = "VWAP_ACCEPTED"
    REJECTED = "VWAP_REJECTED"
    NEUTRAL  = "VWAP_NEUTRAL"


@dataclass
class VWAPState:
    position:         str    = VWAPPosition.NEUTRAL   # VWAP_ACCEPTED / REJECTED / NEUTRAL
    time_above_pct:   float  = 0.5       # 0.0 – 1.0
    rejection_count:  int    = 0         # # bars touching VWAP from above and dropping
    reclaim_count:    int    = 0         # # bars touching VWAP from below and recovering
    distance_pct:     float  = 0.0       # signed % distance from VWAP (+ = above)
    slope_pct:        float  = 0.0       # VWAP slope per bar in %
    vwap_value:       float  = 0.0
    confidence:       float  = 0.5
    notes:            str    = ""
    timestamp:        datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(tz=IST)

    def to_dict(self) -> dict:
        return {
            "position":        self.position,
            "time_above_pct":  round(self.time_above_pct * 100, 1),
            "rejection_count": self.rejection_count,
            "reclaim_count":   self.reclaim_count,
            "distance_pct":    round(self.distance_pct, 3),
            "slope_pct":       round(self.slope_pct, 4),
            "vwap_value":      round(self.vwap_value, 2),
            "confidence":      round(self.confidence, 2),
            "notes":           self.notes,
        }


class VWAPEngine:
    """
    Measures VWAP acceptance / rejection from OHLCV data.

    Usage:
        from analysis.vwap_engine import vwap_engine

        state = vwap_engine.evaluate("NIFTY", df_5m)
        if state.position == "VWAP_ACCEPTED" and state.confidence > 0.7:
            # institutional buying confirmed
    """

    def __init__(self):
        self._cache: dict[str, VWAPState] = {}
        self._lock  = threading.Lock()

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        lookback: int = 40,
        force_refresh: bool = False,
    ) -> VWAPState:
        """
        Compute VWAP acceptance for the last `lookback` bars.
        Returns cached result if fresh enough.
        """
        if not ENABLED:
            return VWAPState(position=VWAPPosition.NEUTRAL, notes="vwap_engine disabled")

        with self._lock:
            cached = self._cache.get(symbol)
            if (not force_refresh and cached and
                    (datetime.now(tz=IST) - cached.timestamp).total_seconds() < CACHE_TTL_SECONDS):
                return cached

        try:
            state = self._compute(df, lookback)
            state.timestamp = datetime.now(tz=IST)
            with self._lock:
                self._cache[symbol] = state
            logger.debug(
                f"[VWAP] {symbol} → {state.position} "
                f"(above={state.time_above_pct:.0%} dist={state.distance_pct:+.2f}% "
                f"rej={state.rejection_count} rcl={state.reclaim_count})"
            )
            return state
        except Exception as e:
            logger.debug(f"[VWAP] {symbol} compute failed: {e}")
            return VWAPState(position=VWAPPosition.NEUTRAL, notes=str(e))

    def get_cached(self, symbol: str) -> Optional[VWAPState]:
        with self._lock:
            return self._cache.get(symbol)

    # ── Computation ──────────────────────────────────────────────

    def _compute(self, df: pd.DataFrame, lookback: int) -> VWAPState:
        from analysis.indicators import vwap as calc_vwap

        if df is None or len(df) < 10:
            return VWAPState(notes="insufficient data")

        # Use last `lookback` bars
        window = df.tail(lookback).copy().reset_index(drop=True)
        vwap_s = calc_vwap(window)

        close     = window["close"]
        ltp       = close.iloc[-1]
        vwap_now  = vwap_s.iloc[-1]
        vwap_prev = vwap_s.iloc[max(0, len(vwap_s) - 11)]

        distance_pct = (ltp - vwap_now) / vwap_now * 100 if vwap_now > 0 else 0.0
        slope_pct    = (vwap_now - vwap_prev) / vwap_prev * 100 if vwap_prev > 0 else 0.0

        # Time above VWAP
        above_mask    = close > vwap_s
        time_above    = above_mask.mean()

        # Rejection: bar was above VWAP (high > vwap) but closed below vwap
        # Reclaim:   bar was below VWAP (low < vwap) but closed above vwap
        rejection_count = 0
        reclaim_count   = 0
        for i in range(1, len(window)):
            bar_high = window["high"].iloc[i]
            bar_low  = window["low"].iloc[i]
            bar_close= close.iloc[i]
            vwap_val = vwap_s.iloc[i]
            if bar_high > vwap_val and bar_close < vwap_val:
                rejection_count += 1
            if bar_low < vwap_val and bar_close > vwap_val:
                reclaim_count += 1

        # Classify position
        # ACCEPTED: >60% time above + positive slope + few rejections vs reclaims
        # REJECTED: <40% time above + negative slope
        # NEUTRAL:  everything else
        acceptance_score = (
            time_above * 0.5
            + (1 if slope_pct > 0 else 0) * 0.2
            + (reclaim_count - rejection_count) / max(1, reclaim_count + rejection_count) * 0.3
        )

        if acceptance_score > 0.60 and time_above > 0.55:
            position   = VWAPPosition.ACCEPTED
            confidence = min(0.95, 0.55 + (acceptance_score - 0.60) * 1.5)
            notes      = f"above={time_above:.0%} rcl={reclaim_count} rej={rejection_count}"
        elif acceptance_score < 0.40 and time_above < 0.45:
            position   = VWAPPosition.REJECTED
            confidence = min(0.95, 0.55 + (0.40 - acceptance_score) * 1.5)
            notes      = f"above={time_above:.0%} rej={rejection_count} rcl={reclaim_count}"
        else:
            position   = VWAPPosition.NEUTRAL
            confidence = 0.50 + abs(acceptance_score - 0.50) * 0.5
            notes      = f"oscillating — above={time_above:.0%}"

        return VWAPState(
            position        = position,
            time_above_pct  = round(float(time_above), 3),
            rejection_count = rejection_count,
            reclaim_count   = reclaim_count,
            distance_pct    = round(distance_pct, 3),
            slope_pct       = round(slope_pct, 4),
            vwap_value      = round(vwap_now, 2),
            confidence      = round(confidence, 2),
            notes           = notes,
        )


# Singleton
vwap_engine = VWAPEngine()
