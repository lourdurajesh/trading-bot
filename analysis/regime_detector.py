"""
regime_detector.py
──────────────────
Classifies the current market regime for each symbol.
The regime determines which strategy module gets activated.

Regimes:
    TRENDING   → trend_follow.py is active
    RANGING    → mean_reversion.py + options_income.py active
    VOLATILE   → directional_options.py active, equity strategies paused
    BREAKOUT   → trend_follow.py with tighter stops, higher confidence
    UNKNOWN    → insufficient data, no strategies fire

Re-evaluates every 15 minutes per symbol (configurable).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Optional

import pandas as pd

from analysis.indicators import adx, atr, bollinger_width, ema, ema_slope, rsi
from data.data_store import store

logger = logging.getLogger(__name__)

# Re-evaluation interval in minutes
REGIME_REFRESH_MINUTES = 15


class Regime(str, Enum):
    TRENDING  = "TRENDING"
    RANGING   = "RANGING"
    VOLATILE  = "VOLATILE"
    BREAKOUT  = "BREAKOUT"
    UNKNOWN   = "UNKNOWN"


@dataclass
class RegimeResult:
    regime:     Regime
    confidence: float           # 0.0 – 1.0
    adx_value:  float
    bb_width:   float
    atr_pct:    float           # ATR as % of price
    rsi_value:  float
    slope:      float           # EMA(20) slope %
    timestamp:  datetime
    notes:      str = ""


class RegimeDetector:
    """
    Classifies market regime per symbol using multiple indicators.

    Usage:
        detector = RegimeDetector()
        result   = detector.get_regime("NSE:RELIANCE-EQ", timeframe="1H")
        print(result.regime)   # Regime.TRENDING
    """

    def __init__(self):
        # Cache: symbol_tf → (RegimeResult, evaluated_at)
        self._cache: dict[str, tuple[RegimeResult, datetime]] = {}

    # ─────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────

    def get_regime(
        self,
        symbol: str,
        timeframe: str = "1H",
        force_refresh: bool = False,
    ) -> RegimeResult:
        """
        Returns the current regime for a symbol/timeframe.
        Uses cache unless stale (> REGIME_REFRESH_MINUTES old).
        """
        cache_key = f"{symbol}_{timeframe}"
        now = datetime.now(tz=IST)

        if not force_refresh and cache_key in self._cache:
            cached_result, cached_at = self._cache[cache_key]
            age_minutes = (now - cached_at).total_seconds() / 60
            if age_minutes < REGIME_REFRESH_MINUTES:
                return cached_result

        result = self._classify(symbol, timeframe)
        self._cache[cache_key] = (result, now)
        return result

    def get_all_regimes(self, symbols: list[str], timeframe: str = "1H") -> dict[str, RegimeResult]:
        """Classify regime for a list of symbols. Returns dict symbol → RegimeResult."""
        return {sym: self.get_regime(sym, timeframe) for sym in symbols}

    def is_tradeable(self, symbol: str, timeframe: str = "1H") -> bool:
        """Returns True if the regime is not UNKNOWN (sufficient data + clear signal)."""
        result = self.get_regime(symbol, timeframe)
        return result.regime != Regime.UNKNOWN and result.confidence >= 0.5

    # ─────────────────────────────────────────────────────────────
    # INTERNAL — classification logic
    # ─────────────────────────────────────────────────────────────

    def _classify(self, symbol: str, timeframe: str) -> RegimeResult:
        """Core classification logic. Runs fresh on every cache miss."""

        df = store.get_ohlcv(symbol, timeframe, n=200)

        if df is None or len(df) < 50:
            return RegimeResult(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                adx_value=0, bb_width=0, atr_pct=0, rsi_value=50, slope=0,
                timestamp=datetime.now(tz=IST),
                notes="Insufficient data",
            )

        close = df["close"]
        ltp   = close.iloc[-1]

        # ── Compute indicators ────────────────────────────────────
        adx_series, plus_di, minus_di = adx(df)
        adx_val  = adx_series.iloc[-1]
        plus_val = plus_di.iloc[-1]
        minus_val = minus_di.iloc[-1]

        bb_w    = bollinger_width(close).iloc[-1]
        atr_val = atr(df).iloc[-1]
        atr_pct = (atr_val / ltp) * 100 if ltp > 0 else 0

        rsi_val  = rsi(close).iloc[-1]
        slope_val = ema_slope(close, period=20, lookback=5).iloc[-1]

        # ── Classification rules ──────────────────────────────────

        # VOLATILE: very high ATR % + extreme RSI
        if atr_pct > 3.0 or rsi_val > 85 or rsi_val < 15:
            confidence = self._confidence([
                atr_pct > 3.0,
                rsi_val > 80 or rsi_val < 20,
                adx_val > 20,
            ])
            return RegimeResult(
                regime=Regime.VOLATILE,
                confidence=confidence,
                adx_value=adx_val, bb_width=bb_w,
                atr_pct=atr_pct, rsi_value=rsi_val, slope=slope_val,
                timestamp=datetime.now(tz=IST),
                notes=f"High ATR {atr_pct:.1f}% or extreme RSI {rsi_val:.0f}",
            )

        # BREAKOUT: BB squeeze breaking out + surging ADX
        bb_squeeze   = bb_w < 0.04           # very tight bands
        adx_rising   = adx_val > 20 and (adx_series.iloc[-1] > adx_series.iloc[-3])
        price_thrust = abs(slope_val) > 1.0  # price moving fast

        if bb_squeeze and (adx_rising or price_thrust):
            confidence = self._confidence([
                bb_w < 0.04,
                adx_val > 20,
                price_thrust,
                adx_rising,
            ])
            direction = "bullish" if slope_val > 0 else "bearish"
            return RegimeResult(
                regime=Regime.BREAKOUT,
                confidence=confidence,
                adx_value=adx_val, bb_width=bb_w,
                atr_pct=atr_pct, rsi_value=rsi_val, slope=slope_val,
                timestamp=datetime.now(tz=IST),
                notes=f"BB squeeze breakout ({direction}), ADX {adx_val:.0f}",
            )

        # TRENDING: strong ADX + directional DI + EMA slope
        if adx_val > 25 and abs(slope_val) > 0.3:
            confidence = self._confidence([
                adx_val > 25,
                adx_val > 30,             # bonus for strong trend
                abs(slope_val) > 0.5,
                plus_val != minus_val,    # directional clarity
            ])
            direction = "up" if plus_val > minus_val else "down"
            return RegimeResult(
                regime=Regime.TRENDING,
                confidence=confidence,
                adx_value=adx_val, bb_width=bb_w,
                atr_pct=atr_pct, rsi_value=rsi_val, slope=slope_val,
                timestamp=datetime.now(tz=IST),
                notes=f"Trending {direction}, ADX {adx_val:.0f}, slope {slope_val:.2f}%",
            )

        # RANGING: low ADX + tight BB + flat slope
        if adx_val < 20 and abs(slope_val) < 0.3:
            confidence = self._confidence([
                adx_val < 20,
                adx_val < 15,             # bonus for very flat
                abs(slope_val) < 0.2,
                bb_w < 0.08,
            ])
            return RegimeResult(
                regime=Regime.RANGING,
                confidence=confidence,
                adx_value=adx_val, bb_width=bb_w,
                atr_pct=atr_pct, rsi_value=rsi_val, slope=slope_val,
                timestamp=datetime.now(tz=IST),
                notes=f"Ranging, ADX {adx_val:.0f}, BB width {bb_w:.3f}",
            )

        # Default: ambiguous, treat as RANGING with low confidence
        return RegimeResult(
            regime=Regime.RANGING,
            confidence=0.4,
            adx_value=adx_val, bb_width=bb_w,
            atr_pct=atr_pct, rsi_value=rsi_val, slope=slope_val,
            timestamp=datetime.now(tz=IST),
            notes=f"Ambiguous — ADX {adx_val:.0f}, slope {slope_val:.2f}%",
        )

    @staticmethod
    def _confidence(conditions: list[bool]) -> float:
        """
        Converts a list of boolean checks to a confidence score (0.5 – 1.0).
        More conditions true → higher confidence.
        Base confidence is 0.5 even with 0 bonus conditions.
        """
        true_count = sum(conditions)
        total      = len(conditions)
        return round(0.5 + 0.5 * (true_count / total), 2) if total > 0 else 0.5


# ── Module-level singleton ────────────────────────────────────────
regime_detector = RegimeDetector()
