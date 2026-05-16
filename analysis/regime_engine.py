"""
regime_engine.py
────────────────
Spec TASK 3: Enhanced 7-state Market Regime Classification Engine.

Extends the existing regime_detector (TRENDING/RANGING/VOLATILE) with a
richer 7-state model that separates TREND_UP from TREND_DOWN, identifies
CHOPPY and MEAN_REVERSION states, and handles HIGH/LOW volatility regimes.

Feature flag: REGIME_ENGINE_ENABLED (default True).
Falls back to UNKNOWN if insufficient data — never blocks execution.

Inputs per classification:
  - ADX (trend strength)
  - VWAP position (institutional bias)
  - ATR expansion ratio (volatility acceleration)
  - RSI (momentum direction)
  - Volume expansion (participation)
  - VIX level (macro volatility)

Used by: strategy_matrix.py to decide which strategies to enable.
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

ENABLED = os.getenv("REGIME_ENGINE_ENABLED", "true").lower() != "false"
CACHE_TTL_SECONDS = 300   # re-classify every 5 minutes


class MarketRegime(str, Enum):
    TREND_UP        = "TREND_UP"
    TREND_DOWN      = "TREND_DOWN"
    BREAKOUT        = "BREAKOUT"
    CHOPPY          = "CHOPPY"
    MEAN_REVERSION  = "MEAN_REVERSION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY  = "LOW_VOLATILITY"
    UNKNOWN         = "UNKNOWN"


@dataclass
class RegimeState:
    regime:        MarketRegime = MarketRegime.UNKNOWN
    confidence:    float        = 0.0          # 0.0 – 1.0
    adx_value:     float        = 0.0
    atr_ratio:     float        = 1.0          # current ATR / 20-bar avg ATR
    vwap_position: str          = ""           # "above" / "below" / "at"
    rsi_value:     float        = 50.0
    vix_value:     float        = 0.0
    volume_ratio:  float        = 1.0          # current vol / 20-bar avg vol
    notes:         str          = ""
    timestamp:     datetime     = field(default_factory=lambda: datetime.now(tz=IST))


class RegimeEngine:
    """
    Classifies market regime from OHLCV data.

    Usage:
        from analysis.regime_engine import regime_engine, MarketRegime

        state = regime_engine.classify("NIFTY", df_1h, vix=15.0)
        if state.regime == MarketRegime.TREND_UP:
            ...
    """

    def __init__(self):
        self._cache: dict[str, RegimeState] = {}
        self._lock  = threading.Lock()

    def classify(
        self,
        symbol: str,
        df: pd.DataFrame,
        vix: float = 0.0,
        force_refresh: bool = False,
    ) -> RegimeState:
        """
        Classify regime for a symbol using its OHLCV DataFrame.
        Returns cached result if < CACHE_TTL_SECONDS old.
        """
        if not ENABLED:
            return RegimeState(regime=MarketRegime.UNKNOWN, notes="regime_engine disabled")

        with self._lock:
            cached = self._cache.get(symbol)
            if (not force_refresh and cached and
                    (datetime.now(tz=IST) - cached.timestamp).total_seconds() < CACHE_TTL_SECONDS):
                return cached

        try:
            state = self._classify(df, vix)
            state.timestamp = datetime.now(tz=IST)
            with self._lock:
                self._cache[symbol] = state
            logger.debug(
                f"[RegimeEngine] {symbol} → {state.regime.value} "
                f"(conf={state.confidence:.0%} ADX={state.adx_value:.0f} "
                f"ATR×{state.atr_ratio:.2f} VWAP:{state.vwap_position})"
            )
            return state
        except Exception as e:
            logger.warning(f"[RegimeEngine] {symbol} classification failed: {e}")
            return RegimeState(regime=MarketRegime.UNKNOWN, notes=str(e))

    def get_cached(self, symbol: str) -> Optional[RegimeState]:
        """Return last cached regime without recomputing."""
        with self._lock:
            return self._cache.get(symbol)

    # ── Classification logic ─────────────────────────────────────

    def _classify(self, df: pd.DataFrame, vix: float) -> RegimeState:
        from analysis.indicators import adx, atr, vwap, rsi, relative_volume

        if df is None or len(df) < 30:
            return RegimeState(regime=MarketRegime.UNKNOWN, notes="insufficient data (<30 bars)")

        close = df["close"]
        adx_s, plus_di, minus_di = adx(df, period=14)
        adx_val  = adx_s.iloc[-1]
        plus_val = plus_di.iloc[-1]
        minus_val= minus_di.iloc[-1]

        atr_s    = atr(df, period=14)
        atr_now  = atr_s.iloc[-1]
        atr_avg  = atr_s.iloc[-20:-1].mean() if len(atr_s) >= 20 else atr_now
        atr_ratio = round(atr_now / atr_avg, 2) if atr_avg > 0 else 1.0

        rsi_val  = rsi(close).iloc[-1]

        vwap_s   = vwap(df)
        vwap_val = vwap_s.iloc[-1]
        ltp      = close.iloc[-1]
        vwap_dist_pct = (ltp - vwap_val) / vwap_val * 100 if vwap_val > 0 else 0
        if vwap_dist_pct > 0.3:
            vwap_pos = "above"
        elif vwap_dist_pct < -0.3:
            vwap_pos = "below"
        else:
            vwap_pos = "at"

        rvol = relative_volume(df).iloc[-1]

        state = RegimeState(
            adx_value     = round(adx_val, 1),
            atr_ratio     = atr_ratio,
            vwap_position = vwap_pos,
            rsi_value     = round(rsi_val, 1),
            vix_value     = vix,
            volume_ratio  = round(rvol, 2),
        )

        # ── Regime decision tree ──────────────────────────────────
        # Priority: HIGH_VOL → LOW_VOL → TREND_UP/DOWN → BREAKOUT → MEAN_REVERSION → CHOPPY

        # HIGH_VOLATILITY: ATR expanding fast OR VIX elevated AND ADX < 30
        if (atr_ratio > 1.6 and adx_val < 30) or (vix > 20 and atr_ratio > 1.3):
            state.regime     = MarketRegime.HIGH_VOLATILITY
            state.confidence = min(0.9, 0.5 + (atr_ratio - 1.6) * 0.3)
            state.notes      = f"ATR×{atr_ratio:.2f} / VIX={vix:.1f}"
            return state

        # LOW_VOLATILITY: ATR contracting, volume quiet, ADX < 18
        if atr_ratio < 0.7 and adx_val < 18 and rvol < 0.8:
            state.regime     = MarketRegime.LOW_VOLATILITY
            state.confidence = min(0.85, 0.5 + (0.7 - atr_ratio) * 0.5)
            state.notes      = f"ATR×{atr_ratio:.2f} ADX={adx_val:.0f} RVOL={rvol:.2f}"
            return state

        # TREND_UP: strong ADX + price above VWAP + plus_DI dominates
        if adx_val > 25 and vwap_pos == "above" and plus_val > minus_val and rsi_val > 50:
            conf = min(0.95, 0.5 + (adx_val - 25) / 50 + (rsi_val - 50) / 200)
            state.regime     = MarketRegime.TREND_UP
            state.confidence = round(conf, 2)
            state.notes      = f"ADX={adx_val:.0f} +DI={plus_val:.0f} VWAP:above RSI={rsi_val:.0f}"
            return state

        # TREND_DOWN: strong ADX + price below VWAP + minus_DI dominates
        if adx_val > 25 and vwap_pos == "below" and minus_val > plus_val and rsi_val < 50:
            conf = min(0.95, 0.5 + (adx_val - 25) / 50 + (50 - rsi_val) / 200)
            state.regime     = MarketRegime.TREND_DOWN
            state.confidence = round(conf, 2)
            state.notes      = f"ADX={adx_val:.0f} -DI={minus_val:.0f} VWAP:below RSI={rsi_val:.0f}"
            return state

        # BREAKOUT: ADX rising sharply from low (< 20 → now > 22), ATR expanding, high volume
        if adx_val > 20 and atr_ratio > 1.25 and rvol > 1.3:
            # check ADX was lower 5 bars ago
            adx_5ago = adx_s.iloc[-6] if len(adx_s) >= 6 else adx_val
            if adx_val - adx_5ago > 3:
                state.regime     = MarketRegime.BREAKOUT
                state.confidence = min(0.85, 0.5 + (rvol - 1.3) * 0.2 + (adx_val - adx_5ago) / 30)
                state.notes      = f"ADX +{adx_val - adx_5ago:.0f} in 5bars RVOL={rvol:.2f}"
                return state

        # MEAN_REVERSION: low ADX + price far from VWAP (stretched and snapping back)
        if adx_val < 20 and abs(vwap_dist_pct) > 0.5:
            state.regime     = MarketRegime.MEAN_REVERSION
            state.confidence = min(0.80, 0.4 + abs(vwap_dist_pct) / 3)
            state.notes      = f"ADX={adx_val:.0f} VWAP dist={vwap_dist_pct:+.1f}%"
            return state

        # CHOPPY: everything else (low ADX, price near VWAP, no expansion)
        state.regime     = MarketRegime.CHOPPY
        state.confidence = 0.55
        state.notes      = f"ADX={adx_val:.0f} RVOL={rvol:.2f} — no clear structure"
        return state


# Singleton
regime_engine = RegimeEngine()
