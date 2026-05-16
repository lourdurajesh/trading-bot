"""
breadth_engine.py
─────────────────
Spec TASK 4: Breadth Engine.

Measures true market participation by tracking which symbols are advancing
vs declining relative to their previous close.

Outputs:
  advance_decline_ratio  — advancing / declining (>1 = bullish, <1 = bearish)
  volume_breadth_score   — volume-weighted A/D ratio
  breadth_score          — 0–10 composite (5 = neutral, >7 = bullish, <3 = bearish)

Data source: live LTPs from data_store vs daily open prices.
Updates on every call (no caching — called from slow loop, 60s cadence).

Feature flag: BREADTH_ENGINE_ENABLED (default True).
Fails gracefully — returns neutral (5.0) if data unavailable.
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("BREADTH_ENGINE_ENABLED", "true").lower() != "false"


@dataclass
class BreadthState:
    advance_count:         int   = 0
    decline_count:         int   = 0
    unchanged_count:       int   = 0
    advance_decline_ratio: float = 1.0       # A / D; 1.0 = neutral
    volume_breadth_score:  float = 5.0       # 0–10
    breadth_score:         float = 5.0       # 0–10 composite
    sample_size:           int   = 0         # symbols measured
    interpretation:        str   = "NEUTRAL" # BULLISH / BEARISH / NEUTRAL
    timestamp:             datetime = field(default_factory=lambda: datetime.now(tz=IST))

    def to_dict(self) -> dict:
        return {
            "advance_count":         self.advance_count,
            "decline_count":         self.decline_count,
            "unchanged_count":       self.unchanged_count,
            "advance_decline_ratio": round(self.advance_decline_ratio, 2),
            "volume_breadth_score":  round(self.volume_breadth_score, 1),
            "breadth_score":         round(self.breadth_score, 1),
            "sample_size":           self.sample_size,
            "interpretation":        self.interpretation,
            "timestamp":             self.timestamp.isoformat(),
        }


class BreadthEngine:
    """
    Computes market breadth from live data store LTPs.

    Usage:
        from analysis.breadth_engine import breadth_engine
        from data.data_store import store

        state = breadth_engine.update(store)
        if state.breadth_score < 3.0:
            # weak breadth — reduce position sizes
    """

    def __init__(self):
        self._lock         = threading.Lock()
        self._day_opens:   dict[str, float] = {}   # symbol → day-open price
        self._tracked_date: Optional[date]  = None
        self._last_state:  Optional[BreadthState] = None

    def update(self, store) -> BreadthState:
        """
        Compute breadth from current LTPs vs day open.
        Call this from the slow evaluation loop (~60s cadence).
        """
        if not ENABLED:
            return BreadthState(breadth_score=5.0, interpretation="NEUTRAL")
        try:
            return self._compute(store)
        except Exception as e:
            logger.debug(f"[Breadth] update() failed: {e}")
            return BreadthState(breadth_score=5.0, interpretation="NEUTRAL")

    def get_last(self) -> Optional[BreadthState]:
        with self._lock:
            return self._last_state

    # ── Internal ─────────────────────────────────────────────────

    def _compute(self, store) -> BreadthState:
        with self._lock:
            today = date.today()
            # Reset day opens at start of each new trading day
            if self._tracked_date != today:
                self._day_opens     = {}
                self._tracked_date  = today

        symbols = store.get_active_symbols()
        if not symbols:
            return BreadthState(breadth_score=5.0, interpretation="NEUTRAL")

        advance = decline = unchanged = 0
        advance_vol = decline_vol = 1.0   # start at 1 to avoid div-by-zero

        for sym in symbols:
            ltp = store.get_ltp(sym)
            if not ltp or ltp <= 0:
                continue

            # Seed day open on first tick of the day
            with self._lock:
                if sym not in self._day_opens:
                    self._day_opens[sym] = ltp
                day_open = self._day_opens[sym]

            if day_open <= 0:
                continue

            change_pct = (ltp - day_open) / day_open * 100

            # Get volume from latest candle
            df = store.get_ohlcv(sym, "1m", n=1)
            vol = float(df["volume"].iloc[-1]) if df is not None and len(df) > 0 else 1.0

            if change_pct > 0.15:
                advance      += 1
                advance_vol  += vol
            elif change_pct < -0.15:
                decline      += 1
                decline_vol  += vol
            else:
                unchanged    += 1

        total = advance + decline
        if total == 0:
            return BreadthState(sample_size=len(symbols), breadth_score=5.0)

        ad_ratio        = advance / max(decline, 1)
        vol_breadth_raw = advance_vol / (advance_vol + decline_vol)
        vol_breadth_score = round(vol_breadth_raw * 10, 1)

        # Composite score: A/D ratio mapped to 0–10 (5 = 1:1)
        # ad_ratio 2.0 → score ~8, 0.5 → score ~2
        import math
        breadth_score = round(5.0 + math.log(max(ad_ratio, 0.01)) * 2.5, 1)
        breadth_score = max(0.0, min(10.0, breadth_score))

        # Blend with volume breadth
        breadth_score = round((breadth_score * 0.6 + vol_breadth_score * 0.4), 1)

        if breadth_score >= 6.5:
            interpretation = "BULLISH"
        elif breadth_score <= 3.5:
            interpretation = "BEARISH"
        else:
            interpretation = "NEUTRAL"

        state = BreadthState(
            advance_count         = advance,
            decline_count         = decline,
            unchanged_count       = unchanged,
            advance_decline_ratio = round(ad_ratio, 2),
            volume_breadth_score  = vol_breadth_score,
            breadth_score         = breadth_score,
            sample_size           = len(symbols),
            interpretation        = interpretation,
        )
        with self._lock:
            self._last_state = state
        return state


# Singleton
breadth_engine = BreadthEngine()
