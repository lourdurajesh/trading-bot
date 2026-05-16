"""
iv_percentile.py
────────────────
Spec TASK 7: IV Percentile Engine.

Wraps the existing options_engine.get_iv_rank() and adds:
  - iv_percentile  (% of days in history where IV was LOWER than today)
  - volatility_regime  (LOW_VOL / NORMAL_VOL / HIGH_VOL / EXTREME_VOL)
  - iv_context snapshot for audit logging

The existing iv_rank (IV Rank) and iv_percentile are different:
  IV Rank        = (current - 52w_low) / (52w_high - 52w_low) × 100
  IV Percentile  = % of days where IV < today's IV (percentile of distribution)

IVContext objects are cached per symbol for 15 minutes (same as options chain cache).

Feature flag: IV_PERCENTILE_ENABLED (default True).
"""

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("IV_PERCENTILE_ENABLED", "true").lower() != "false"
CACHE_TTL_SECONDS = 900   # 15 minutes


class VolatilityRegime(str):
    LOW_VOL     = "LOW_VOL"
    NORMAL_VOL  = "NORMAL_VOL"
    HIGH_VOL    = "HIGH_VOL"
    EXTREME_VOL = "EXTREME_VOL"


@dataclass
class IVContext:
    symbol:            str
    current_iv:        float    # current implied volatility (annualised decimal e.g. 0.18)
    iv_rank:           float    # 0–100 (existing options_engine calculation)
    iv_percentile:     float    # 0–100 (% of history days where IV < today)
    iv_52w_high:       float
    iv_52w_low:        float
    history_days:      int
    volatility_regime: str      # LOW_VOL / NORMAL_VOL / HIGH_VOL / EXTREME_VOL
    timestamp:         datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(tz=IST)

    def to_dict(self) -> dict:
        return {
            "symbol":            self.symbol,
            "current_iv":        round(self.current_iv, 4),
            "iv_rank":           round(self.iv_rank, 1),
            "iv_percentile":     round(self.iv_percentile, 1),
            "iv_52w_high":       round(self.iv_52w_high, 4),
            "iv_52w_low":        round(self.iv_52w_low, 4),
            "history_days":      self.history_days,
            "volatility_regime": self.volatility_regime,
            "timestamp":         self.timestamp.isoformat(),
        }


class IVPercentileEngine:
    """
    Computes and caches IV context for instruments.

    Usage:
        from analysis.iv_percentile import iv_percentile_engine

        ctx = iv_percentile_engine.get_context("CRUDEOIL", current_iv=0.35)
        if ctx.volatility_regime == "HIGH_VOL":
            # expand stop loss
    """

    def __init__(self):
        self._cache: dict[str, IVContext] = {}
        self._lock  = threading.Lock()

    def get_context(
        self,
        symbol:     str,
        current_iv: float,
        force_refresh: bool = False,
    ) -> IVContext:
        """
        Returns IVContext for symbol.
        Uses existing options_engine.get_iv_rank() for rank;
        adds iv_percentile from the same history.
        """
        if not ENABLED:
            return self._neutral(symbol, current_iv)

        with self._lock:
            cached = self._cache.get(symbol)
            if (not force_refresh and cached and
                    (datetime.now(tz=IST) - cached.timestamp).total_seconds() < CACHE_TTL_SECONDS):
                return cached

        ctx = self._compute(symbol, current_iv)
        with self._lock:
            self._cache[symbol] = ctx
        return ctx

    # ── Internal ─────────────────────────────────────────────────

    def _compute(self, symbol: str, current_iv: float) -> IVContext:
        try:
            from analysis.options_engine import options_engine

            # Get IV rank from existing engine
            iv_rank = options_engine.get_iv_rank(symbol)

            # Get history for percentile calculation
            history = options_engine._iv_history.get(symbol, [])
            if len(history) >= 5:
                iv_vals = [h["iv"] for h in history if isinstance(h, dict) and "iv" in h]
            else:
                iv_vals = []

            if iv_vals:
                iv_52w_high  = max(iv_vals)
                iv_52w_low   = min(iv_vals)
                iv_percentile = round(sum(1 for v in iv_vals if v < current_iv) / len(iv_vals) * 100, 1)
                history_days  = len(iv_vals)
            else:
                # VIX proxy fallback — no history yet
                iv_52w_high   = current_iv * 1.5
                iv_52w_low    = current_iv * 0.6
                iv_percentile = iv_rank   # best estimate
                history_days  = 0

            regime = self._classify_regime(iv_percentile)
            return IVContext(
                symbol            = symbol,
                current_iv        = current_iv,
                iv_rank           = round(iv_rank, 1),
                iv_percentile     = iv_percentile,
                iv_52w_high       = iv_52w_high,
                iv_52w_low        = iv_52w_low,
                history_days      = history_days,
                volatility_regime = regime,
            )
        except Exception as e:
            logger.debug(f"[IVPct] {symbol} compute failed: {e}")
            return self._neutral(symbol, current_iv)

    @staticmethod
    def _classify_regime(iv_percentile: float) -> str:
        """Bucket IV percentile into a named volatility regime."""
        if iv_percentile >= 80:
            return VolatilityRegime.EXTREME_VOL
        if iv_percentile >= 55:
            return VolatilityRegime.HIGH_VOL
        if iv_percentile >= 30:
            return VolatilityRegime.NORMAL_VOL
        return VolatilityRegime.LOW_VOL

    @staticmethod
    def _neutral(symbol: str, current_iv: float) -> IVContext:
        return IVContext(
            symbol            = symbol,
            current_iv        = current_iv,
            iv_rank           = 50.0,
            iv_percentile     = 50.0,
            iv_52w_high       = current_iv,
            iv_52w_low        = current_iv,
            history_days      = 0,
            volatility_regime = VolatilityRegime.NORMAL_VOL,
        )


# Singleton
iv_percentile_engine = IVPercentileEngine()
